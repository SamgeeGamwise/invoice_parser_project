import re
from datetime import datetime
from decimal import Decimal, ROUND_DOWN

from ..schemas import InvoiceLineItem, ParsedInvoice


class AmazonInvoiceParserService:
    """
    Parse Amazon invoice raw text into invoice metadata plus line items.

    The parser is intentionally heuristic and review-friendly. It focuses on
    preserving the invoice-level GL hint while extracting enough line-item
    detail to support downstream GL suggestions and human approval.
    """

    INVOICE_NUMBER_PATTERN = re.compile(r"Invoice #\s+([A-Z0-9-]+)")
    INVOICE_DATE_PATTERN = re.compile(r"Invoice #\s+[A-Z0-9-]+\s+\|\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})")
    PURCHASE_DATE_PATTERN = re.compile(r"Purchase date\s+(\d{2}-[A-Za-z]{3}-\d{4})")
    PURCHASER_PATTERN = re.compile(r"Purchased by\s+(.+)")
    PO_NUMBER_PATTERN = re.compile(r"PO #\s+(.+)")
    PROPERTY_CODE_PATTERN = re.compile(r"Property Code\s+([A-Za-z0-9_-]+)")
    GL_CODE_PATTERN = re.compile(r"GL code\s+([A-Za-z0-9_-]+)")
    SUBTOTAL_PATTERN = re.compile(r"Total before tax\s+\$?\s*([\d,]+\.\d{2})")
    TAX_TOTAL_PATTERN = re.compile(r"Tax\s+\$?\s*([\d,]+\.\d{2})")
    GRAND_TOTAL_PATTERN = re.compile(r"Amount due\s+\$?\s*([\d,]+\.\d{2})")
    PRODUCT_LINE_PATTERN = re.compile(
        r"^(?P<line_number>\d+)\s+"
        r"(?P<description>.+?)\s+"
        r"(?P<quantity>\d+)\s+"
        r"\$(?P<unit_price>[\d,]+\.\d{2})\s+"
        r"\$(?P<line_total>[\d,]+\.\d{2})\s+"
        r"(?P<tax_rate>[\d.]+%)$"
    )
    DISCOUNT_LINE_PATTERN = re.compile(
        r"^(?P<line_number>\d+)\s+"
        r"(?P<description>Promotions\s*&\s*discounts|Discounts?)\s+"
        r"\(\$(?P<line_total>[\d,]+\.\d{2})\)\s+"
        r"(?P<tax_rate>[\d.]+%)$",
        re.IGNORECASE,
    )
    SHIPPING_LINE_PATTERN = re.compile(
        r"^(?P<line_number>\d+)\s+"
        r"(?P<description>Shipping\s*&\s*handling)\s+"
        r"\$?(?P<line_total>[\d,]+\.\d{2})\s+"
        r"(?P<tax_rate>[\d.]+%)$",
        re.IGNORECASE,
    )
    ASIN_INLINE_PATTERN = re.compile(r"ASIN:\s*(?P<asin>[A-Z0-9]{10})?", re.IGNORECASE)
    ASIN_STANDALONE_PATTERN = re.compile(r"^[A-Z0-9]{10}$")
    SOLD_BY_PATTERN = re.compile(r"Sold by:\s*(?P<vendor>.+)", re.IGNORECASE)
    ORDER_PATTERN = re.compile(r"Order #\s*(?P<order_number>[\d-]+)", re.IGNORECASE)

    def parse(self, raw_text: str) -> ParsedInvoice:
        property_code_raw = self._search(self.PROPERTY_CODE_PATTERN, raw_text)
        parsed_invoice = ParsedInvoice(
            invoice_number=self._search(self.INVOICE_NUMBER_PATTERN, raw_text),
            invoice_date=self._search_date(self.INVOICE_DATE_PATTERN, raw_text, "%B %d, %Y"),
            purchase_date=self._search_date(self.PURCHASE_DATE_PATTERN, raw_text, "%d-%b-%Y"),
            purchaser=self._search(self.PURCHASER_PATTERN, raw_text),
            po_number=self._search(self.PO_NUMBER_PATTERN, raw_text),
            invoice_gl_code=self._search(self.GL_CODE_PATTERN, raw_text),
            property_code_raw=property_code_raw,
            property_code_normalized=self._normalize_property_code(property_code_raw),
            subtotal=self._search_money(self.SUBTOTAL_PATTERN, raw_text),
            tax_total=self._search_money(self.TAX_TOTAL_PATTERN, raw_text),
            grand_total=self._search_money(self.GRAND_TOTAL_PATTERN, raw_text),
            line_items=self._extract_line_items(raw_text),
        )
        self._reconcile_tax(parsed_invoice)
        self._validate_invoice(parsed_invoice)
        return parsed_invoice

    def _reconcile_tax(self, parsed_invoice: ParsedInvoice) -> None:
        """Adjust per-item tax so the sum matches the invoice-level tax total.

        Per-item tax is computed from line_total * tax_rate, which can drift by
        a penny or two due to rounding.  The difference is added to the first
        taxable line item so the per-item amounts reconcile exactly to the
        invoice total.
        """
        if parsed_invoice.tax_total is None:
            return
        taxable_items = [
            item for item in parsed_invoice.line_items
            if item.tax_amount and item.tax_amount != Decimal("0.00")
        ]
        if not taxable_items:
            return
        computed_tax = sum(item.tax_amount for item in taxable_items)
        difference = parsed_invoice.tax_total - computed_tax
        if difference != Decimal("0"):
            taxable_items[0].tax_amount += difference

    def _validate_invoice(self, parsed_invoice: ParsedInvoice) -> None:
        missing_fields = []
        if not parsed_invoice.invoice_number:
            missing_fields.append("invoice number")
        if not parsed_invoice.property_code_raw:
            missing_fields.append("property code")
        if not parsed_invoice.line_items:
            missing_fields.append("line items")

        if missing_fields:
            missing = ", ".join(missing_fields)
            raise ValueError(
                "Unsupported invoice PDF. Could not identify required Amazon "
                f"invoice field(s): {missing}."
            )

        has_invoice_context = any([
            parsed_invoice.invoice_date,
            parsed_invoice.purchase_date,
            parsed_invoice.purchaser,
            parsed_invoice.po_number,
            parsed_invoice.subtotal is not None,
            parsed_invoice.tax_total is not None,
            parsed_invoice.grand_total is not None,
        ])
        if not has_invoice_context:
            raise ValueError(
                "Unsupported invoice PDF. It has an invoice number, property "
                "code, and line-like rows, but no recognizable Amazon invoice "
                "metadata or totals."
            )

    def _extract_line_items(self, raw_text: str) -> list[InvoiceLineItem]:
        lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
        line_items: list[InvoiceLineItem] = []
        active_line_item: InvoiceLineItem | None = None

        for line in lines:
            if self._is_section_break(line):
                active_line_item = None
                continue

            product_match = self.PRODUCT_LINE_PATTERN.match(line)
            if product_match:
                line_total = self._to_decimal(product_match.group("line_total"))
                tax_rate = self._parse_tax_rate(product_match.group("tax_rate"))
                active_line_item = InvoiceLineItem(
                    line_number=int(product_match.group("line_number")),
                    item_type="product",
                    description=product_match.group("description").strip(),
                    quantity=int(product_match.group("quantity")),
                    unit_price=self._to_decimal(product_match.group("unit_price")),
                    line_total=line_total,
                    tax_rate=tax_rate,
                    tax_amount=self._compute_tax(line_total, tax_rate),
                )
                line_items.append(active_line_item)
                continue

            discount_match = self.DISCOUNT_LINE_PATTERN.match(line)
            if discount_match:
                line_total = -self._to_decimal(discount_match.group("line_total"))
                tax_rate = self._parse_tax_rate(discount_match.group("tax_rate"))
                active_line_item = InvoiceLineItem(
                    line_number=int(discount_match.group("line_number")),
                    item_type="discount",
                    description=discount_match.group("description").strip(),
                    quantity=None,
                    unit_price=None,
                    line_total=line_total,
                    tax_rate=tax_rate,
                    tax_amount=self._compute_tax(line_total, tax_rate),
                )
                line_items.append(active_line_item)
                continue

            shipping_match = self.SHIPPING_LINE_PATTERN.match(line)
            if shipping_match:
                line_total = self._to_decimal(shipping_match.group("line_total"))
                tax_rate = self._parse_tax_rate(shipping_match.group("tax_rate"))
                active_line_item = InvoiceLineItem(
                    line_number=int(shipping_match.group("line_number")),
                    item_type="shipping",
                    description=shipping_match.group("description").strip(),
                    quantity=None,
                    unit_price=None,
                    line_total=line_total,
                    tax_rate=tax_rate,
                    tax_amount=self._compute_tax(line_total, tax_rate),
                )
                line_items.append(active_line_item)
                continue

            if not active_line_item:
                continue

            self._capture_item_metadata(active_line_item, line)

            if self._is_description_continuation(line, active_line_item):
                active_line_item.description = f"{active_line_item.description} {line}".strip()

        for line_item in line_items:
            line_item.normalized_description = self._normalize_description(line_item.description)

        return line_items

    def _capture_item_metadata(self, line_item: InvoiceLineItem, line: str) -> None:
        asin_inline = self.ASIN_INLINE_PATTERN.search(line)
        if asin_inline:
            asin_value = (asin_inline.group("asin") or "").strip()
            if asin_value:
                line_item.asin = asin_value

        sold_by_match = self.SOLD_BY_PATTERN.search(line)
        if sold_by_match:
            line_item.vendor = sold_by_match.group("vendor").strip()

        order_match = self.ORDER_PATTERN.search(line)
        if order_match:
            line_item.order_number = order_match.group("order_number").strip()

        if self.ASIN_STANDALONE_PATTERN.match(line) and not line_item.asin:
            line_item.asin = line

    def _is_section_break(self, line: str) -> bool:
        section_prefixes = (
            "Invoice",
            "Invoice summary",
            "Item subtotal",
            "Description Qty Unit price before tax Tax",
            "Total before tax",
            "Tax ",
            "Amount due",
            "Registered business name",
            "Pay by",
            "Bill to",
            "Ship to",
            "FAQs",
            "How is tax calculated?",
            "How are digital products and services taxed?",
            "When will I get a refund for undelivered items?",
            "Visit https://",
            "Page",
            "Include Amazon invoice number",
        )
        return line.startswith(section_prefixes)

    def _is_description_continuation(self, line: str, active_line_item: InvoiceLineItem) -> bool:
        metadata_prefixes = (
            "ASIN:",
            "Sold by:",
            "Order #",
        )
        if line.startswith(metadata_prefixes):
            return False
        if self.ASIN_STANDALONE_PATTERN.match(line) and not active_line_item.description.endswith(line):
            return False
        if self.PRODUCT_LINE_PATTERN.match(line) or self.DISCOUNT_LINE_PATTERN.match(line) or self.SHIPPING_LINE_PATTERN.match(line):
            return False
        if self._is_section_break(line):
            return False
        if line.lower().startswith("purchased by"):
            return False
        return True

    def _search(self, pattern: re.Pattern[str], raw_text: str) -> str:
        match = pattern.search(raw_text)
        return match.group(1).strip() if match else ""

    def _search_money(self, pattern: re.Pattern[str], raw_text: str) -> Decimal | None:
        match = pattern.search(raw_text)
        if not match:
            return None
        return self._to_decimal(match.group(1))

    def _search_date(self, pattern: re.Pattern[str], raw_text: str, date_format: str):
        match = pattern.search(raw_text)
        if not match:
            return None
        try:
            return datetime.strptime(match.group(1).strip(), date_format).date()
        except ValueError:
            return None

    def _normalize_property_code(self, value: str) -> str:
        return value.strip().upper()

    def _normalize_description(self, value: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower())
        return re.sub(r"\s+", " ", cleaned).strip()

    def _parse_tax_rate(self, value: str) -> Decimal:
        cleaned = value.replace("%", "").strip()
        try:
            return Decimal(cleaned)
        except Exception:
            return Decimal("0")

    def _compute_tax(self, line_total: Decimal, tax_rate: Decimal) -> Decimal:
        if not line_total or not tax_rate:
            return Decimal("0.00")
        raw = line_total * tax_rate / Decimal("100")
        return raw.quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    def _to_decimal(self, value: str) -> Decimal:
        from decimal import InvalidOperation
        cleaned_value = value.replace(",", "").replace("$", "").strip()
        try:
            return Decimal(cleaned_value)
        except InvalidOperation:
            raise ValueError(f"Could not parse amount: {value!r}")
