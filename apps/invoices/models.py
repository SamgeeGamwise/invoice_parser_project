from django.db import models
from django.db.models import Q
from django.utils import timezone


class GLAccount(models.Model):
    code = models.CharField(max_length=10, unique=True)
    description = models.CharField(max_length=255)
    in_review_range = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} - {self.description}"


class PropertyReference(models.Model):
    website_id = models.CharField(max_length=20, blank=True)
    yardi_code = models.CharField(max_length=20, unique=True)
    normalized_code = models.CharField(max_length=20, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["normalized_code"]

    def __str__(self) -> str:
        return self.normalized_code


class Invoice(models.Model):
    invoice_number = models.CharField(max_length=40, unique=True)
    source_file_name = models.CharField(max_length=255, blank=True)
    source_file_size = models.BigIntegerField(null=True, blank=True)
    source_content_type = models.CharField(max_length=100, blank=True)
    invoice_date = models.DateField(null=True, blank=True)
    purchase_date = models.DateField(null=True, blank=True)
    purchaser = models.CharField(max_length=255, blank=True)
    po_number = models.CharField(max_length=100, blank=True)
    invoice_gl_code = models.CharField(max_length=10, blank=True)
    invoice_gl_description = models.CharField(max_length=255, blank=True)
    property_code_raw = models.CharField(max_length=20, blank=True)
    property_code_normalized = models.CharField(max_length=20, db_index=True, blank=True)
    property_reference = models.ForeignKey(
        PropertyReference,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="invoices",
    )
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    tax_total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    grand_total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    raw_text = models.TextField(blank=True)
    status = models.CharField(max_length=255, blank=True)
    upload_batch_id = models.CharField(max_length=64, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-invoice_date", "-created_at"]

    @property
    def has_valid_property(self) -> bool:
        return self.property_reference_id is not None

    @property
    def property_status_label(self) -> str:
        return self.property_code_normalized or self.property_code_raw or "Missing property code"

    @property
    def pending_review_count(self) -> int:
        return self.line_items.filter(
            item_type=InvoiceLineItem.ItemType.PRODUCT,
        ).filter(
            Q(approved_gl__isnull=True) | Q(invoice__property_reference__isnull=True)
        ).count()

    def __str__(self) -> str:
        return self.invoice_number


class InvoiceLineItem(models.Model):
    class ItemType(models.TextChoices):
        PRODUCT = "product", "Product"
        SHIPPING = "shipping", "Shipping"
        DISCOUNT = "discount", "Discount"
        FEE = "fee", "Fee"

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="line_items")
    line_number = models.PositiveIntegerField()
    item_type = models.CharField(max_length=20, choices=ItemType.choices, default=ItemType.PRODUCT)
    description = models.TextField()
    normalized_description = models.TextField(blank=True)
    quantity = models.IntegerField(null=True, blank=True)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    line_total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    asin = models.CharField(max_length=20, blank=True)
    vendor = models.CharField(max_length=255, blank=True)
    order_number = models.CharField(max_length=40, blank=True)
    invoice_gl_code_hint = models.CharField(max_length=10, blank=True)
    suggested_gl = models.ForeignKey(
        GLAccount,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="suggested_line_items",
    )
    suggested_confidence = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    suggestion_reason = models.TextField(blank=True)
    suggestion_candidates = models.JSONField(default=list, blank=True)
    approved_gl = models.ForeignKey(
        GLAccount,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_line_items",
    )
    approval_notes = models.TextField(blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["invoice_id", "line_number", "id"]
        unique_together = ("invoice", "line_number")

    @property
    def effective_gl(self) -> GLAccount | None:
        return self.approved_gl or self.suggested_gl

    @property
    def has_valid_property(self) -> bool:
        return self.invoice.property_reference_id is not None

    @property
    def approval_block_reason(self) -> str:
        if self.has_valid_property:
            return ""
        return (
            "Missing validated property code "
            f"for invoice {self.invoice.invoice_number}."
        )

    @property
    def needs_review(self) -> bool:
        return (
            self.item_type == self.ItemType.PRODUCT
            and (self.approved_gl_id is None or not self.has_valid_property)
        )

    def mark_reviewed(self) -> None:
        self.reviewed_at = timezone.now()

    def __str__(self) -> str:
        return f"{self.invoice.invoice_number} #{self.line_number}"
