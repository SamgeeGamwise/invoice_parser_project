import json
import queue
import threading
from pathlib import Path

from django.conf import settings
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import BulkInvoiceUploadForm, InvoiceUploadForm
from .models import GLAccount, Invoice, InvoiceLineItem
from .schemas import InvoiceLineItem as ParsedLineItem
from .services.classification import LineItemGLClassifierService
from .services.data_catalog import ProjectDataCatalogService
from .services.orchestrator import InvoiceProcessingService
from .services.output_writer import InvoiceOutputWriterService
from .services.reporting import ReportingService
from .services.repository import InvoiceRepositoryService


def _display_path(path) -> str:
    """Return a path relative to BASE_DIR for cleaner display."""
    if isinstance(path, str):
        path = Path(path)
    try:
        return str(path.relative_to(settings.BASE_DIR))
    except ValueError:
        return str(path)


def _dashboard_context(single_form=None, bulk_form=None) -> dict:
    reporting = ReportingService()
    data_catalog = ProjectDataCatalogService()
    stats = reporting.dashboard_stats()

    total_reviewable = stats["line_item_count"]
    reviewed = stats["reviewed_item_count"]
    review_pct = int((reviewed / total_reviewable) * 100) if total_reviewable else 0

    return {
        "single_form": single_form or InvoiceUploadForm(),
        "bulk_form": bulk_form or BulkInvoiceUploadForm(),
        "stats": stats,
        "review_pct": review_pct,
        "recent_invoices": Invoice.objects.prefetch_related("line_items")[:10],
        "sample_invoice_count": len(data_catalog.list_sample_invoices()),
        "reference_files": data_catalog.list_reference_files(),
        "output_path": _display_path(settings.PARSED_INVOICES_JSON),
        "max_files": settings.BULK_UPLOAD_MAX_FILES,
    }


def dashboard_view(request: HttpRequest) -> HttpResponse:
    """Main hub: shows stats, a single-file upload form, and recent invoices."""
    if request.method == "POST":
        form = InvoiceUploadForm(request.POST, request.FILES)
        if form.is_valid():
            processor = InvoiceProcessingService()
            repository = InvoiceRepositoryService()
            parsed = processor.process(form.cleaned_data["invoice_pdf"])
            saved = repository.save_parsed_invoices([parsed])[0]
            return redirect("invoices:invoice_detail", invoice_id=saved.id)
        return render(request, "invoices/dashboard.html", _dashboard_context(single_form=form))

    return render(request, "invoices/dashboard.html", _dashboard_context())


def bulk_upload_view(request: HttpRequest) -> HttpResponse:
    """Upload many PDFs at once, parse them all, save results to DB and JSON."""
    if request.method == "POST":
        form = BulkInvoiceUploadForm(request.POST, request.FILES)
        if form.is_valid():
            files = form.cleaned_data["invoice_pdfs"]
            progress_q: queue.Queue = queue.Queue()

            def run():
                try:
                    processor = InvoiceProcessingService()
                    repository = InvoiceRepositoryService()
                    writer = InvoiceOutputWriterService()

                    def on_progress(current, total, filename, status):
                        progress_q.put({
                            "type": "progress",
                            "current": current,
                            "total": total,
                            "filename": filename,
                            "status": status,
                        })

                    result = processor.bulk_process(files, progress_callback=on_progress)

                    progress_q.put({"type": "saving"})
                    repository.save_parsed_invoices(result.invoices)
                    output_path = writer.write(result)

                    progress_q.put({
                        "type": "done",
                        "success": result.success_count,
                        "errors": result.error_count,
                        "error_list": result.errors[:20],
                        "output_path": _display_path(output_path),
                    })
                except Exception as exc:
                    progress_q.put({"type": "fatal", "error": str(exc)})
                finally:
                    progress_q.put(None)  # sentinel

            threading.Thread(target=run, daemon=True).start()

            def generate():
                while True:
                    item = progress_q.get()
                    if item is None:
                        break
                    yield f"data: {json.dumps(item)}\n\n"

            response = StreamingHttpResponse(generate(), content_type="text/event-stream")
            response["Cache-Control"] = "no-cache"
            response["X-Accel-Buffering"] = "no"  # disable nginx buffering if present
            return response

        return render(request, "invoices/bulk_upload.html", {
            "form": form,
            "max_files": settings.BULK_UPLOAD_MAX_FILES,
        })

    return render(request, "invoices/bulk_upload.html", {
        "form": BulkInvoiceUploadForm(),
        "max_files": settings.BULK_UPLOAD_MAX_FILES,
    })


def _rescore_unreviewed(invoice: Invoice) -> None:
    """Re-run the classifier for every unreviewed product item on this invoice.

    Called on each GET so that suggestions always reflect the latest approvals
    from other invoices (the KNN history grows with every human approval).
    """
    classifier = LineItemGLClassifierService()
    invoice_gl_code = invoice.invoice_gl_code or ""
    to_update: list[InvoiceLineItem] = []

    for item in invoice.line_items.all():
        if not item.needs_review:
            continue

        parsed = ParsedLineItem(
            item_type=item.item_type,
            description=item.description,
            vendor=item.vendor,
            invoice_gl_code_hint=invoice_gl_code,
        )
        suggestions = classifier.suggest(parsed, invoice_gl_code)
        if not suggestions:
            continue

        top = suggestions[0]
        new_gl = GLAccount.objects.filter(code=top.gl_code).first()
        item.suggested_gl = new_gl
        item.suggested_confidence = top.confidence
        item.suggestion_reason = " ".join(top.reasons)
        item.suggestion_candidates = [s.to_dict() for s in suggestions]
        to_update.append(item)

    if to_update:
        InvoiceLineItem.objects.bulk_update(
            to_update,
            ["suggested_gl", "suggested_confidence", "suggestion_reason", "suggestion_candidates"],
        )


def invoice_detail_view(request: HttpRequest, invoice_id: int) -> HttpResponse:
    """Review and approve GL codes for each line item on an invoice."""
    invoice = get_object_or_404(
        Invoice.objects.prefetch_related(
            "line_items__suggested_gl",
            "line_items__approved_gl",
            "property_reference",
        ),
        pk=invoice_id,
    )
    gl_accounts = list(GLAccount.objects.filter(in_review_range=True).order_by("code"))

    if request.method == "POST":
        for item in invoice.line_items.all():
            approved_gl_code = request.POST.get(f"item_{item.id}_gl", "").strip()
            approval_notes = request.POST.get(f"item_{item.id}_notes", "").strip()
            approved_gl = GLAccount.objects.filter(code=approved_gl_code).first() if approved_gl_code else None

            item.approved_gl = approved_gl
            item.approval_notes = approval_notes
            if approved_gl:
                item.mark_reviewed()
            else:
                item.reviewed_at = None
            item.save(update_fields=["approved_gl", "approval_notes", "reviewed_at", "updated_at"])

        return redirect("invoices:invoice_detail", invoice_id=invoice.id)

    _rescore_unreviewed(invoice)

    return render(request, "invoices/invoice_detail.html", {
        "invoice": invoice,
        "gl_accounts": gl_accounts,
    })


def reports_view(request: HttpRequest) -> HttpResponse:
    """Spend breakdown by GL code and property."""
    reporting = ReportingService()
    spend_by_gl = reporting.spend_by_gl()

    # Calculate bar widths for the CSS chart (relative to the largest amount).
    if spend_by_gl:
        max_amount = max(row["total_amount"] for row in spend_by_gl)
        for row in spend_by_gl:
            row["bar_pct"] = int((row["total_amount"] / max_amount) * 100) if max_amount else 0

    items_by_property = reporting.items_by_property()
    if items_by_property:
        max_prop = max(row["total_amount"] for row in items_by_property)
        for row in items_by_property:
            row["bar_pct"] = int((row["total_amount"] / max_prop) * 100) if max_prop else 0

    return render(request, "invoices/reports.html", {
        "stats": reporting.dashboard_stats(),
        "spend_by_gl": spend_by_gl,
        "items_by_property": items_by_property,
    })


def results_view(request: HttpRequest) -> HttpResponse:
    return redirect("invoices:dashboard")


_QUEUE_PAGE_SIZE = 50


def review_queue_view(request: HttpRequest) -> HttpResponse:
    """Paginated queue of all unreviewed product line items across every invoice."""
    pending_qs = (
        InvoiceLineItem.objects
        .filter(item_type=InvoiceLineItem.ItemType.PRODUCT, approved_gl__isnull=True)
        .select_related("invoice", "suggested_gl")
        .order_by("suggested_confidence", "invoice__invoice_date", "line_number")
    )
    total_pending = pending_qs.count()

    if request.method == "POST":
        action = request.POST.get("action", "save")
        item_ids = [int(x) for x in request.POST.get("item_ids", "").split(",") if x.strip()]
        items_on_page = list(
            InvoiceLineItem.objects.filter(pk__in=item_ids).select_related("suggested_gl")
        )

        now = timezone.now()
        to_update: list[InvoiceLineItem] = []

        for item in items_on_page:
            if action == "accept_all":
                # Accept the model's top suggestion for every item on this page.
                gl = item.suggested_gl
            else:
                gl_code = request.POST.get(f"item_{item.id}_gl", "").strip()
                gl = GLAccount.objects.filter(code=gl_code).first() if gl_code else None

            if gl:
                item.approved_gl = gl
                item.reviewed_at = now
                to_update.append(item)

        if to_update:
            InvoiceLineItem.objects.bulk_update(to_update, ["approved_gl", "reviewed_at", "updated_at"])

        page = request.POST.get("page", "1")
        return redirect(f"{request.path}?page={page}")

    paginator = Paginator(pending_qs, _QUEUE_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page", 1))
    item_ids_csv = ",".join(str(item.pk) for item in page_obj)
    gl_accounts = list(GLAccount.objects.filter(in_review_range=True).order_by("code"))

    return render(request, "invoices/review_queue.html", {
        "page_obj": page_obj,
        "gl_accounts": gl_accounts,
        "total_pending": total_pending,
        "item_ids_csv": item_ids_csv,
    })


def clear_data_view(request: HttpRequest) -> HttpResponse:
    """
    Delete all invoices and line items. Only available when DEBUG=True.
    GL accounts, property references, and the ML model are not affected.
    """
    if not settings.DEBUG:
        from django.http import Http404
        raise Http404

    from .models import InvoiceLineItem

    if request.method == "POST":
        invoice_count = Invoice.objects.count()
        InvoiceLineItem.objects.all().delete()
        Invoice.objects.all().delete()

        json_path = settings.PARSED_INVOICES_JSON
        if json_path.exists():
            json_path.unlink()

        return render(request, "invoices/clear_data.html", {
            "cleared": True,
            "invoice_count": invoice_count,
        })

    return render(request, "invoices/clear_data.html", {
        "cleared": False,
        "invoice_count": Invoice.objects.count(),
    })
