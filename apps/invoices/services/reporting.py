from decimal import Decimal

from django.db.models import Q, Sum

from ..models import Invoice, InvoiceLineItem


class ReportingService:
    def dashboard_stats(self) -> dict:
        invoices = Invoice.objects.count()
        line_items = InvoiceLineItem.objects.count()
        pending_approval = InvoiceLineItem.objects.filter(
            item_type=InvoiceLineItem.ItemType.PRODUCT,
        ).filter(
            Q(approved_gl__isnull=True) | Q(invoice__property_reference__isnull=True)
        ).count()
        approved_items = InvoiceLineItem.objects.filter(
            item_type=InvoiceLineItem.ItemType.PRODUCT,
            approved_gl__isnull=False,
            invoice__property_reference__isnull=False,
        ).count()
        total_amount = Invoice.objects.aggregate(t=Sum("grand_total"))["t"] or Decimal("0.00")
        ready_to_submit = line_items - pending_approval

        return {
            "invoice_count": invoices,
            "line_item_count": line_items,
            "pending_review_count": pending_approval,
            "approved_count": approved_items,
            "ready_to_submit": ready_to_submit,
            "total_amount": total_amount,
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

    # ── CSV export helpers ───────────────────────────────────────────────────

    def _base_queryset(self):
        return (
            InvoiceLineItem.objects
            .select_related("invoice", "invoice__property_reference", "approved_gl", "suggested_gl")
            .order_by("invoice__invoice_number", "line_number")
        )

    def _item_row(self, item: InvoiceLineItem) -> dict:
        gl = item.effective_gl
        line_total = item.line_total or Decimal("0.00")
        tax = item.tax_amount or Decimal("0.00")
        return {
            "invoice_number": item.invoice.invoice_number,
            "invoice_date": item.invoice.invoice_date or "",
            "property_code": item.invoice.property_code_normalized or "",
            "line_number": item.line_number,
            "item_type": item.item_type,
            "description": item.description,
            "quantity": item.quantity or "",
            "subtotal": line_total,
            "tax_rate": item.tax_rate or Decimal("0"),
            "tax_amount": tax,
            "total": line_total + tax,
            "gl_code": gl.code if gl else "",
            "gl_description": gl.description if gl else "",
            "gl_source": "approved" if item.approved_gl_id else ("suggested" if item.suggested_gl_id else ""),
        }

    def line_item_detail(self) -> list[dict]:
        """Every line item with its effective GL classification."""
        return [self._item_row(item) for item in self._base_queryset()]

    def gl_spend_summary(self) -> list[dict]:
        """Total spend grouped by GL code, tax-inclusive."""
        grouped: dict[str, dict] = {}
        for item in self._base_queryset():
            gl = item.effective_gl
            if not gl:
                continue
            line_total = item.line_total or Decimal("0.00")
            tax = item.tax_amount or Decimal("0.00")
            row = grouped.setdefault(gl.code, {
                "gl_code": gl.code,
                "gl_description": gl.description,
                "line_item_count": 0,
                "total": Decimal("0.00"),
            })
            row["line_item_count"] += 1
            row["total"] += line_total + tax
        return sorted(grouped.values(), key=lambda r: r["total"], reverse=True)

    def items_by_gl_detail(self) -> list[dict]:
        """All line items sorted by GL code, then invoice."""
        rows = [self._item_row(item) for item in self._base_queryset()]
        return sorted(rows, key=lambda r: (r["gl_code"] or "zzz", r["invoice_number"], r["line_number"]))

    def items_by_property_detail(self) -> list[dict]:
        """All line items sorted by property code, then invoice."""
        rows = [self._item_row(item) for item in self._base_queryset()]
        return sorted(rows, key=lambda r: (r["property_code"] or "zzz", r["invoice_number"], r["line_number"]))
