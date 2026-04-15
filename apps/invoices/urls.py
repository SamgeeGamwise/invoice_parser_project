from django.urls import path

from .views import (
    approve_item_view,
    bulk_upload_view,
    clear_data_view,
    dashboard_view,
    export_gl_spend_csv,
    export_items_by_gl_csv,
    export_items_by_property_csv,
    export_line_items_csv,
    gl_codes_view,
    invoice_detail_view,
    properties_view,
    property_audit_view,
    reference_data_view,
    reports_view,
    review_queue_view,
    yardi_download_view,
    yardi_submit_view,
)


app_name = "invoices"

urlpatterns = [
    path("", dashboard_view, name="dashboard"),
    path("upload/", bulk_upload_view, name="bulk_upload"),
    path("invoices/<int:invoice_id>/", invoice_detail_view, name="invoice_detail"),
    path("reports/", reports_view, name="reports"),
    path("reports/export/line-items/", export_line_items_csv, name="export_line_items"),
    path("reports/export/gl-spend/", export_gl_spend_csv, name="export_gl_spend"),
    path("reports/export/items-by-gl/", export_items_by_gl_csv, name="export_items_by_gl"),
    path("reports/export/items-by-property/", export_items_by_property_csv, name="export_items_by_property"),
    path("review/", review_queue_view, name="review_queue"),
    path("review/approve/<int:item_id>/", approve_item_view, name="approve_item"),
    path("gl-codes/", gl_codes_view, name="gl_codes"),
    path("properties/", properties_view, name="properties"),
    path("reference-data/", reference_data_view, name="reference_data"),
    path("property-audit/", property_audit_view, name="property_audit"),
    path("clear-data/", clear_data_view, name="clear_data"),
    path("yardi-submit/", yardi_submit_view, name="yardi_submit"),
    path("yardi-submit/download/<str:filename>/", yardi_download_view, name="yardi_download"),
]
