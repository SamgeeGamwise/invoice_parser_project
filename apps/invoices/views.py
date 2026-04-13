import json
import queue
import threading
from pathlib import Path

from django.contrib import messages
from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import F, Q
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import BulkInvoiceUploadForm, InvoiceUploadForm
from .models import GLAccount, Invoice, InvoiceLineItem, PropertyReference
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
        approved_count = 0
        cleared_count = 0

        for item in invoice.line_items.all():
            approved_gl_code = request.POST.get(f"item_{item.id}_gl", "").strip()
            approval_notes = request.POST.get(f"item_{item.id}_notes", "").strip()
            approved_gl = GLAccount.objects.filter(code=approved_gl_code).first() if approved_gl_code else None
            block_reason = _approval_block_reason(item, approved_gl) if approved_gl else None

            item.approval_notes = approval_notes

            if block_reason:
                item.approved_gl = None
                item.reviewed_at = None
                item.save(update_fields=["approved_gl", "approval_notes", "reviewed_at", "updated_at"])
                continue

            item.approved_gl = approved_gl
            if approved_gl:
                item.mark_reviewed()
                approved_count += 1
            else:
                item.reviewed_at = None
                cleared_count += 1
            item.save(update_fields=["approved_gl", "approval_notes", "reviewed_at", "updated_at"])

        if not invoice.has_valid_property:
            messages.error(
                request,
                (
                    f"Invoice {invoice.invoice_number} is flagged: approvals require both a GL "
                    "and a validated property code."
                ),
            )
        elif approved_count:
            messages.success(
                request,
                f"Saved {approved_count} approval{'s' if approved_count != 1 else ''} for invoice {invoice.invoice_number}.",
            )
        elif cleared_count:
            messages.info(
                request,
                f"Cleared {cleared_count} approval{'s' if cleared_count != 1 else ''} for invoice {invoice.invoice_number}.",
            )

        return redirect("invoices:invoice_detail", invoice_id=invoice.id)

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


_SORT_FIELDS = {
    "description": "description",
    "invoice":     "invoice__invoice_number",
    "amount":      "line_total",
    "gl":          "invoice_gl_code_hint",
    "confidence":  "suggested_confidence",
}


def _approval_block_reason(item: InvoiceLineItem, gl: GLAccount | None) -> str | None:
    if gl is None:
        return "No GL code provided."
    if not item.has_valid_property:
        return (
            f"Invoice {item.invoice.invoice_number} is flagged because its property code "
            "is missing or not validated."
        )
    return None


def _item_tier(item: InvoiceLineItem, has_invoice_peers: bool = False) -> str:
    """Return 'auto', 'confirm', or 'review' based on confidence and invoice GL agreement.

    has_invoice_peers: True when at least one other item on the same invoice
    has already been approved to that invoice's own GL code. This acts as a
    strong reinforcement signal — if peers confirm the invoice GL is correct,
    remaining items on that invoice should be treated with higher confidence.
    """
    cfg = settings.ML_CONFIG
    confidence = float(item.suggested_confidence or 0)
    agrees = (
        item.suggested_gl_id is not None
        and item.invoice_gl_code_hint
        and item.suggested_gl.code == item.invoice_gl_code_hint
    )
    if agrees and confidence >= cfg["TIER_AUTO_APPROVE_AGREE"]:
        return "auto"
    # Peer boost: sibling items on this invoice already confirmed to the invoice GL.
    # Promote to auto if we agree and at least meet the confirm threshold.
    if agrees and has_invoice_peers and confidence >= cfg["TIER_QUICK_CONFIRM_AGREE"]:
        return "auto"
    if agrees and confidence >= cfg["TIER_QUICK_CONFIRM_AGREE"]:
        return "confirm"
    if not agrees and confidence >= cfg["TIER_QUICK_CONFIRM_OVERRIDE"]:
        return "confirm"
    # Peer boost for borderline cases: agree with invoice GL but below confirm threshold.
    if agrees and has_invoice_peers:
        return "confirm"
    return "review"


def review_queue_view(request: HttpRequest) -> HttpResponse:
    """Paginated queue of all unreviewed product line items across every invoice."""

    # Sorting
    sort_col = request.GET.get("sort", "confidence")
    if sort_col not in _SORT_FIELDS:
        sort_col = "confidence"

    sort_dir = "desc" if request.GET.get("dir") == "desc" else "asc"
    sort_field = _SORT_FIELDS[sort_col]
    order_expr = sort_field if sort_dir == "asc" else f"-{sort_field}"

    pending_qs = (
        InvoiceLineItem.objects
        .filter(item_type=InvoiceLineItem.ItemType.PRODUCT)
        .filter(Q(approved_gl__isnull=True) | Q(invoice__property_reference__isnull=True))
        .select_related("invoice", "invoice__property_reference", "suggested_gl", "approved_gl")
        .order_by(order_expr, "id")
    )
    total_pending = pending_qs.count()

    if request.method == "POST":
        item_ids = [int(x) for x in request.POST.get("item_ids", "").split(",") if x.strip()]
        items_on_page = list(
            InvoiceLineItem.objects.filter(pk__in=item_ids).select_related(
                "invoice", "invoice__property_reference", "suggested_gl", "approved_gl"
            )
        )
        now = timezone.now()
        to_update: list[InvoiceLineItem] = []
        approved_count = 0
        blocked_invoices: set[str] = set()

        for item in items_on_page:
            gl_code = request.POST.get(f"item_{item.id}_gl", "").strip()
            gl = GLAccount.objects.filter(code=gl_code).first() if gl_code else None
            block_reason = _approval_block_reason(item, gl) if gl else None

            if block_reason:
                blocked_invoices.add(item.invoice.invoice_number)
                continue

            if gl:
                item.approved_gl = gl
                item.reviewed_at = now
                to_update.append(item)
                approved_count += 1

        if to_update:
            InvoiceLineItem.objects.bulk_update(to_update, ["approved_gl", "reviewed_at", "updated_at"])

        if approved_count:
            messages.success(
                request,
                f"Saved {approved_count} approval{'s' if approved_count != 1 else ''} from this page.",
            )
        if blocked_invoices:
            blocked_list = ", ".join(sorted(blocked_invoices))
            messages.error(
                request,
                f"Flagged invoice{'s' if len(blocked_invoices) != 1 else ''} require a validated property code before approval: {blocked_list}.",
            )

        page = request.POST.get("page", "1")
        return redirect(f"{request.path}?page={page}&sort={sort_col}&dir={sort_dir}")

    paginator = Paginator(pending_qs, _QUEUE_PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page", 1))

    # Pre-compute which invoices on this page have at least one item already
    # approved to that invoice's own GL code. One query for the whole page.
    page_invoice_ids = [item.invoice_id for item in page_obj]
    invoices_with_peers = set(
        InvoiceLineItem.objects
        .filter(
            invoice_id__in=page_invoice_ids,
            approved_gl__isnull=False,
            invoice__property_reference__isnull=False,
            approved_gl__code=F("invoice__invoice_gl_code"),
        )
        .values_list("invoice_id", flat=True)
        .distinct()
    )

    # Annotate each item with its confidence tier for the template.
    for item in page_obj:
        item.tier = _item_tier(item, has_invoice_peers=item.invoice_id in invoices_with_peers)

    property_blocked_count = sum(1 for item in page_obj if not item.has_valid_property)
    item_ids_csv = ",".join(str(item.pk) for item in page_obj)
    gl_accounts = list(GLAccount.objects.filter(in_review_range=True).order_by("code"))

    return render(request, "invoices/review_queue.html", {
        "page_obj": page_obj,
        "gl_accounts": gl_accounts,
        "total_pending": total_pending,
        "item_ids_csv": item_ids_csv,
        "sort_col": sort_col,
        "sort_dir": sort_dir,
        "property_blocked_count": property_blocked_count,
    })


@require_POST
def approve_item_view(request: HttpRequest, item_id: int) -> JsonResponse:
    """Approve a single line item via AJAX. Returns updated pending count."""
    item = get_object_or_404(
        InvoiceLineItem.objects.select_related("invoice", "invoice__property_reference", "suggested_gl"),
        pk=item_id,
        item_type=InvoiceLineItem.ItemType.PRODUCT,
    )
    gl_code = request.POST.get("gl_code", "").strip()
    gl = GLAccount.objects.filter(code=gl_code).first() if gl_code else item.suggested_gl
    block_reason = _approval_block_reason(item, gl)

    if block_reason:
        return JsonResponse({"ok": False, "error": block_reason}, status=400)

    item.approved_gl = gl
    item.reviewed_at = timezone.now()
    item.save(update_fields=["approved_gl", "reviewed_at", "updated_at"])

    pending = InvoiceLineItem.objects.filter(
        item_type=InvoiceLineItem.ItemType.PRODUCT,
    ).filter(
        Q(approved_gl__isnull=True) | Q(invoice__property_reference__isnull=True)
    ).count()

    return JsonResponse({"ok": True, "pending": pending})


def property_audit_view(request: HttpRequest) -> HttpResponse:
    """Diagnostic: shows every raw property code found on invoices and how it resolved."""
    from collections import defaultdict
    from django.db.models import Count

    # All unique (raw, normalized, reference) combos with invoice counts
    combos = list(
        Invoice.objects
        .values("property_code_raw", "property_code_normalized", "property_reference_id")
        .annotate(count=Count("id"))
        .order_by("property_code_raw", "property_code_normalized")
    )

    # Group raw codes case-insensitively so "ssoh" and "SSOH" collapse together.
    raw_groups: dict = defaultdict(list)
    for row in combos:
        raw_key = (row["property_code_raw"] or "").strip().upper()
        raw_groups[raw_key].append(row)

    invoice_groups = []
    for raw, entries in sorted(raw_groups.items(), key=lambda x: (x[0] or "")):
        normalized_set = sorted({e["property_code_normalized"] for e in entries if e["property_code_normalized"]})
        invoice_groups.append({
            "raw": raw or "(blank)",
            "total": sum(e["count"] for e in entries),
            "normalized_set": normalized_set,
            "matched": any(e["property_reference_id"] for e in entries),
            "inconsistent": len(normalized_set) > 1,
        })

    # All known PropertyReferences annotated with how many invoices they received
    references = list(
        PropertyReference.objects
        .annotate(invoice_count=Count("invoices"))
        .order_by("normalized_code")
    )

    total = Invoice.objects.count()
    matched = Invoice.objects.filter(property_reference__isnull=False).count()

    return render(request, "invoices/property_audit.html", {
        "invoice_groups": invoice_groups,
        "references": references,
        "total_invoices": total,
        "matched_invoices": matched,
        "unmatched_invoices": total - matched,
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
