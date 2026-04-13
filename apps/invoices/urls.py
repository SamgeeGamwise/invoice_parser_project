from django.urls import path

from .views import (
    approve_item_view,
    bulk_upload_view,
    clear_data_view,
    dashboard_view,
    invoice_detail_view,
    property_audit_view,
    reports_view,
    results_view,
    review_queue_view,
)


app_name = "invoices"

urlpatterns = [
    path("", dashboard_view, name="dashboard"),
    path("upload/", dashboard_view, name="upload"),
    path("bulk/", bulk_upload_view, name="bulk_upload"),
    path("invoices/<int:invoice_id>/", invoice_detail_view, name="invoice_detail"),
    path("reports/", reports_view, name="reports"),
    path("results/", results_view, name="results"),
    path("review/", review_queue_view, name="review_queue"),
    path("review/approve/<int:item_id>/", approve_item_view, name="approve_item"),
    path("property-audit/", property_audit_view, name="property_audit"),
    path("clear-data/", clear_data_view, name="clear_data"),
]
