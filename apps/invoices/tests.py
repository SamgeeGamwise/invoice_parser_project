from datetime import date
from decimal import Decimal
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
        self.assertTrue(PropertyReference.objects.filter(code="SSOH").exists())
        self.assertTrue(property_match.is_valid)
        self.assertEqual(property_match.normalized_code, "SSOH")

    def test_sync_can_store_property_display_name_when_present(self) -> None:
        class StubSpreadsheetReader:
            def read_rows(self, path):
                if "Property List.xlsx" in str(path):
                    return [
                        ["Website ID", "Yardi Code", "Display Name"],
                        ["312", "bwoh", "Briarwood Oaks"],
                    ]
                return [
                    ["scode", "sdesc"],
                    ["6328", "OFFICE EQUIPMENT PURCHASES"],
                ]

        service = ReferenceDataSyncService(spreadsheet_reader=StubSpreadsheetReader())
        service.sync_all(force=True)

        prop = PropertyReference.objects.get(code="BWOH")
        self.assertEqual(prop.display_name, "Briarwood Oaks")


class LineItemGLClassifierServiceTests(TestCase):
    def setUp(self) -> None:
        GLAccount.objects.bulk_create([
            GLAccount(code="6328", description="OFFICE EQUIPMENT PURCHASES", in_review_range=True),
            GLAccount(code="6332", description="OFFICE SUPPLIES", in_review_range=True),
            GLAccount(code="6734", description="POOL / REC SUPPLIES", in_review_range=True),
        ])

    def test_suggest_prefers_highest_embedding_score(self) -> None:
        classifier = LineItemGLClassifierService()
        line_item = ParsedLineItem(
            line_number=1,
            description="STERLING Sunnywood Sports Heavy Duty Tetherball Set",
            normalized_description="sterling sunnywood sports heavy duty tetherball set",
        )

        with patch(
            "apps.invoices.services.classification.embedding_classifier.score_description_against_gl",
            return_value={"6734": 0.95, "6328": 0.30},
        ), patch(
            "apps.invoices.services.classification.embedding_classifier.score_against_approved_history",
            return_value={},
        ):
            suggestions = classifier.suggest(line_item, invoice_gl_code="")

        self.assertEqual(suggestions[0].gl_code, "6734")
        self.assertTrue(any("Semantic match" in reason for reason in suggestions[0].reasons))

    def test_history_vote_can_overcome_invoice_prior(self) -> None:
        classifier = LineItemGLClassifierService()
        line_item = ParsedLineItem(
            line_number=1,
            description="TRU RED Copy Paper",
            normalized_description="tru red copy paper",
        )

        with patch(
            "apps.invoices.services.classification.embedding_classifier.score_description_against_gl",
            return_value={"6328": 0.15, "6332": 0.10},
        ), patch(
            "apps.invoices.services.classification.embedding_classifier.score_against_approved_history",
            return_value={"6332": 1.40},
        ):
            suggestions = classifier.suggest(line_item, invoice_gl_code="6328")

        self.assertEqual(suggestions[0].gl_code, "6332")
        self.assertTrue(any("KNN vote" in reason for reason in suggestions[0].reasons))


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
        self.assertEqual(saved_invoice.property_reference.code, "SSOH")
        self.assertEqual(saved_invoice.line_items.count(), 1)
        self.assertEqual(saved_invoice.line_items.first().suggested_gl.code, "6734")

    def test_single_line_auto_approve_requires_validated_property(self) -> None:
        invoice_gl = GLAccount.objects.get(code="6328")
        parsed_invoice = ParsedInvoice(
            source_file=SourceFileInfo(name="invoice.pdf", size_bytes=123),
            invoice_number="INV-NO-PROP",
            invoice_date=date(2026, 3, 31),
            purchaser="Deanna Yost",
            invoice_gl_code=invoice_gl.code,
            invoice_gl_description=invoice_gl.description,
            property_code_raw="unknown",
            property_code_normalized="UNKNOWN",
            property_code_validated=False,
            subtotal=Decimal("25.00"),
            tax_total=Decimal("0.00"),
            grand_total=Decimal("25.00"),
            line_items=[
                ParsedLineItem(
                    line_number=1,
                    description="Paper clips",
                    normalized_description="paper clips",
                    line_total=Decimal("25.00"),
                    suggested_gl_code=invoice_gl.code,
                    suggested_gl_description=invoice_gl.description,
                )
            ],
        )

        saved_invoice = InvoiceRepositoryService().save_parsed_invoices([parsed_invoice])[0]

        self.assertIsNone(saved_invoice.property_reference)
        self.assertIsNone(saved_invoice.line_items.first().approved_gl)


class DashboardViewTests(TestCase):
    def setUp(self) -> None:
        ReferenceDataSyncService().sync_all()

    def _build_parsed_invoice(
        self,
        *,
        invoice_number: str = "1FD6-HNRM-7M69",
        property_code_raw: str = "ssoh",
        property_code_normalized: str = "SSOH",
        property_code_validated: bool = True,
    ) -> ParsedInvoice:
        invoice_gl = GLAccount.objects.get(code="6328")
        suggested_gl = GLAccount.objects.get(code="6734")
        return ParsedInvoice(
            source_file=SourceFileInfo(name="invoice.pdf", size_bytes=123, content_type="application/pdf"),
            invoice_number=invoice_number,
            invoice_date=date(2026, 3, 31),
            purchase_date=date(2026, 3, 20),
            purchaser="Deanna Yost",
            po_number="1085654",
            invoice_gl_code=invoice_gl.code,
            invoice_gl_description=invoice_gl.description,
            property_code_raw=property_code_raw,
            property_code_normalized=property_code_normalized,
            property_code_validated=property_code_validated,
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

    def test_bulk_upload_get_renders_form(self) -> None:
        response = self.client.get(reverse("invoices:bulk_upload"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Upload Invoices")

    def test_invoice_detail_post_saves_review_override(self) -> None:
        invoice = InvoiceRepositoryService().save_parsed_invoices([self._build_parsed_invoice()])[0]
        approved_gl = GLAccount.objects.get(code="6328")
        line_item = invoice.line_items.first()

        response = self.client.post(
            reverse("invoices:invoice_detail", args=[invoice.id]),
            {f"item_{line_item.id}_gl": approved_gl.code},
        )

        self.assertRedirects(response, reverse("invoices:invoice_detail", args=[invoice.id]))
        line_item.refresh_from_db()
        self.assertEqual(line_item.approved_gl.code, "6328")

    def test_invoice_detail_post_blocks_approval_without_validated_property(self) -> None:
        invoice = InvoiceRepositoryService().save_parsed_invoices([
            self._build_parsed_invoice(
                invoice_number="INV-BLOCKED",
                property_code_raw="unknown",
                property_code_normalized="UNKNOWN",
                property_code_validated=False,
            )
        ])[0]
        approved_gl = GLAccount.objects.get(code="6328")
        line_item = invoice.line_items.first()

        response = self.client.post(
            reverse("invoices:invoice_detail", args=[invoice.id]),
            {
                f"item_{line_item.id}_gl": approved_gl.code,
                f"item_{line_item.id}_notes": "Tried to approve.",
            },
            follow=True,
        )

        line_item.refresh_from_db()
        self.assertIsNone(line_item.approved_gl)
        self.assertContains(response, "validated property code")

    def test_review_queue_post_skips_flagged_invoices(self) -> None:
        valid_invoice = InvoiceRepositoryService().save_parsed_invoices([
            self._build_parsed_invoice(invoice_number="INV-VALID")
        ])[0]
        invalid_invoice = InvoiceRepositoryService().save_parsed_invoices([
            self._build_parsed_invoice(
                invoice_number="INV-FLAGGED",
                property_code_raw="unknown",
                property_code_normalized="UNKNOWN",
                property_code_validated=False,
            )
        ])[0]

        valid_item = valid_invoice.line_items.first()
        invalid_item = invalid_invoice.line_items.first()
        approved_gl = GLAccount.objects.get(code="6328")

        response = self.client.post(
            reverse("invoices:review_queue"),
            {
                "item_ids": f"{valid_item.id},{invalid_item.id}",
                "page": "1",
                f"item_{valid_item.id}_gl": approved_gl.code,
                f"item_{invalid_item.id}_gl": approved_gl.code,
            },
            follow=True,
        )

        valid_item.refresh_from_db()
        invalid_item.refresh_from_db()
        self.assertEqual(valid_item.approved_gl.code, approved_gl.code)
        self.assertIsNone(invalid_item.approved_gl)
        self.assertContains(response, "Flagged invoice")

    def test_review_queue_page_contains_bulk_save_confirmation_modal(self) -> None:
        InvoiceRepositoryService().save_parsed_invoices([
            self._build_parsed_invoice(
                invoice_number="INV-MODAL",
                property_code_raw="unknown",
                property_code_normalized="UNKNOWN",
                property_code_validated=False,
            )
        ])

        response = self.client.get(reverse("invoices:review_queue"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Confirm bulk save")

    def test_property_audit_groups_property_codes_case_insensitively(self) -> None:
        Invoice.objects.create(
            invoice_number="INV-CASE-1",
            property_code_raw="ssoh",
            property_code_normalized="SSOH",
        )
        Invoice.objects.create(
            invoice_number="INV-CASE-2",
            property_code_raw="SSOH",
            property_code_normalized="SSOH",
        )

        response = self.client.get(reverse("invoices:property_audit"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<span class=\"code\">SSOH</span>", html=True)
        self.assertContains(response, "<span class=\"count\">2</span>", html=True)

    def test_ajax_approve_rejects_missing_property(self) -> None:
        invoice = InvoiceRepositoryService().save_parsed_invoices([
            self._build_parsed_invoice(
                invoice_number="INV-AJAX-BLOCKED",
                property_code_raw="unknown",
                property_code_normalized="UNKNOWN",
                property_code_validated=False,
            )
        ])[0]
        line_item = invoice.line_items.first()
        approved_gl = GLAccount.objects.get(code="6328")

        response = self.client.post(
            reverse("invoices:approve_item", args=[line_item.id]),
            {"gl_code": approved_gl.code},
        )

        line_item.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(line_item.approved_gl)
        self.assertIn("validated", response.json()["error"])

    def test_reports_view_renders_gl_and_property_aggregates(self) -> None:
        invoice = InvoiceRepositoryService().save_parsed_invoices([self._build_parsed_invoice()])[0]
        line_item = invoice.line_items.first()
        line_item.approved_gl = GLAccount.objects.get(code="6734")
        line_item.save(update_fields=["approved_gl", "updated_at"])

        response = self.client.get(reverse("invoices:reports"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Spend by GL Code")
        self.assertContains(response, "6734 &mdash; POOL / REC SUPPLIES", html=True)
        self.assertContains(response, "SSOH")


class ReferenceDataViewTests(TestCase):
    def setUp(self) -> None:
        ReferenceDataSyncService().sync_all()

    def test_reference_data_view_renders(self) -> None:
        response = self.client.get(reverse("invoices:reference_data"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reference Data")
        self.assertContains(response, "GL Accounts")
        self.assertContains(response, "Property References")

    def test_reference_data_view_can_create_gl_account(self) -> None:
        response = self.client.post(
            reverse("invoices:reference_data"),
            {
                "action": "save_gl",
                "gl-code": "6999",
                "gl-description": "TEST GL ACCOUNT",
                "gl-in_review_range": "on",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(GLAccount.objects.filter(code="6999", description="TEST GL ACCOUNT").exists())

    def test_reference_data_view_can_create_property_reference(self) -> None:
        response = self.client.post(
            reverse("invoices:reference_data"),
            {
                "action": "save_property",
                "property-code": "test-prop",
                "property-website_id": "web-1",
                "property-display_name": "Test Property",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            PropertyReference.objects.filter(
                code="TEST-PROP",
                display_name="Test Property",
            ).exists()
        )


class RuntimeReferenceDataGuardTests(TestCase):
    def test_bulk_upload_post_shows_error_when_reference_data_not_loaded(self) -> None:
        response = self.client.post(
            reverse("invoices:bulk_upload"),
            {
                "invoice_pdfs": SimpleUploadedFile(
                    "invoice.pdf",
                    b"pdf-bytes",
                    content_type="application/pdf",
                )
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "GL codes and/or property references have not been loaded")
