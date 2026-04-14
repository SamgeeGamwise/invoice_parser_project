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
        total_amount = sum(inv.grand_total or Decimal("0") for inv in invoices)
        all_count = Invoice.objects.count()
        return {
            "invoice_count": len(invoices),
            "total_amount": total_amount,
            "remaining_count": all_count - len(invoices),
            "invoices": invoices,
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

        total_amount = sum(inv.grand_total or Decimal("0") for inv in invoices)
        line_item_count = sum(
            inv.line_items.filter(item_type=InvoiceLineItem.ItemType.PRODUCT).count()
            for inv in invoices
        )

        self.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.output_dir / f"yardi_upload_{stamp}.json"
        audit_path = self.output_dir / f"yardi_audit_{stamp}.csv"

        # Write files BEFORE deleting invoices. If a write fails, invoices are preserved.
        try:
            self._write_json(invoices, now, json_path)
            self._write_audit(invoices, now, audit_path)
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
    # JSON output
    # ------------------------------------------------------------------

    def _write_json(self, invoices: list[Invoice], now: datetime, path: Path) -> None:
        payload = {
            "submitted_at": now.isoformat(),
            "submitted_by": SUBMITTED_BY,
            "invoice_count": len(invoices),
            "total_amount": str(sum(inv.grand_total or Decimal("0") for inv in invoices)),
            "invoices": [self._invoice_to_dict(inv) for inv in invoices],
        }
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=_json_default)

    def _invoice_to_dict(self, inv: Invoice) -> dict:
        return {
            "invoice_number": inv.invoice_number,
            "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
            "purchase_date": inv.purchase_date.isoformat() if inv.purchase_date else None,
            "property_code": inv.property_reference.code if inv.property_reference else None,
            "property_website_id": (
                inv.property_reference.website_id if inv.property_reference else None
            ),
            "po_number": inv.po_number or None,
            "purchaser": inv.purchaser or None,
            "subtotal": str(inv.subtotal) if inv.subtotal is not None else None,
            "tax_total": str(inv.tax_total) if inv.tax_total is not None else None,
            "grand_total": str(inv.grand_total) if inv.grand_total is not None else None,
            "line_items": [
                self._item_to_dict(item)
                for item in inv.line_items
                    .filter(item_type=InvoiceLineItem.ItemType.PRODUCT)
                    .order_by("line_number")
            ],
        }

    def _item_to_dict(self, item: InvoiceLineItem) -> dict:
        return {
            "line_number": item.line_number,
            "description": item.description,
            "quantity": item.quantity,
            "unit_price": str(item.unit_price) if item.unit_price is not None else None,
            "line_total": str(item.line_total) if item.line_total is not None else None,
            "gl_code": item.approved_gl.code if item.approved_gl else None,
            "gl_description": item.approved_gl.description if item.approved_gl else None,
            "asin": item.asin or None,
            "vendor": item.vendor or None,
            "order_number": item.order_number or None,
        }

    # ------------------------------------------------------------------
    # Audit CSV output
    # ------------------------------------------------------------------

    def _write_audit(self, invoices: list[Invoice], now: datetime, path: Path) -> None:
        submitted_at_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)

            # Header
            writer.writerow([
                "Invoice #",
                "Invoice Date",
                "Property Code",
                "Property Website ID",
                "Purchaser",
                "PO Number",
                "Subtotal",
                "Tax",
                "Grand Total",
                "Line Items",
                "Submitted By",
                "Submitted At",
            ])

            # One row per invoice
            for inv in invoices:
                item_count = inv.line_items.filter(
                    item_type=InvoiceLineItem.ItemType.PRODUCT
                ).count()
                writer.writerow([
                    inv.invoice_number,
                    inv.invoice_date.isoformat() if inv.invoice_date else "",
                    inv.property_reference.code if inv.property_reference else "",
                    inv.property_reference.website_id if inv.property_reference else "",
                    inv.purchaser or "",
                    inv.po_number or "",
                    str(inv.subtotal or ""),
                    str(inv.tax_total or ""),
                    str(inv.grand_total or ""),
                    item_count,
                    SUBMITTED_BY,
                    submitted_at_str,
                ])

            # Summary totals
            writer.writerow([])
            writer.writerow(["--- TOTALS ---"])
            writer.writerow([
                "TOTAL",
                "",
                f"{len(invoices)} invoice(s)",
                "",
                "",
                "",
                str(sum(inv.subtotal or Decimal("0") for inv in invoices)),
                str(sum(inv.tax_total or Decimal("0") for inv in invoices)),
                str(sum(inv.grand_total or Decimal("0") for inv in invoices)),
                str(sum(
                    inv.line_items.filter(item_type=InvoiceLineItem.ItemType.PRODUCT).count()
                    for inv in invoices
                )),
                SUBMITTED_BY,
                submitted_at_str,
            ])
