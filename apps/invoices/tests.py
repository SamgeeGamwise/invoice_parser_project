from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import GLAccount, Invoice, InvoiceLineItem, PropertyReference
from .schemas import GLSuggestion, InvoiceLineItem as ParsedLineItem, ParsedInvoice, SourceFileInfo
from .services.classification import LineItemGLClassifierService
from .services.invoice_parser import AmazonInvoiceParserService
from .services.reference_data import ReferenceDataSyncService
from .services.repository import InvoiceRepositoryService


SAMPLE_INVOICE_TEXT = """Invoice
Invoice # 1FD6-HNRM-7M69 | March 31, 2026
Invoice summary Payment due by April 30, 2026 Account # AAHC19ZAFILC4
Payment terms Net 30
Item subtotal before tax $119.99
Shipping & handling $ 0.00 Purchase date 20-Mar-2026
Promos & discounts $ 0.00
Purchased by Deanna Yost
Total before tax $119.99 PO # 1085654
Tax $ 8.70 GL code 6328
Property Code ssoh
Amount due $128.69 USD
Invoice details
Item subtotal
Description Qty Unit price before tax Tax
1 STERLING Sunnywood Sports Heavy Duty Tetherball Set for 1 $119.99 $119.99 7.250%
Outdoor Backyard with Ball, Rope and Pole 10-1/2' Height
ASIN:
B08D39Z3CM
Sold by: Taiga Marketing, Inc
Order # 114-8888272-4762604
Total before tax $119.99
Tax $8.70
Amount due $128.69
"""


class AmazonInvoiceParserServiceTests(TestCase):
    def test_parse_extracts_invoice_metadata_and_line_items(self) -> None:
        parsed_invoice = AmazonInvoiceParserService().parse(SAMPLE_INVOICE_TEXT)

        self.assertEqual(parsed_invoice.invoice_number, "1FD6-HNRM-7M69")
        self.assertEqual(parsed_invoice.invoice_date, date(2026, 3, 31))
        self.assertEqual(parsed_invoice.purchase_date, date(2026, 3, 20))
        self.assertEqual(parsed_invoice.purchaser, "Deanna Yost")
        self.assertEqual(parsed_invoice.po_number, "1085654")
        self.assertEqual(parsed_invoice.invoice_gl_code, "6328")
        self.assertEqual(parsed_invoice.property_code_raw, "ssoh")
        self.assertEqual(parsed_invoice.property_code_normalized, "SSOH")
        self.assertEqual(parsed_invoice.subtotal, Decimal("119.99"))
        self.assertEqual(parsed_invoice.tax_total, Decimal("8.70"))
        self.assertEqual(parsed_invoice.grand_total, Decimal("128.69"))
        self.assertEqual(len(parsed_invoice.line_items), 1)
        self.assertIn("Tetherball", parsed_invoice.line_items[0].description)
        self.assertEqual(parsed_invoice.line_items[0].asin, "B08D39Z3CM")
        self.assertEqual(parsed_invoice.line_items[0].vendor, "Taiga Marketing, Inc")
        self.assertEqual(parsed_invoice.line_items[0].order_number, "114-8888272-4762604")


class ReferenceDataSyncServiceTests(TestCase):
    def test_sync_populates_gl_and_property_reference_tables(self) -> None:
        service = ReferenceDataSyncService()

        service.sync_all()
        property_match = service.match_property_code("ssoh")

        self.assertTrue(GLAccount.objects.filter(code="6328").exists())
        self.assertTrue(GLAccount.objects.filter(code="6734").exists())
        self.assertTrue(PropertyReference.objects.filter(normalized_code="SSOH").exists())
        self.assertTrue(property_match.is_valid)
        self.assertEqual(property_match.normalized_code, "SSOH")


class LineItemGLClassifierServiceTests(TestCase):
    def setUp(self) -> None:
        GLAccount.objects.bulk_create([
            GLAccount(code="6328", description="OFFICE EQUIPMENT PURCHASES", in_review_range=True),
            GLAccount(code="6332", description="OFFICE SUPPLIES", in_review_range=True),
            GLAccount(code="6734", description="POOL / REC SUPPLIES", in_review_range=True),
        ])

    def test_suggest_prefers_keyword_based_gl_for_recreation_item(self) -> None:
        classifier = LineItemGLClassifierService()
        line_item = ParsedLineItem(
            line_number=1,
            description="STERLING Sunnywood Sports Heavy Duty Tetherball Set",
            normalized_description="sterling sunnywood sports heavy duty tetherball set",
        )

        suggestions = classifier.suggest(line_item, invoice_gl_code="6328")

        self.assertEqual(suggestions[0].gl_code, "6734")
        self.assertTrue(any("Keyword match" in reason for reason in suggestions[0].reasons))

    def test_history_strengthens_repeated_approved_descriptions(self) -> None:
        approved_gl = GLAccount.objects.get(code="6332")
        invoice = Invoice.objects.create(invoice_number="INV-001")
        InvoiceLineItem.objects.create(
            invoice=invoice,
            line_number=1,
            item_type=InvoiceLineItem.ItemType.PRODUCT,
            description="TRU RED Copy Paper",
            normalized_description="tru red copy paper",
            line_total=Decimal("10.00"),
            approved_gl=approved_gl,
        )

        classifier = LineItemGLClassifierService()
        line_item = ParsedLineItem(
            line_number=1,
            description="TRU RED Copy Paper",
            normalized_description="tru red copy paper",
        )

        suggestions = classifier.suggest(line_item, invoice_gl_code="6328")

        self.assertEqual(suggestions[0].gl_code, "6332")
        self.assertTrue(any("Exact match" in reason for reason in suggestions[0].reasons))


class InvoiceRepositoryServiceTests(TestCase):
    def setUp(self) -> None:
        self.reference_data = ReferenceDataSyncService()
        self.reference_data.sync_all()

    def test_save_parsed_invoices_persists_invoice_and_line_items(self) -> None:
        invoice_gl = GLAccount.objects.get(code="6328")
        suggested_gl = GLAccount.objects.get(code="6734")
        parsed_invoice = ParsedInvoice(
            source_file=SourceFileInfo(name="invoice.pdf", size_bytes=123),
            invoice_number="1FD6-HNRM-7M69",
            invoice_date=date(2026, 3, 31),
            purchase_date=date(2026, 3, 20),
            purchaser="Deanna Yost",
            po_number="1085654",
            invoice_gl_code=invoice_gl.code,
            invoice_gl_description=invoice_gl.description,
            property_code_raw="ssoh",
            property_code_normalized="SSOH",
            property_code_validated=True,
            subtotal=Decimal("119.99"),
            tax_total=Decimal("8.70"),
            grand_total=Decimal("128.69"),
            line_items=[
                ParsedLineItem(
                    line_number=1,
                    description="Tetherball Set",
                    normalized_description="tetherball set",
                    line_total=Decimal("119.99"),
                    suggested_gl_code=suggested_gl.code,
                    suggested_gl_description=suggested_gl.description,
                    suggested_confidence=0.88,
                    suggestion_reason="Keyword match for tetherball.",
                    suggestion_candidates=[
                        GLSuggestion(
                            gl_code=suggested_gl.code,
                            gl_description=suggested_gl.description,
                            score=8.0,
                            confidence=0.88,
                            reasons=["Keyword match for tetherball."],
                        )
                    ],
                )
            ],
            status="Parsed and classified.",
        )

        saved_invoice = InvoiceRepositoryService().save_parsed_invoices([parsed_invoice], upload_batch_id="job-1")[0]

        self.assertEqual(saved_invoice.property_code_normalized, "SSOH")
        self.assertEqual(saved_invoice.property_reference.normalized_code, "SSOH")
        self.assertEqual(saved_invoice.line_items.count(), 1)
        self.assertEqual(saved_invoice.line_items.first().suggested_gl.code, "6734")


class DashboardViewTests(TestCase):
    def setUp(self) -> None:
        ReferenceDataSyncService().sync_all()

    def _build_parsed_invoice(self) -> ParsedInvoice:
        invoice_gl = GLAccount.objects.get(code="6328")
        suggested_gl = GLAccount.objects.get(code="6734")
        return ParsedInvoice(
            source_file=SourceFileInfo(name="invoice.pdf", size_bytes=123, content_type="application/pdf"),
            invoice_number="1FD6-HNRM-7M69",
            invoice_date=date(2026, 3, 31),
            purchase_date=date(2026, 3, 20),
            purchaser="Deanna Yost",
            po_number="1085654",
            invoice_gl_code=invoice_gl.code,
            invoice_gl_description=invoice_gl.description,
            property_code_raw="ssoh",
            property_code_normalized="SSOH",
            property_code_validated=True,
            subtotal=Decimal("119.99"),
            tax_total=Decimal("8.70"),
            grand_total=Decimal("128.69"),
            line_items=[
                ParsedLineItem(
                    line_number=1,
                    description="Tetherball Set",
                    normalized_description="tetherball set",
                    quantity=1,
                    unit_price=Decimal("119.99"),
                    line_total=Decimal("119.99"),
                    suggested_gl_code=suggested_gl.code,
                    suggested_gl_description=suggested_gl.description,
                    suggested_confidence=0.88,
                    suggestion_reason="Keyword match for tetherball.",
                    suggestion_candidates=[
                        GLSuggestion(
                            gl_code=suggested_gl.code,
                            gl_description=suggested_gl.description,
                            score=8.0,
                            confidence=0.88,
                            reasons=["Keyword match for tetherball."],
                        )
                    ],
                )
            ],
            status="Parsed and classified.",
        )

    def test_dashboard_post_saves_invoice_and_redirects_to_review(self) -> None:
        with patch(
            "apps.invoices.views.InvoiceProcessingService.process",
            return_value=self._build_parsed_invoice(),
        ):
            response = self.client.post(
                reverse("invoices:dashboard"),
                {
                    "invoice_pdf": SimpleUploadedFile(
                        "invoice.pdf",
                        b"pdf-bytes",
                        content_type="application/pdf",
                    )
                },
            )

        saved_invoice = Invoice.objects.get(invoice_number="1FD6-HNRM-7M69")
        self.assertRedirects(response, reverse("invoices:invoice_detail", args=[saved_invoice.id]))

    def test_bulk_upload_post_renders_progress_page(self) -> None:
        with patch(
            "apps.invoices.views.BulkUploadJobService.start_job",
        ) as start_job_mock:
            start_job_mock.return_value.job_id = "job-123"
            response = self.client.post(
                reverse("invoices:bulk_upload"),
                {
                    "invoice_pdfs": [
                        SimpleUploadedFile("invoice-1.pdf", b"one", content_type="application/pdf"),
                        SimpleUploadedFile("invoice-2.pdf", b"two", content_type="application/pdf"),
                    ]
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bulk Upload Progress")

    def test_invoice_detail_post_saves_review_override(self) -> None:
        invoice = InvoiceRepositoryService().save_parsed_invoices([self._build_parsed_invoice()])[0]
        approved_gl = GLAccount.objects.get(code="6328")
        line_item = invoice.line_items.first()

        response = self.client.post(
            reverse("invoices:invoice_detail", args=[invoice.id]),
            {
                f"line_item_{line_item.id}_gl": approved_gl.code,
                f"line_item_{line_item.id}_notes": "Reviewed and moved to office equipment.",
            },
        )

        self.assertRedirects(response, reverse("invoices:invoice_detail", args=[invoice.id]))
        line_item.refresh_from_db()
        self.assertEqual(line_item.approved_gl.code, "6328")
        self.assertEqual(line_item.approval_notes, "Reviewed and moved to office equipment.")

    def test_reports_view_renders_gl_and_property_aggregates(self) -> None:
        invoice = InvoiceRepositoryService().save_parsed_invoices([self._build_parsed_invoice()])[0]
        line_item = invoice.line_items.first()
        line_item.approved_gl = GLAccount.objects.get(code="6734")
        line_item.save(update_fields=["approved_gl", "updated_at"])

        response = self.client.get(reverse("invoices:reports"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Spend By GL")
        self.assertContains(response, "6734 - POOL / REC SUPPLIES")
        self.assertContains(response, "SSOH")
