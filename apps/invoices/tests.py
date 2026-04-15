from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from .models import GLAccount, Invoice, InvoiceLineItem, PropertyReference
from .schemas import GLSuggestion, InvoiceLineItem as ParsedLineItem, ParsedInvoice, SourceFileInfo
from .services.classification import LineItemGLClassifierService
from .services.invoice_parser import AmazonInvoiceParserService
from .services.reference_data import ReferenceDataSyncService
from .services.repository import InvoiceRepositoryService
from .services.yardi_submit import YardiSubmitService


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
        self.assertEqual(parsed_invoice.line_items[0].tax_rate, Decimal("7.250"))
        self.assertEqual(parsed_invoice.line_items[0].tax_amount, Decimal("8.70"))
        self.assertEqual(parsed_invoice.line_items[0].asin, "B08D39Z3CM")
        self.assertEqual(parsed_invoice.line_items[0].vendor, "Taiga Marketing, Inc")
        self.assertEqual(parsed_invoice.line_items[0].order_number, "114-8888272-4762604")

    def test_parse_rejects_non_invoice_pdf_text(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported invoice PDF"):
            AmazonInvoiceParserService().parse(
                "BASIC RULES\nD&D Basic Rules, Version 1.0, Released November 2018"
            )

    def test_parse_allows_missing_gl_code(self) -> None:
        raw_text = SAMPLE_INVOICE_TEXT.replace("Tax $ 8.70 GL code 6328\n", "Tax $ 8.70\n")

        parsed_invoice = AmazonInvoiceParserService().parse(raw_text)

        self.assertEqual(parsed_invoice.invoice_number, "1FD6-HNRM-7M69")
        self.assertEqual(parsed_invoice.invoice_gl_code, "")
        self.assertEqual(parsed_invoice.property_code_raw, "ssoh")
        self.assertEqual(len(parsed_invoice.line_items), 1)

    def test_parse_requires_property_code(self) -> None:
        raw_text = SAMPLE_INVOICE_TEXT.replace("Tax $ 8.70 GL code 6328\n", "Tax $ 8.70\n")
        raw_text = raw_text.replace("Property Code ssoh\n", "")

        with self.assertRaisesRegex(ValueError, "property code"):
            AmazonInvoiceParserService().parse(raw_text)

    def test_parse_rejects_thin_invoice_shaped_text(self) -> None:
        raw_text = """Invoice
Invoice # ABC-123
Property Code TEST
Invoice details
Item subtotal
Description Qty Unit price before tax Tax
1 Looks like a row but lacks supporting invoice context 1 $1.00 $1.00 0.000%
"""

        with self.assertRaisesRegex(ValueError, "no recognizable Amazon invoice metadata"):
            AmazonInvoiceParserService().parse(raw_text)


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

    def test_strong_winner_confidence_can_reach_upper_cap(self) -> None:
        classifier = LineItemGLClassifierService()
        line_item = ParsedLineItem(
            line_number=1,
            description="TRU RED Copy Paper",
            normalized_description="tru red copy paper",
        )

        with patch(
            "apps.invoices.services.classification.embedding_classifier.score_description_against_gl",
            return_value={"6332": 0.95, "6328": 0.05},
        ), patch(
            "apps.invoices.services.classification.embedding_classifier.score_against_approved_history",
            return_value={"6332": 2.00},
        ):
            suggestions = classifier.suggest(line_item, invoice_gl_code="")

        self.assertEqual(suggestions[0].gl_code, "6332")
        self.assertEqual(suggestions[0].confidence, 0.95)

    def test_confidence_values_spread_across_close_candidates(self) -> None:
        classifier = LineItemGLClassifierService()
        line_item = ParsedLineItem(
            line_number=1,
            description="Office supplies and recreation items",
            normalized_description="office supplies and recreation items",
        )

        with patch(
            "apps.invoices.services.classification.embedding_classifier.score_description_against_gl",
            return_value={"6734": 0.90, "6332": 0.70, "6328": 0.30},
        ), patch(
            "apps.invoices.services.classification.embedding_classifier.score_against_approved_history",
            return_value={},
        ):
            suggestions = classifier.suggest(line_item, invoice_gl_code="")

        confidences = [suggestion.confidence for suggestion in suggestions]
        self.assertEqual(len(set(confidences)), 3)
        self.assertGreater(confidences[0], confidences[1])
        self.assertGreater(confidences[1], confidences[2])


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
        self.assertContains(response, "Code Audit")
        self.assertContains(response, "<span class=\"code\">SSOH</span>", html=True)
        self.assertContains(response, "<span class=\"count\">2</span>", html=True)

    def test_property_audit_surfaces_missing_gl_codes(self) -> None:
        Invoice.objects.create(
            invoice_number="INV-GL-AUDIT",
            invoice_gl_code="6999",
            invoice_gl_description="UNMAPPED GL",
            property_code_raw="ssoh",
            property_code_normalized="SSOH",
        )

        response = self.client.get(reverse("invoices:property_audit"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "GL Code Audit")
        self.assertContains(response, "6999")
        self.assertContains(response, "UNMAPPED GL")

    def test_property_audit_can_create_gl_code(self) -> None:
        Invoice.objects.create(
            invoice_number="INV-GL-CREATE",
            invoice_gl_code="6999",
            invoice_gl_description="UNMAPPED GL",
            property_code_raw="ssoh",
            property_code_normalized="SSOH",
        )

        response = self.client.post(
            reverse("invoices:property_audit"),
            {
                "action": "create_gl_from_audit",
                "audit_gl-code": "6999",
                "audit_gl-description": "UNMAPPED GL",
                "audit_gl-in_review_range": "on",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(GLAccount.objects.filter(code="6999", description="UNMAPPED GL").exists())
        self.assertContains(response, "Added GL code 6999")

    def test_property_audit_can_create_property_and_link_invoices(self) -> None:
        invoice = InvoiceRepositoryService().save_parsed_invoices([
            self._build_parsed_invoice(
                invoice_number="INV-PROP-AUDIT",
                property_code_raw="zz99",
                property_code_normalized="ZZ99",
                property_code_validated=False,
            )
        ])[0]

        response = self.client.post(
            reverse("invoices:property_audit"),
            {
                "action": "create_property_from_audit",
                "audit_code": "ZZ99",
                "audit_property-code": "ZZ99",
                "audit_property-website_id": "9001",
                "audit_property-display_name": "Audit Created Property",
            },
            follow=True,
        )

        invoice.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(invoice.property_reference)
        self.assertEqual(invoice.property_reference.code, "ZZ99")
        self.assertContains(response, "Added property ZZ99")

    def test_property_audit_can_link_invoices_to_existing_property(self) -> None:
        target = PropertyReference.objects.create(
            code="LINKED",
            website_id="4444",
            display_name="Linked Property",
        )
        invoice = InvoiceRepositoryService().save_parsed_invoices([
            self._build_parsed_invoice(
                invoice_number="INV-PROP-LINK",
                property_code_raw="alias",
                property_code_normalized="ALIAS",
                property_code_validated=False,
            )
        ])[0]

        response = self.client.post(
            reverse("invoices:property_audit"),
            {
                "action": "assign_property_from_audit",
                "audit_code": "ALIAS",
                "property_reference_id": str(target.id),
            },
            follow=True,
        )

        invoice.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(invoice.property_reference_id, target.id)
        self.assertContains(response, "Linked 1 invoice")

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


class ClearDataCommandTests(TestCase):
    def setUp(self) -> None:
        ReferenceDataSyncService().sync_all()
        invoice_gl = GLAccount.objects.get(code="6328")
        suggested_gl = GLAccount.objects.get(code="6734")
        InvoiceRepositoryService().save_parsed_invoices([
            ParsedInvoice(
                source_file=SourceFileInfo(name="invoice.pdf", size_bytes=123),
                invoice_number="CLEAR-CMD-1",
                invoice_date=date(2026, 3, 31),
                invoice_gl_code=invoice_gl.code,
                invoice_gl_description=invoice_gl.description,
                property_code_raw="ssoh",
                property_code_normalized="SSOH",
                property_code_validated=True,
                line_items=[
                    ParsedLineItem(
                        line_number=1,
                        description="Tetherball Set",
                        normalized_description="tetherball set",
                        line_total=Decimal("119.99"),
                        suggested_gl_code=suggested_gl.code,
                        suggested_gl_description=suggested_gl.description,
                    )
                ],
            )
        ])

    def test_clear_invoices_preserves_reference_codes(self) -> None:
        out = StringIO()

        call_command("clear_data", "--yes", stdout=out)

        self.assertEqual(Invoice.objects.count(), 0)
        self.assertEqual(InvoiceLineItem.objects.count(), 0)
        self.assertGreater(GLAccount.objects.count(), 0)
        self.assertGreater(PropertyReference.objects.count(), 0)
        self.assertIn("Cleared 1 invoice(s)", out.getvalue())

    def test_clear_codes_only_preserves_invoice_data(self) -> None:
        out = StringIO()

        call_command("clear_data", "--yes", "--codes-only", stdout=out)

        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(InvoiceLineItem.objects.count(), 1)
        self.assertEqual(GLAccount.objects.count(), 0)
        self.assertEqual(PropertyReference.objects.count(), 0)
        self.assertIn("Cleared", out.getvalue())
        self.assertIn("GL account", out.getvalue())


class YardiSubmitServiceTests(TestCase):
    def test_audit_file_is_written_as_pdf(self) -> None:
        service = YardiSubmitService()
        entries = [
            {
                "property_yardi_code": "1234",
                "property_code": "SSOH",
                "gl_code": "6734",
                "gl_description": "POOL / REC SUPPLIES",
                "amount": Decimal("119.99"),
                "date": date(2026, 3, 31),
                "reference": "1FD6-HNRM-7M69",
            }
        ]

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "yardi_audit_test.pdf"
            service._write_audit(entries, date(2026, 4, 15), path)
            payload = path.read_bytes()

        self.assertTrue(payload.startswith(b"%PDF-1.4"))
        self.assertIn(b"Yardi Submission Audit", payload)
        self.assertIn(b"1FD6-HNRM-7M69", payload)
