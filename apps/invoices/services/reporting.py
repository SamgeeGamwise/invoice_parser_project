from collections import defaultdict
from decimal import Decimal

from django.db.models import Q

from ..models import Invoice, InvoiceLineItem


class ReportingService:
    def dashboard_stats(self) -> dict:
        invoices = Invoice.objects.count()
        line_items = InvoiceLineItem.objects.count()
        pending_review = InvoiceLineItem.objects.filter(
            item_type=InvoiceLineItem.ItemType.PRODUCT,
        ).filter(
            Q(approved_gl__isnull=True) | Q(invoice__property_reference__isnull=True)
        ).count()
        reviewed_items = InvoiceLineItem.objects.filter(
            approved_gl__isnull=False,
            invoice__property_reference__isnull=False,
        ).count()

        return {
            "invoice_count": invoices,
            "line_item_count": line_items,
            "pending_review_count": pending_review,
            "reviewed_item_count": reviewed_items,
        }

    def spend_by_gl(self) -> list[dict]:
        grouped: dict[tuple[str, str], dict] = {}
        queryset = InvoiceLineItem.objects.select_related("approved_gl", "suggested_gl", "invoice")

        for item in queryset:
            account = item.effective_gl
            if not account or item.line_total is None:
                continue
            key = (account.code, account.description)
            row = grouped.setdefault(
                key,
                {
                    "gl_code": account.code,
                    "gl_description": account.description,
                    "total_amount": Decimal("0.00"),
                    "line_item_count": 0,
                    "sample_items": [],
                },
            )
            row["total_amount"] += item.line_total
            row["line_item_count"] += 1
            if len(row["sample_items"]) < 3:
                row["sample_items"].append(item.description)

        return sorted(grouped.values(), key=lambda row: row["total_amount"], reverse=True)

    def items_by_property(self) -> list[dict]:
        grouped: dict[str, dict] = {}
        queryset = InvoiceLineItem.objects.select_related("invoice")

        for item in queryset:
            property_code = item.invoice.property_code_normalized or "UNKNOWN"
            row = grouped.setdefault(
                property_code,
                {
                    "property_code": property_code,
                    "total_amount": Decimal("0.00"),
                    "line_item_count": 0,
                    "sample_items": [],
                },
            )
            row["line_item_count"] += 1
            row["total_amount"] += item.line_total or Decimal("0.00")
            if len(row["sample_items"]) < 3:
                row["sample_items"].append(item.description)

        return sorted(grouped.values(), key=lambda row: row["total_amount"], reverse=True)
