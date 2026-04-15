# Notes for the Handwritten README

## Project Summary

This project is a local Django dashboard for processing Amazon Business invoice PDFs for Monarch. The app extracts invoice data, validates property codes, suggests GL codes for each individual line item, supports human review and overrides, and reports spend by GL code and property.

The key idea is that the invoice-level GL code is treated as a helpful hint, not the final answer. Individual line items may belong to different GL categories, so the app classifies each line item separately and lets a reviewer approve or correct the suggestion.

## How to Run the App

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Then open:

```text
http://127.0.0.1:8000/
```

The project also includes a `Makefile` with helper commands:

```bash
make setup
make run
make migrate
make clear-invoices
make clear-codes
```

## What the App Does

- Uploads Amazon invoice PDFs in bulk.
- Reads PDF text locally using `pdfplumber`.
- Extracts invoice metadata such as invoice number, dates, purchaser, PO number, invoice-level GL code, property code, subtotal, tax, total, and line items.
- Imports GL accounts and property references from the provided Excel files.
- Normalizes property codes to uppercase and validates them against the property reference data.
- Suggests a GL code for each product line item.
- Lets a reviewer approve or override suggested GL codes.
- Blocks approval when the property code is missing or invalid.
- Provides an approval queue for unreviewed items.
- Shows reports for spend by GL and spend/items by property.
- Provides a mock Yardi submission flow that exports JSON and PDF audit files for fully approved invoices.

## Technologies Used

- Python
- Django
- Django templates
- SQLite
- `pdfplumber`
- `sentence-transformers`
- Local Excel reference files
- Django ORM models
- Server-sent events/threading for upload progress
- JSON and PDF output files

## Architecture and Design Decisions

I chose Django because the assignment needed a local full-stack app with uploads, forms, persistence, review pages, and reporting. Django made it possible to build those pieces in one framework without adding a separate frontend stack.

The app is split into service classes so each part has one clear responsibility. PDF reading, invoice parsing, reference-data loading, GL classification, database persistence, reporting, and Yardi-style output are separate pieces under `apps/invoices/services/`.

The invoice parser is heuristic because the supplied PDFs are Amazon invoices with consistent text patterns. Regular expressions are used to extract known fields and line items. This is simpler and easier to debug than a larger document AI pipeline for the scope of the assignment.

For GL classification, the app combines several signals:

- The GL code printed on the invoice.
- Semantic similarity between the line-item description and GL account descriptions.
- A small boost for commonly used GL account ranges.
- KNN-style matching against previously approved line items.

The human review workflow is central to the design. The app suggests GL codes, but the reviewer is the final authority. Approved decisions are saved and used as history to improve future suggestions.

SQLite and local files were chosen because this is a local interview project. They keep the app easy to install and run on one machine.

## Assumptions

- The invoices are Amazon Business PDFs with extractable text.
- The PDFs are not scanned image-only documents.
- Property codes appear on the invoice and can be normalized to uppercase.
- The provided GL list and property list are the source of truth.
- The invoice-level GL code is useful but may be too broad for line-item coding.
- Product line items need review.
- Shipping and discounts can default to the invoice-level GL code.
- Human-approved GL choices are more trustworthy than model suggestions.
- The app is meant for a local single-user demo, not production multi-user accounting.

## Known Limitations

- The PDF parser depends on Amazon's invoice layout, so unusual formats may need parser changes.
- The GL model suggests codes but cannot guarantee accounting correctness.
- The first use of the embedding model may require a model download/cache step.
- SQLite is fine locally but not ideal for production concurrency.
- Bulk processing is local and thread-based, not backed by a durable job queue.
- There is no login, permission system, or reviewer audit trail.
- The Yardi submission is a local JSON/PDF export, not a real Yardi API integration.
- Re-uploading an invoice can reset previous approval work.
- Reports are useful but still basic.

## What I Would Improve for Production

- Add authentication, user roles, and reviewer attribution.
- Move background processing to Celery, Django-Q, or another durable worker queue.
- Persist upload/job progress in the database instead of process memory.
- Add a complete audit log for approval changes.
- Add stronger invoice validation and an exception queue.
- Add CSV/Excel exports for accounting review.
- Add better search, filtering, and pagination throughout the dashboard.
- Evaluate the classifier against labeled examples and track accuracy.
- Add a real Yardi API integration.
- Move from SQLite to Postgres.
- Add production settings for secrets, storage, deployment, logging, and security.

## Short Opening Sentence

This app is a local Django dashboard that turns Amazon invoice PDFs into reviewable accounting data by extracting invoice line items, suggesting GL codes per line item, validating property codes, and producing spend reports plus a mock Yardi submission export.
