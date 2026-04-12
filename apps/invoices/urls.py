from django.urls import path

from .views import (
    bulk_upload_view,
    clear_data_view,
    dashboard_view,
    invoice_detail_view,
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
    path("clear-data/", clear_data_view, name="clear_data"),
]
