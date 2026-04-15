"""Yardi submission service.

Identifies all fully-approved invoices, writes a timestamped Yardi JSON upload
file and an audit PDF, then deletes the submitted invoices from the database so
they no longer appear in the review queue.  Incomplete invoices are left in place.
"""

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
        audit_path = self.output_dir / f"yardi_audit_{stamp}.pdf"

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
    # Entry builder (shared by preview, JSON, and PDF)
    # ------------------------------------------------------------------

    def _build_entries(self, invoices: list[Invoice]) -> list[dict]:
        """Flatten invoices into a list of accounting entries, one per approved line item."""
        entries = []
        for inv in invoices:
            property_yardi_code = inv.property_reference.website_id if inv.property_reference else ""
            property_code = inv.property_reference.code if inv.property_reference else ""
            items = (
                inv.line_items
                .filter(item_type=InvoiceLineItem.ItemType.PRODUCT)
                .order_by("line_number")
            )
            for item in items:
                if not item.approved_gl:
                    continue
                line_total = item.line_total or Decimal("0")
                tax = item.tax_amount or Decimal("0")
                entries.append({
                    "property_yardi_code": property_yardi_code,
                    "property_code": property_code,
                    "gl_code": item.approved_gl.code,
                    "gl_description": item.approved_gl.description,
                    "amount": line_total + tax,
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
    # Audit PDF output
    # ------------------------------------------------------------------

    def _write_audit(self, entries: list[dict], now: datetime, path: Path) -> None:
        submitted_at_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")
        total_amount = sum(e["amount"] for e in entries)
        pages = self._build_audit_pdf_pages(entries, submitted_at_str, total_amount)
        self._write_pdf(path, pages)

    def _build_audit_pdf_pages(
        self,
        entries: list[dict],
        submitted_at_str: str,
        total_amount: Decimal,
    ) -> list[str]:
        rows_per_page = 24
        chunks = [
            entries[index:index + rows_per_page]
            for index in range(0, len(entries), rows_per_page)
        ] or [[]]
        pages = []

        for page_number, chunk in enumerate(chunks, start=1):
            commands: list[str] = []
            self._pdf_text(commands, 36, 570, "Yardi Submission Audit", size=18, bold=True)
            self._pdf_text(commands, 36, 548, f"Submitted by: {SUBMITTED_BY}", size=9)
            self._pdf_text(commands, 36, 534, f"Submitted at: {submitted_at_str}", size=9)
            self._pdf_text(commands, 36, 520, f"Entries: {len(entries)}", size=9)
            self._pdf_text(commands, 150, 520, f"Total: ${total_amount:.2f}", size=9, bold=True)
            self._pdf_text(commands, 700, 570, f"Page {page_number} of {len(chunks)}", size=9)
            self._pdf_line(commands, 36, 505, 756, 505)

            columns = [
                ("Date", 36),
                ("Property", 100),
                ("Yardi ID", 160),
                ("GL", 210),
                ("GL Description", 250),
                ("Amount", 510),
                ("Invoice Ref", 590),
            ]
            for label, x in columns:
                self._pdf_text(commands, x, 486, label, size=8, bold=True)
            self._pdf_line(commands, 36, 478, 756, 478)

            y = 460
            for entry in chunk:
                description = self._truncate(entry["gl_description"], 40)
                self._pdf_text(commands, 36, y, entry["date"].isoformat() if entry["date"] else "", size=8)
                self._pdf_text(commands, 100, y, entry["property_code"], size=8)
                self._pdf_text(commands, 160, y, entry["property_yardi_code"], size=8)
                self._pdf_text(commands, 210, y, entry["gl_code"], size=8)
                self._pdf_text(commands, 250, y, description, size=8)
                self._pdf_text(commands, 510, y, f"${entry['amount']:.2f}", size=8)
                self._pdf_text(commands, 590, y, entry["reference"], size=8)
                self._pdf_line(commands, 36, y - 8, 756, y - 8)
                y -= 16

            pages.append("\n".join(commands))

        return pages

    def _write_pdf(self, path: Path, page_streams: list[str]) -> None:
        objects: list[bytes] = []

        def add_object(payload: str | bytes) -> int:
            if isinstance(payload, str):
                payload = payload.encode("latin-1", errors="replace")
            objects.append(payload)
            return len(objects)

        catalog_id = add_object("")
        pages_id = add_object("")
        font_regular_id = add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        font_bold_id = add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
        page_ids = []

        for stream in page_streams:
            stream_bytes = stream.encode("latin-1", errors="replace")
            content_id = add_object(
                b"<< /Length " + str(len(stream_bytes)).encode("ascii") + b" >>\nstream\n"
                + stream_bytes
                + b"\nendstream"
            )
            page_id = add_object(
                (
                    f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 792 612] "
                    f"/Resources << /Font << /F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R >> >> "
                    f"/Contents {content_id} 0 R >>"
                )
            )
            page_ids.append(page_id)

        objects[catalog_id - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("latin-1")
        kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
        objects[pages_id - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("latin-1")

        output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for index, payload in enumerate(objects, start=1):
            offsets.append(len(output))
            output.extend(f"{index} 0 obj\n".encode("ascii"))
            output.extend(payload)
            output.extend(b"\nendobj\n")

        xref_offset = len(output)
        output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
        output.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
        output.extend(
            (
                f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
                f"startxref\n{xref_offset}\n%%EOF\n"
            ).encode("ascii")
        )

        path.write_bytes(output)

    def _pdf_text(
        self,
        commands: list[str],
        x: int,
        y: int,
        text: str,
        *,
        size: int,
        bold: bool = False,
    ) -> None:
        font = "F2" if bold else "F1"
        commands.append(f"BT /{font} {size} Tf {x} {y} Td ({self._pdf_escape(str(text))}) Tj ET")

    def _pdf_line(self, commands: list[str], x1: int, y1: int, x2: int, y2: int) -> None:
        commands.append(f"0.82 0.86 0.91 RG {x1} {y1} m {x2} {y2} l S 0 0 0 RG")

    def _pdf_escape(self, value: str) -> str:
        value = value.encode("latin-1", errors="replace").decode("latin-1")
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    def _truncate(self, value: str, max_length: int) -> str:
        if len(value) <= max_length:
            return value
        return value[: max_length - 3].rstrip() + "..."
