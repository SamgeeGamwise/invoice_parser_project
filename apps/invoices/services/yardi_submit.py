"""Yardi submission service.

Identifies all fully-approved invoices, writes a timestamped Yardi JSON upload
file and an audit CSV, then deletes the submitted invoices from the database so
they no longer appear in the review queue.  Incomplete invoices are left in place.
"""

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db.models import Exists, OuterRef

from ..models import Invoice, InvoiceLineItem


SUBMITTED_BY = "MIMG Invoice Parser"


def _json_default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Not JSON serializable: {type(obj).__name__}")


@dataclass
class SubmissionResult:
    submitted_at: datetime
    invoice_count: int
    line_item_count: int
    total_amount: Decimal
    json_path: Path
    audit_path: Path
    remaining_count: int   # invoices still in the system (not ready, kept)


class YardiSubmitService:

    def __init__(self) -> None:
        self.output_dir: Path = settings.OUTPUT_DIR

    # ------------------------------------------------------------------
    # Queryset helpers
    # ------------------------------------------------------------------

    def ready_invoices_qs(self):
        """Queryset of invoices that are fully approved and ready to submit.

        Criteria:
          - property_reference is set
          - at least one PRODUCT line item exists
          - every PRODUCT line item has an approved GL code
        """
        has_unreviewed = InvoiceLineItem.objects.filter(
            invoice=OuterRef("pk"),
            item_type=InvoiceLineItem.ItemType.PRODUCT,
            approved_gl__isnull=True,
        )
        has_product_items = InvoiceLineItem.objects.filter(
            invoice=OuterRef("pk"),
            item_type=InvoiceLineItem.ItemType.PRODUCT,
        )
        return (
            Invoice.objects
            .filter(property_reference__isnull=False)
            .filter(Exists(has_product_items))
            .exclude(Exists(has_unreviewed))
            .select_related("property_reference")
            .prefetch_related("line_items__approved_gl")
            .order_by("invoice_date", "invoice_number")
        )

    # ------------------------------------------------------------------
    # Preview (no side effects)
    # ------------------------------------------------------------------

    def preview(self) -> dict:
        """Return a summary dict for the confirmation page.  No data is written."""
        ready_qs = self.ready_invoices_qs()
        invoices = list(ready_qs)
        entries = self._build_entries(invoices)
        total_amount = sum(e["amount"] for e in entries)
        all_count = Invoice.objects.count()

        # Group entries by (property_yardi_code, gl_code) for the preview table.
        grouped: dict[tuple, dict] = {}
        for e in entries:
            key = (e["property_yardi_code"], e["gl_code"])
            if key not in grouped:
                grouped[key] = {
                    "property_yardi_code": e["property_yardi_code"],
                    "gl_code": e["gl_code"],
                    "gl_description": e["gl_description"],
                    "amount": Decimal("0"),
                    "entry_count": 0,
                }
            grouped[key]["amount"] += e["amount"]
            grouped[key]["entry_count"] += 1

        grouped_entries = sorted(grouped.values(), key=lambda x: (x["property_yardi_code"], x["gl_code"]))

        return {
            "invoice_count": len(invoices),
            "entry_count": len(entries),
            "total_amount": total_amount,
            "remaining_count": all_count - len(invoices),
            "grouped_entries": grouped_entries,
        }

    # ------------------------------------------------------------------
    # Submission (writes files, deletes submitted invoices)
    # ------------------------------------------------------------------

    def submit(self) -> SubmissionResult:
        """Generate output files, then delete submitted invoices."""
        invoices = list(self.ready_invoices_qs())

        if not invoices:
            raise ValueError(
                "No invoices are ready to submit. "
                "All invoices need a validated property code and every product line item must be approved."
            )

        now = datetime.now(tz=timezone.utc)
        stamp = now.strftime("%Y%m%d_%H%M%S")

        all_count = Invoice.objects.count()
        remaining_count = all_count - len(invoices)

        entries = self._build_entries(invoices)
        total_amount = sum(e["amount"] for e in entries)
        line_item_count = len(entries)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.output_dir / f"yardi_upload_{stamp}.json"
        audit_path = self.output_dir / f"yardi_audit_{stamp}.csv"

        # Write files BEFORE deleting invoices. If a write fails, invoices are preserved.
        try:
            self._write_json(invoices, entries, now, json_path)
            self._write_audit(entries, now, audit_path)
        except OSError as exc:
            raise RuntimeError(
                f"Failed to write submission files: {exc}. "
                "Invoices have NOT been removed — check disk space and permissions, then try again."
            ) from exc

        # Offload submitted invoices (line items cascade via FK)
        Invoice.objects.filter(pk__in=[inv.pk for inv in invoices]).delete()

        return SubmissionResult(
            submitted_at=now,
            invoice_count=len(invoices),
            line_item_count=line_item_count,
            total_amount=total_amount,
            json_path=json_path,
            audit_path=audit_path,
            remaining_count=remaining_count,
        )

    # ------------------------------------------------------------------
    # Entry builder (shared by preview, JSON, and CSV)
    # ------------------------------------------------------------------

    def _build_entries(self, invoices: list[Invoice]) -> list[dict]:
        """Flatten invoices into a list of accounting entries, one per approved line item."""
        entries = []
        for inv in invoices:
            property_yardi_code = inv.property_reference.website_id if inv.property_reference else ""
            items = (
                inv.line_items
                .filter(item_type=InvoiceLineItem.ItemType.PRODUCT)
                .order_by("line_number")
            )
            for item in items:
                if not item.approved_gl:
                    continue
                entries.append({
                    "property_yardi_code": property_yardi_code,
                    "gl_code": item.approved_gl.code,
                    "gl_description": item.approved_gl.description,
                    "amount": item.line_total or Decimal("0"),
                    "date": inv.invoice_date,
                    "reference": inv.invoice_number,
                })
        return entries

    # ------------------------------------------------------------------
    # JSON output
    # ------------------------------------------------------------------

    def _write_json(self, invoices: list[Invoice], entries: list[dict], now: datetime, path: Path) -> None:
        payload = {
            "submitted_at": now.isoformat(),
            "submitted_by": SUBMITTED_BY,
            "invoice_count": len(invoices),
            "entry_count": len(entries),
            "total_amount": str(sum(e["amount"] for e in entries)),
            "entries": [
                {
                    "property_yardi_code": e["property_yardi_code"],
                    "gl_code": e["gl_code"],
                    "gl_description": e["gl_description"],
                    "amount": str(e["amount"]),
                    "date": e["date"].isoformat() if e["date"] else None,
                    "reference": e["reference"],
                }
                for e in entries
            ],
        }
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=_json_default)

    # ------------------------------------------------------------------
    # Audit CSV output
    # ------------------------------------------------------------------

    def _write_audit(self, entries: list[dict], now: datetime, path: Path) -> None:
        submitted_at_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)

            writer.writerow([
                "Date",
                "Property (Yardi Code)",
                "GL Code",
                "GL Description",
                "Amount",
                "Invoice Reference",
                "Submitted By",
                "Submitted At",
            ])

            for e in entries:
                writer.writerow([
                    e["date"].isoformat() if e["date"] else "",
                    e["property_yardi_code"],
                    e["gl_code"],
                    e["gl_description"],
                    str(e["amount"]),
                    e["reference"],
                    SUBMITTED_BY,
                    submitted_at_str,
                ])

            writer.writerow([])
            writer.writerow([
                "TOTAL", "", "", "",
                str(sum(e["amount"] for e in entries)),
                f"{len(entries)} entries",
                SUBMITTED_BY,
                submitted_at_str,
            ])
