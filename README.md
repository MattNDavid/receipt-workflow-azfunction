# Receipt Function

An Azure Function app for tracking household expenses. You POST a photo of a receipt, Azure AI Document Intelligence reads it, and the result is stored in Cosmos DB. Every Monday morning it emails a summary of the previous week's spending, with a CSV of line items attached. You can also trigger the report by hand.

## How it works

```
receipt image ──POST /api/receipts──▶ Document Intelligence (prebuilt-receipt)
                                              │
                                              ▼
                                Cosmos DB  expenses / receipts
                                              │
        timer (Mon 15:00 UTC) or POST /api/report
                                              ▼
                           Azure Communication Services email
                              (HTML summary + CSV attachment)
```

- **Deduplication:** each receipt's document ID is the SHA-256 hash of the image, so uploading the same image again overwrites the existing record instead of creating a duplicate.
- **Weekly grouping:** receipts are partitioned by `weekStart`, the Monday of the week of the transaction date. If no date can be read, today's date is used.
- **Confidence flag:** the lowest field confidence is stored as `minConfidence`. Receipts below 0.8 get a ⚠️ in the email and `Needs Review = yes` in the CSV.

## Endpoints

All HTTP endpoints use function-level auth, so pass a function key as `?code=<key>` or in the `x-functions-key` header.

### `POST /api/receipts`

Upload a receipt image as the raw request body.

| Query param | Required | Description |
|---|---|---|
| `name` | No | Original file name, stored as `sourceFile` |

```bash
curl -X POST "https://<app>.azurewebsites.net/api/receipts?name=receipt1.jpeg&code=<key>" \
     --data-binary @receipt1.jpeg
```

Responses:
- `201` returns the stored receipt as JSON (merchant, date, subtotal, tax, total, line items).
- `400` means the request body was empty.
- `422` means no receipt was detected in the image.
- `500` means processing failed. Details are in the function logs.

### `POST /api/report`

Send the weekly report now.

| Query param | Required | Description |
|---|---|---|
| `week` | No | Monday of the week to report, `YYYY-MM-DD`. Defaults to last week. |
| `email` | No | Recipient. Defaults to `EMAIL_TO`. |

```bash
curl -X POST "https://<app>.azurewebsites.net/api/report?week=2026-09-28&code=<key>"
```

Returns a JSON summary: `{ "week", "receipts", "total", "emailTo" }`.

### Weekly timer

`weekly_report` runs every Monday at 15:00 UTC (8am PDT / 7am PST) and emails the previous week's report to `EMAIL_TO`. Week boundaries are calculated in `America/Los_Angeles` time.

## Azure resources

| Resource | Notes |
|---|---|
| Function App | Python, Functions v4 runtime |
| Document Intelligence | Uses the `prebuilt-receipt` model |
| Cosmos DB (NoSQL) | Database `expenses`, container `receipts`, partition key `/weekStart` |
| Communication Services + Email | Needs a verified sender domain |

## Configuration

Set these as app settings in Azure. For local runs, put them in `local.settings.json` under `Values`.

| Setting | Description |
|---|---|
| `DI_ENDPOINT` | Document Intelligence endpoint URL |
| `DI_KEY` | Document Intelligence key |
| `COSMOS_ENDPOINT` | Cosmos DB account endpoint |
| `COSMOS_KEY` | Cosmos DB account key |
| `ACS_CONNECTION_STRING` | Azure Communication Services connection string |
| `EMAIL_SENDER` | Verified sender address, e.g. `DoNotReply@<domain>` |
| `EMAIL_TO` | Default report recipient |

## Running locally

Requires Python 3 and [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-run-local) v4.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
func start
```

`local.settings.json` also needs `"FUNCTIONS_WORKER_RUNTIME": "python"` and an `AzureWebJobsStorage` value for the timer trigger. You can use `"UseDevelopmentStorage=true"` with Azurite.

`receipt1.jpeg` is a sample image for testing the upload endpoint.

## Deployment

Pushing to `main` runs [.github/workflows/main_rctloader.yml](.github/workflows/main_rctloader.yml). The workflow installs dependencies into `.python_packages/`, zips the app, and deploys it to the `rctloader` Function App using OIDC login. You can also start it manually from the Actions tab.
