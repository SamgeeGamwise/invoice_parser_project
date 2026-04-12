# Amazon Invoice Coding Dashboard

Local Django dashboard for reviewing Amazon invoice PDFs, extracting structured invoice data, suggesting line-item GL mappings from the provided chart of accounts, capturing human approvals or overrides, and reporting spend by GL and property.

## What the app does

- Upload one Amazon invoice PDF or a bulk batch of PDFs through a local web UI.
- Extract invoice metadata including invoice number, invoice date, purchase date, purchaser, PO number, invoice-level GL, property code, totals, and line items.
- Normalize property codes and validate them against the provided property reference list.
- Treat the invoice-level GL as a hint, not the final answer.
- Suggest a GL for each individual line item using local, explainable signals.
- Let a reviewer approve or override the suggested GL for each line item.
- Reuse prior approved line items as local history to strengthen future suggestions.
- Show reporting tables for spend by GL and item activity by property.
- Overwrite `data/output/parsed_invoices.json` with the latest bulk import snapshot.

## How to run it

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Open `http://127.0.0.1:8000/`.

## Main workflow

1. Upload a single PDF from the dashboard, or open the bulk workflow for multiple PDFs.
2. The app reads the PDF text locally with `pdfplumber`.
3. The parser extracts invoice metadata and line items.
4. The reference-data sync loads the GL chart and property list from the supplied spreadsheets.
5. Each line item receives one or more GL suggestions with reasoning.
6. Bulk uploads run as tracked local jobs and show progress while files are being processed.
7. Parsed invoices and line items are saved to SQLite for review.
8. A reviewer approves or overrides GLs on the invoice detail page.
9. Reports aggregate spend by effective GL and by property.

## Architecture and design decisions

- Framework: Django with server-rendered templates.
  Rationale: the assignment calls for a working local full-stack app with uploads, persistence, forms, and review workflows. Django gives those pieces without adding frontend complexity.

- Parsing and classification are separated into services.
  The codebase keeps PDF reading, invoice parsing, reference-data loading, GL suggestion logic, persistence, and reporting in different services under `apps/invoices/services/`.

- Human-in-the-loop classification is the product center.
  The app stores both the invoice-level GL hint and the line-item suggested or approved GL separately. Review happens at the line-item level.

- Explainable heuristics over black-box automation.
  GL suggestions are based on a blend of:
  - keyword hints tied to likely GL categories
  - overlap with the GL chart descriptions
  - prior approved item history
  - the invoice-level GL as a weak prior

- Local reference files are the source of truth.
  The GL list and property list are read directly from the provided Excel files and synchronized into local tables.

- Bulk uploads favor local stability over maximum throughput.
  The app allows up to 500 PDFs per request, spills larger uploads to disk early, and processes them in 50-file batches with a bounded worker pool.

- Reporting uses approved GLs first.
  If a reviewer has approved a line item, reporting uses that approved GL. Otherwise it falls back to the current suggestion.

## Assumptions

- Property codes are normalized to uppercase for consistency.
  Example: `ssoh` becomes `SSOH`.

- Invoice-level GL is informational only.
  It is preserved, shown in the UI, and used as one weak signal, but it is not treated as the final coding answer.

- The `6000-7070` GL range is the primary review range for line-item coding.
  The reference loader marks those accounts as the main candidate set for suggestions and dropdown review.

- Non-merchandise lines such as discounts and shipping are kept separate from product lines.
  For this MVP, those lines default to the invoice-level GL with a lower-confidence explanation unless a reviewer overrides them.

- The suggestion engine is intentionally modest and reviewable.
  It is designed to reduce repetitive work, not eliminate review.

- Prior approved items are the “learning” mechanism for the MVP.
  The app does not currently train a separate machine-learning model.

## Known limitations

- Amazon invoice parsing is still heuristic.
  Some PDFs will contain formatting differences that may require parser tuning.

- The GL suggestion engine uses rules and history, not accounting expertise.
  It should be treated as a coding assistant, not an authoritative accounting engine.

- Bulk-job progress starts after the upload request is accepted.
  The app shows processing progress, not browser-level network upload progress.

- Background jobs are in-memory for local use.
  If the Django process restarts, in-flight job state is lost.

- Spreadsheet ingestion uses a lightweight internal XLSX reader.
  It is sufficient for the provided files, but not intended as a general spreadsheet ETL layer.

- Reporting is table-based and intentionally simple.
  There are no charts, exports beyond the JSON snapshot, or multi-user workflows yet.

## What I would change for production

- Move bulk jobs to a durable worker queue such as Celery or Django-Q.
- Persist job state in the database instead of process memory.
- Add authentication and reviewer attribution.
- Add audit history for approval changes.
- Add richer parsing validation and invoice exception queues.
- Add upload-progress support at the browser level.
- Expand the classifier with a small local text model trained from approved history, while still keeping human approval as the source of truth.
- Add CSV or Excel exports for reviewed coding and summary reporting.
- Add pagination, filtering, search, and better analytics UI.

## Project layout

- `apps/invoices/models.py`: GL, property, invoice, and line-item persistence.
- `apps/invoices/views.py`: dashboard, upload, review, bulk-job, and report views.
- `apps/invoices/services/invoice_parser.py`: invoice metadata and line-item extraction.
- `apps/invoices/services/classification.py`: explainable GL suggestion logic.
- `apps/invoices/services/reference_data.py`: GL and property reference synchronization.
- `apps/invoices/services/repository.py`: persistence of parsed invoices and line items.
- `apps/invoices/services/reporting.py`: report aggregation helpers.
- `apps/invoices/services/bulk_jobs.py`: local tracked bulk-processing jobs.
- `data/reference/`: supplied GL and property spreadsheets.
- `data/output/parsed_invoices.json`: latest bulk import snapshot.

## Testing

Run:

```bash
./.venv/bin/python manage.py test apps.invoices
```
