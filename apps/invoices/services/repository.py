from django.db import transaction
from django.utils import timezone

from ..models import GLAccount, Invoice, InvoiceLineItem
from ..schemas import ParsedInvoice
from .reference_data import ReferenceDataSyncService


class InvoiceRepositoryService:
    def __init__(self, reference_data: ReferenceDataSyncService | None = None) -> None:
        self.reference_data = reference_data or ReferenceDataSyncService()

    @transaction.atomic
    def save_parsed_invoices(
        self,
        parsed_invoices: list[ParsedInvoice],
        upload_batch_id: str = "",
    ) -> list[Invoice]:
        saved_invoices: list[Invoice] = []

        for parsed_invoice in parsed_invoices:
            # Warn if re-uploading an invoice that already has human approvals.
            # update_or_create will overwrite the invoice and clear line items below,
            # which would destroy approval work.  Set status so the caller can surface it.
            existing = Invoice.objects.filter(
                invoice_number=parsed_invoice.invoice_number
            ).first()
            if existing:
                has_approvals = existing.line_items.filter(
                    approved_gl__isnull=False
                ).exists()
                if has_approvals:
                    parsed_invoice.status = (
                        f"Re-uploaded (previously had approvals — they have been reset). "
                        f"Original: {parsed_invoice.status}"
                    )

            property_match = self.reference_data.match_property_code(
                parsed_invoice.property_code_normalized or parsed_invoice.property_code_raw
            )
            invoice, _created = Invoice.objects.update_or_create(
                invoice_number=parsed_invoice.invoice_number,
                defaults={
                    "source_file_name": parsed_invoice.source_file.name,
                    "source_file_size": parsed_invoice.source_file.size_bytes,
                    "source_content_type": parsed_invoice.source_file.content_type,
                    "invoice_date": parsed_invoice.invoice_date,
                    "purchase_date": parsed_invoice.purchase_date,
                    "purchaser": parsed_invoice.purchaser,
                    "po_number": parsed_invoice.po_number,
                    "invoice_gl_code": parsed_invoice.invoice_gl_code,
                    "invoice_gl_description": parsed_invoice.invoice_gl_description,
                    "property_code_raw": parsed_invoice.property_code_raw,
                    "property_code_normalized": property_match.normalized_code,
                    "property_reference": property_match.property_reference,
                    "subtotal": parsed_invoice.subtotal,
                    "tax_total": parsed_invoice.tax_total,
                    "grand_total": parsed_invoice.grand_total,
                    "raw_text": parsed_invoice.raw_text,
                    "status": parsed_invoice.status,
                    "upload_batch_id": upload_batch_id,
                },
            )

            invoice.line_items.all().delete()

            for parsed_line_item in parsed_invoice.line_items:
                suggested_gl = None
                approved_gl = None
                if parsed_line_item.suggested_gl_code:
                    suggested_gl = GLAccount.objects.filter(code=parsed_line_item.suggested_gl_code).first()
                if parsed_line_item.approved_gl_code:
                    approved_gl = GLAccount.objects.filter(code=parsed_line_item.approved_gl_code).first()

                InvoiceLineItem.objects.create(
                    invoice=invoice,
                    line_number=parsed_line_item.line_number,
                    item_type=parsed_line_item.item_type,
                    description=parsed_line_item.description,
                    normalized_description=parsed_line_item.normalized_description,
                    quantity=parsed_line_item.quantity,
                    unit_price=parsed_line_item.unit_price,
                    line_total=parsed_line_item.line_total,
                    tax_rate=parsed_line_item.tax_rate,
                    tax_amount=parsed_line_item.tax_amount,
                    asin=parsed_line_item.asin,
                    vendor=parsed_line_item.vendor,
                    order_number=parsed_line_item.order_number,
                    invoice_gl_code_hint=parsed_line_item.invoice_gl_code_hint,
                    suggested_gl=suggested_gl,
                    suggested_confidence=parsed_line_item.suggested_confidence or None,
                    suggestion_reason=parsed_line_item.suggestion_reason,
                    suggestion_candidates=[
                        candidate.to_dict()
                        for candidate in parsed_line_item.suggestion_candidates
                    ],
                    approved_gl=approved_gl,
                )

            # Auto-approve: single product line item + invoice has a GL code.
            # The invoice already told us the answer — no review needed.
            if invoice.invoice_gl_code and invoice.property_reference_id:
                product_items = list(
                    InvoiceLineItem.objects.filter(
                        invoice=invoice,
                        item_type=InvoiceLineItem.ItemType.PRODUCT,
                    )
                )
                if len(product_items) == 1:
                    gl = GLAccount.objects.filter(code=invoice.invoice_gl_code).first()
                    if gl:
                        item = product_items[0]
                        item.approved_gl = gl
                        item.reviewed_at = timezone.now()
                        item.save(update_fields=["approved_gl", "reviewed_at", "updated_at"])

            saved_invoices.append(invoice)

        return saved_invoices
