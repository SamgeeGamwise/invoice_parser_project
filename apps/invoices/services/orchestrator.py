import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
from typing import BinaryIO

from ..schemas import ParsedInvoice, SourceFileInfo
from .classification import LineItemGLClassifierService
from .invoice_parser import AmazonInvoiceParserService
from .pdf_reader import PdfReaderService
from .reference_data import ReferenceDataSyncService

# pdfplumber is I/O-bound so threads work well here.
# Cap workers so memory stays reasonable on large batches.
_MAX_WORKERS = min(16, (os.cpu_count() or 4) * 2)


@dataclass
class BulkProcessingResult:
    invoices: list[ParsedInvoice]
    errors: list[dict]

    @property
    def success_count(self) -> int:
        return len(self.invoices)

    @property
    def error_count(self) -> int:
        return len(self.errors)


class InvoiceProcessingService:
    """
    Thin orchestration layer that keeps views free of parsing logic.

    Each public method follows the same pattern:
      1. Extract raw text via PdfReaderService
      2. Parse structured fields via AmazonInvoiceParserService
      3. Enrich with GL suggestions and property validation
    """

    def __init__(
        self,
        pdf_reader: PdfReaderService | None = None,
        invoice_parser: AmazonInvoiceParserService | None = None,
        classifier: LineItemGLClassifierService | None = None,
        reference_data: ReferenceDataSyncService | None = None,
    ) -> None:
        self.pdf_reader = pdf_reader or PdfReaderService()
        self.invoice_parser = invoice_parser or AmazonInvoiceParserService()
        self.classifier = classifier or LineItemGLClassifierService()
        self.reference_data = reference_data or ReferenceDataSyncService()

    def process(self, file_obj: BinaryIO) -> ParsedInvoice:
        """Parse a single uploaded PDF file."""
        self.reference_data.ensure_loaded()
        raw_text = self.pdf_reader.extract_text(file_obj)
        parsed = self.invoice_parser.parse(raw_text)
        parsed.source_file = SourceFileInfo(
            name=getattr(file_obj, "name", ""),
            size_bytes=getattr(file_obj, "size", None),
            content_type=getattr(file_obj, "content_type", ""),
        )
        parsed.raw_text = raw_text[:4000] if raw_text else ""
        parsed.status = "Parsed successfully."
        self._enrich(parsed)
        return parsed

    def bulk_process(
        self,
        file_objs: list[BinaryIO],
        progress_callback=None,
        status_callback=None,
    ) -> BulkProcessingResult:
        """
        Parse many PDFs concurrently using a thread pool.

        Files are read into BytesIO buffers on the main thread first so Django's
        upload file objects are never accessed from worker threads.

        progress_callback(current, total, filename, status) is called after each
        file finishes ('ok' or 'error'). Used by the streaming view for SSE.
        status_callback(message) is called during setup phases that may take a
        while before the first file-level progress event is available.
        """
        self.reference_data.ensure_loaded()

        # Snapshot each file on the main thread before handing off.
        if status_callback:
            status_callback("Preparing uploaded PDF files...")
        snapshots = []
        total_files = len(file_objs)
        for index, f in enumerate(file_objs, start=1):
            if hasattr(f, "seek"):
                f.seek(0)
            snapshots.append((
                getattr(f, "name", ""),
                getattr(f, "size", None),
                getattr(f, "content_type", ""),
                BytesIO(f.read()),
            ))
            if status_callback:
                status_callback(f"Prepared {index} of {total_files} PDF files.")

        total = len(snapshots)
        invoices: list[ParsedInvoice] = []
        errors: list[dict] = []
        completed = 0

        def parse_one(snapshot: tuple) -> ParsedInvoice:
            name, size, content_type, buf = snapshot
            buf.seek(0)
            raw_text = self.pdf_reader.extract_text(buf)
            parsed = self.invoice_parser.parse(raw_text)
            parsed.source_file = SourceFileInfo(name=name, size_bytes=size, content_type=content_type)
            parsed.raw_text = raw_text[:4000] if raw_text else ""
            parsed.status = "Parsed successfully."
            self._enrich(parsed)
            return parsed

        if status_callback:
            status_callback(
                "Reading PDFs and generating GL suggestions. First run may take longer while the ML model loads."
            )

        with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(snapshots))) as pool:
            future_map = {pool.submit(parse_one, snap): snap[0] for snap in snapshots}
            for future in as_completed(future_map):
                filename = future_map[future]
                completed += 1
                try:
                    invoices.append(future.result())
                    if progress_callback:
                        progress_callback(completed, total, filename, "ok")
                except Exception as exc:
                    errors.append({"filename": filename, "error": str(exc)})
                    if progress_callback:
                        progress_callback(completed, total, filename, "error")

        return BulkProcessingResult(invoices=invoices, errors=errors)

    def _enrich(self, parsed: ParsedInvoice) -> None:
        """Add property validation and GL suggestions to a parsed invoice."""
        property_match = self.reference_data.match_property_code(
            parsed.property_code_normalized or parsed.property_code_raw
        )
        parsed.property_code_normalized = property_match.normalized_code
        parsed.property_code_validated = property_match.is_valid
        parsed.invoice_gl_description = self.reference_data.get_gl_description(
            parsed.invoice_gl_code
        )

        for item in parsed.line_items:
            item.invoice_gl_code_hint = parsed.invoice_gl_code
            suggestions = self.classifier.suggest(item, parsed.invoice_gl_code)
            item.suggestion_candidates = suggestions
            if suggestions:
                item.suggested_gl_code = suggestions[0].gl_code
                item.suggested_gl_description = suggestions[0].gl_description
                item.suggested_confidence = suggestions[0].confidence
                item.suggestion_reason = " ".join(suggestions[0].reasons)

            # Discounts, shipping, and fees always use the invoice-level GL when provided.
            # If no invoice GL is present, they inherit from the most expensive product
            # item after all items have been enriched (see below).
            if item.item_type in ("discount", "shipping", "fee"):
                if parsed.invoice_gl_code:
                    item.approved_gl_code = parsed.invoice_gl_code
                    item.approved_gl_description = parsed.invoice_gl_description

        # Second pass: non-product items on invoices without a GL code inherit the
        # suggested GL of the most expensive product line item on the same invoice.
        if not parsed.invoice_gl_code:
            most_expensive = max(
                (i for i in parsed.line_items if i.item_type == "product"),
                key=lambda i: i.line_total or 0,
                default=None,
            )
            if most_expensive and most_expensive.suggested_gl_code:
                for item in parsed.line_items:
                    if item.item_type in ("discount", "shipping", "fee"):
                        item.suggested_gl_code = most_expensive.suggested_gl_code
                        item.suggested_gl_description = most_expensive.suggested_gl_description
                        item.suggested_confidence = most_expensive.suggested_confidence
