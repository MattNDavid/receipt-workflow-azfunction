import io
import os
import json
import hashlib
import logging
import csv
import html
import base64
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo

import azure.functions as func
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.core.credentials import AzureKeyCredential
from azure.cosmos import CosmosClient
from azure.communication.email import EmailClient

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

# Created once per instance and reused across requests
di = DocumentIntelligenceClient(
    endpoint=os.environ["DI_ENDPOINT"],
    credential=AzureKeyCredential(os.environ["DI_KEY"]),
)
container = (
    CosmosClient(os.environ["COSMOS_ENDPOINT"], credential=os.environ["COSMOS_KEY"])
    .get_database_client("expenses")
    .get_container_client("receipts")
)
email_client = EmailClient.from_connection_string(os.environ["ACS_CONNECTION_STRING"])
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

def money(field):
    return field.value_currency.amount if field and field.value_currency else None


def text(field):
    return field.value_string if field else None


def week_start(d: date) -> str:
    return (d - timedelta(days=d.weekday())).isoformat()


def process_receipt(image_bytes: bytes, source_name: str | None) -> dict:
    result = di.begin_analyze_document(
        "prebuilt-receipt", body=io.BytesIO(image_bytes)
    ).result()
    if not result.documents:
        raise ValueError("No receipt found in image")

    fields = result.documents[0].fields
    date_field = fields.get("TransactionDate")
    tx_date = date_field.value_date if date_field and date_field.value_date else date.today()

    items = []
    if fields.get("Items"):
        for item in fields["Items"].value_array:
            obj = item.value_object
            items.append({
                "description": text(obj.get("Description")),
                "quantity": obj["Quantity"].value_number if obj.get("Quantity") else None,
                "totalPrice": money(obj.get("TotalPrice")),
            })

    confidences = [f.confidence for f in fields.values() if f.confidence is not None]

    doc = {
        "id": hashlib.sha256(image_bytes).hexdigest(),
        "weekStart": week_start(tx_date),
        "date": tx_date.isoformat(),
        "merchant": text(fields.get("MerchantName")),
        "subtotal": money(fields.get("Subtotal")),
        "tax": money(fields.get("TotalTax")),
        "total": money(fields.get("Total")),
        "items": items,
        "sourceFile": source_name,
        "minConfidence": min(confidences) if confidences else None,
    }
    container.upsert_item(doc)
    return doc


@app.route(route="receipts", methods=["POST"])
def upload_receipt(req: func.HttpRequest) -> func.HttpResponse:
    image_bytes = req.get_body()
    if not image_bytes:
        return func.HttpResponse("Send the receipt image as the request body.", status_code=400)

    try:
        doc = process_receipt(image_bytes, req.params.get("name"))
    except ValueError as e:
        return func.HttpResponse(str(e), status_code=422)
    except Exception:
        logging.exception("Failed to process receipt")
        return func.HttpResponse("Processing failed.", status_code=500)

    return func.HttpResponse(json.dumps(doc), status_code=201, mimetype="application/json")

def previous_week_start() -> str:
    today = datetime.now(LOCAL_TZ).date()
    return (today - timedelta(days=today.weekday() + 7)).isoformat()


def build_and_send_report(week: str, email_to: str | None = None) -> dict:
    email_to = email_to or os.environ["EMAIL_TO"]
    receipts = list(container.query_items(
        query="SELECT * FROM c ORDER BY c.date",
        partition_key=week,
    ))

    week_end = (date.fromisoformat(week) + timedelta(days=6)).isoformat()
    grand_total = sum(r["total"] or 0 for r in receipts)

    # CSV: one row per line item, receipt fields repeated
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Date", "Merchant", "Item", "Qty", "Item Price", "Receipt Total", "Needs Review"])
    for r in receipts:
        review = "yes" if (r.get("minConfidence") or 1) < 0.8 else ""
        items = r.get("items") or [{}]
        for it in items:
            writer.writerow([
                r["date"], r.get("merchant") or "",
                it.get("description") or "", it.get("quantity") or "",
                it.get("totalPrice") or "", r.get("total") or "", review,
            ])

    # HTML: one row per receipt
    rows_html = "".join(
        f"<tr><td>{r['date']}</td><td>{html.escape(r.get('merchant') or 'Unknown')}</td>"
        f"<td style='text-align:right'>${(r.get('total') or 0):.2f}</td>"
        f"<td>{'⚠️' if (r.get('minConfidence') or 1) < 0.8 else ''}</td></tr>"
        for r in receipts
    )
    body_html = f"""
    <h2>Matthew's Family Expenses: {week} to {week_end}</h2>
    <p>{len(receipts)} receipts, total <b>${grand_total:.2f}</b></p>
    <table border="1" cellpadding="6" cellspacing="0">
      <tr><th>Date</th><th>Merchant</th><th>Total</th><th>Check</th></tr>
      {rows_html}
    </table>
    <p>⚠️ = low-confidence extraction, worth double-checking. Line items are in the attached CSV.</p>
    """

    message = {
        "senderAddress": os.environ["EMAIL_SENDER"],
        "recipients": {"to": [{"address": email_to}]},
        "content": {
            "subject": f"Weekly expenses {week} to {week_end}: ${grand_total:.2f}",
            "plainText": f"{len(receipts)} receipts, total ${grand_total:.2f}. See attached CSV.",
            "html": body_html,
        },
        "attachments": [{
            "name": f"expenses_{week}.csv",
            "contentType": "text/csv",
            "contentInBase64": base64.b64encode(buf.getvalue().encode("utf-8")).decode("ascii"),
        }],
    }
    email_client.begin_send(message).result()
    return {"week": week, "receipts": len(receipts), "total": grand_total, "emailTo": email_to}


# Mondays 15:00 UTC = 8am PDT / 7am PST
@app.timer_trigger(schedule="0 0 15 * * 1", arg_name="timer", run_on_startup=False)
def weekly_report(timer: func.TimerRequest) -> None:
    summary = build_and_send_report(previous_week_start())
    logging.info("Weekly report sent: %s", summary)


@app.route(route="report", methods=["POST"])
def send_report_now(req: func.HttpRequest) -> func.HttpResponse:
    week = req.params.get("week") or previous_week_start()
    try:
        date.fromisoformat(week)
    except ValueError:
        return func.HttpResponse("week must be YYYY-MM-DD (a Monday)", status_code=400)
    summary = build_and_send_report(week, req.params.get("email"))
    return func.HttpResponse(json.dumps(summary), status_code=200, mimetype="application/json")