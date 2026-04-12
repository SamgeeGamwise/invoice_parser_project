from django.contrib import admin

from .models import GLAccount, Invoice, InvoiceLineItem, PropertyReference


@admin.register(GLAccount)
class GLAccountAdmin(admin.ModelAdmin):
    list_display = ("code", "description", "in_review_range")
    search_fields = ("code", "description")


@admin.register(PropertyReference)
class PropertyReferenceAdmin(admin.ModelAdmin):
    list_display = ("normalized_code", "website_id", "yardi_code")
    search_fields = ("normalized_code", "yardi_code", "website_id")


class InvoiceLineItemInline(admin.TabularInline):
    model = InvoiceLineItem
    extra = 0
    fields = (
        "line_number",
        "item_type",
        "description",
        "line_total",
        "suggested_gl",
        "approved_gl",
    )
    readonly_fields = ("suggestion_reason",)


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        "invoice_number",
        "invoice_date",
        "property_code_normalized",
        "invoice_gl_code",
        "pending_review_count",
    )
    search_fields = ("invoice_number", "property_code_normalized", "purchaser")
    inlines = [InvoiceLineItemInline]
