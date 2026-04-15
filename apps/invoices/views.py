import csv
import io
import json
import queue
import threading
from pathlib import Path

from django.contrib import messages
from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Count, F, Q
from django.http import HttpRequest, HttpResponse, JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import BulkInvoiceUploadForm, GLAccountForm, InvoiceUploadForm, PropertyReferenceForm
from .models import GLAccount, Invoice, InvoiceLineItem, PropertyReference
from .services.data_catalog import ProjectDataCatalogService
from .services.reference_data import ReferenceDataSyncService
from .services.orchestrator import InvoiceProcessingService
from .services.output_writer import InvoiceOutputWriterService
from .services.reporting import ReportingService
from .services.repository import InvoiceRepositoryService
from .services.yardi_submit import YardiSubmitService


def _display_path(path) -> str:
    """Return a path relative to BASE_DIR for cleaner display."""
    if isinstance(path, str):
        path = Path(path)
    try:
        return str(path.relative_to(settings.BASE_DIR))
    except ValueError:
        return str(path)


def _dashboard_context() -> dict:
    reporting = ReportingService()
    stats = reporting.dashboard_stats()
    total = stats["line_item_count"]
    review_pct = int((stats["ready_to_submit"] / total) * 100) if total else 0
    audit = _build_code_audit_context()
    return {
        "stats": stats,
        "review_pct": review_pct,
        "gl_account_count": GLAccount.objects.count(),
        "property_reference_count": PropertyReference.objects.count(),
        "gl_audit_missing_count": audit["gl_audit_missing_count"],
        "gl_audit_missing_invoices": audit["gl_audit_missing_invoices"],
        "property_audit_missing_count": audit["property_audit_missing_count"],
        "property_audit_missing_invoices": audit["property_audit_missing_invoices"],
        "total_audit_issues": (
            audit["gl_audit_missing_count"] + audit["property_audit_missing_count"]
        ),
    }


def _build_code_audit_context() -> dict:
    gl_accounts = {account.code: account for account in GLAccount.objects.order_by("code")}
    gl_rows_by_code: dict[str, dict] = {}
    for row in (
        Invoice.objects.exclude(invoice_gl_code="")
        .values("invoice_gl_code", "invoice_gl_description")
        .annotate(invoice_count=Count("id"))
        .order_by("invoice_gl_code", "invoice_gl_description")
    ):
        code = (row["invoice_gl_code"] or "").strip()
        if not code:
            continue
        bucket = gl_rows_by_code.setdefault(
            code,
            {
                "code": code,
                "invoice_count": 0,
                "descriptions": [],
                "matched_account": gl_accounts.get(code),
            },
        )
        bucket["invoice_count"] += row["invoice_count"]
        description = (row["invoice_gl_description"] or "").strip()
        if description and description not in bucket["descriptions"]:
            bucket["descriptions"].append(description)

    gl_audit_rows = sorted(gl_rows_by_code.values(), key=lambda row: row["code"])
    unmatched_gl_rows = [
        row for row in gl_audit_rows if row["matched_account"] is None
    ]

    property_rows_by_code: dict[str, dict] = {}
    for row in (
        Invoice.objects.values(
            "property_code_raw",
            "property_code_normalized",
            "property_reference_id",
        )
        .annotate(invoice_count=Count("id"))
        .order_by("property_code_normalized", "property_code_raw")
    ):
        raw_value = (row["property_code_raw"] or "").strip()
        normalized = (row["property_code_normalized"] or "").strip().upper()
        audit_code = normalized or raw_value.upper()
        bucket = property_rows_by_code.setdefault(
            audit_code,
            {
                "audit_code": audit_code,
                "display_code": audit_code or "(blank)",
                "invoice_count": 0,
                "raw_variants": [],
                "normalized_variants": [],
                "matched": False,
            },
        )
        bucket["invoice_count"] += row["invoice_count"]
        if raw_value and raw_value not in bucket["raw_variants"]:
            bucket["raw_variants"].append(raw_value)
        if normalized and normalized not in bucket["normalized_variants"]:
            bucket["normalized_variants"].append(normalized)
        bucket["matched"] = bucket["matched"] or bool(row["property_reference_id"])

    property_audit_rows = sorted(
        property_rows_by_code.values(),
        key=lambda row: (row["display_code"] == "(blank)", row["display_code"]),
    )
    unmatched_property_rows = [
        row for row in property_audit_rows if not row["matched"]
    ]

    return {
        "gl_audit_rows": gl_audit_rows,
        "unmatched_gl_rows": unmatched_gl_rows,
        "gl_audit_total_codes": len(gl_audit_rows),
        "gl_audit_missing_count": len(unmatched_gl_rows),
        "gl_audit_missing_invoices": sum(row["invoice_count"] for row in unmatched_gl_rows),
        "gl_audit_blank_invoices": Invoice.objects.filter(
            Q(invoice_gl_code="") | Q(invoice_gl_code__isnull=True)
        ).count(),
        "property_audit_rows": property_audit_rows,
        "unmatched_property_rows": unmatched_property_rows,
        "property_audit_total_codes": len(property_audit_rows),
        "property_audit_missing_count": len(unmatched_property_rows),
        "property_audit_missing_invoices": sum(
            row["invoice_count"] for row in unmatched_property_rows
        ),
        "property_audit_blank_invoices": Invoice.objects.filter(
            Q(property_code_normalized="") | Q(property_code_normalized__isnull=True)
        ).count(),
    }


def dashboard_view(request: HttpRequest) -> HttpResponse:
    return render(request, "invoices/dashboard.html", _dashboard_context())


def bulk_upload_view(request: HttpRequest) -> HttpResponse:
    """Upload many PDFs at once, parse them all, save results to DB and JSON."""
    if request.method == "POST":
        form = BulkInvoiceUploadForm(request.POST, request.FILES)
        if form.is_valid():
            files = form.cleaned_data["invoice_pdfs"]
            try:
                InvoiceProcessingService().reference_data.ensure_loaded()
            except RuntimeError as exc:
                form.add_error(None, str(exc))
                return render(request, "invoices/bulk_upload.html", {
                    "form": form,
                    "max_files": settings.BULK_UPLOAD_MAX_FILES,
                })
            progress_q: queue.Queue = queue.Queue()
            stop_event = threading.Event()

            def run():
                try:
                    processor = InvoiceProcessingService()
                    repository = InvoiceRepositoryService()
                    writer = InvoiceOutputWriterService()

                    def on_progress(current, total, filename, status):
                        # Stop feeding the queue if the client disconnected.
                        if stop_event.is_set():
                            raise InterruptedError("Client disconnected; processing cancelled.")
                        progress_q.put({
                            "type": "progress",
                            "current": current,
                            "total": total,
                            "filename": filename,
                            "status": status,
                        })

                    def on_status(message):
                        if stop_event.is_set():
                            raise InterruptedError("Client disconnected; processing cancelled.")
                        progress_q.put({
                            "type": "status",
                            "message": message,
                        })

                    progress_q.put({
                        "type": "status",
                        "message": "Starting invoice processing...",
                    })
                    result = processor.bulk_process(
                        files,
                        progress_callback=on_progress,
                        status_callback=on_status,
                    )

                    if stop_event.is_set():
                        return  # client left; skip the DB write

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
                except InterruptedError:
                    pass  # clean cancellation, nothing to report
                except Exception as exc:
                    progress_q.put({"type": "fatal", "error": str(exc)})
                finally:
                    progress_q.put(None)  # sentinel always fires

            threading.Thread(target=run, daemon=True).start()

            def generate():
                heartbeat_count = 0
                try:
                    while True:
                        try:
                            item = progress_q.get(timeout=15)
                        except queue.Empty:
                            heartbeat_count += 1
                            elapsed = heartbeat_count * 15
                            heartbeat = {
                                "type": "heartbeat",
                                "message": f"Still working... {elapsed} seconds elapsed.",
                            }
                            yield f"data: {json.dumps(heartbeat)}\n\n"
                            continue
                        if item is None:
                            break
                        yield f"data: {json.dumps(item)}\n\n"
                except GeneratorExit:
                    # Client closed the connection (refresh, navigation, etc.)
                    stop_event.set()

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
    gl_accounts = list(GLAccount.objects.order_by("code"))
    fix_property_form = PropertyReferenceForm(
        initial={
            "code": invoice.property_code_normalized or invoice.property_code_raw or "",
        },
        prefix="fix_prop",
    )

    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "assign_property":
            ref_id = request.POST.get("property_reference_id", "").strip()
            ref = PropertyReference.objects.filter(pk=ref_id).first()
            if ref:
                normalized = invoice.property_code_normalized
                if normalized:
                    updated = Invoice.objects.filter(
                        property_code_normalized=normalized,
                        property_reference__isnull=True,
                    ).update(property_reference=ref)
                else:
                    invoice.property_reference = ref
                    invoice.save(update_fields=["property_reference", "updated_at"])
                    updated = 1
                messages.success(
                    request,
                    f"Assigned \"{ref.code}\" to {updated} invoice(s) with code \"{normalized or invoice.property_code_raw}\".",
                )
            else:
                messages.error(request, "No valid property reference selected.")
            return redirect("invoices:invoice_detail", invoice_id=invoice.id)

        if action == "create_property":
            fix_property_form = PropertyReferenceForm(request.POST, prefix="fix_prop")
            if fix_property_form.is_valid():
                new_ref = fix_property_form.save()
                updated = Invoice.objects.filter(
                    property_code_normalized=new_ref.code,
                    property_reference__isnull=True,
                ).update(property_reference=new_ref)
                messages.success(
                    request,
                    f"Created property \"{new_ref.code}\" and linked {updated} invoice(s).",
                )
                return redirect("invoices:invoice_detail", invoice_id=invoice.id)
            # Form invalid — fall through to render with errors

        else:
            approved_count = 0
            cleared_count = 0

            for item in invoice.line_items.all():
                approved_gl_code = request.POST.get(f"item_{item.id}_gl", "").strip()
                approved_gl = GLAccount.objects.filter(code=approved_gl_code).first() if approved_gl_code else None
                block_reason = _approval_block_reason(item, approved_gl) if approved_gl else None

                if block_reason:
                    item.approved_gl = None
                    item.reviewed_at = None
                    item.save(update_fields=["approved_gl", "reviewed_at", "updated_at"])
                    continue

                item.approved_gl = approved_gl
                if approved_gl:
                    item.mark_reviewed()
                    approved_count += 1
                else:
                    item.reviewed_at = None
                    cleared_count += 1
                item.save(update_fields=["approved_gl", "reviewed_at", "updated_at"])

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
        "property_references": PropertyReference.objects.order_by("code"),
        "fix_property_form": fix_property_form,
    })


_REPORTS_GL_DEFAULT = 30
_REPORTS_PROP_DEFAULT = 30


def reports_view(request: HttpRequest) -> HttpResponse:
    """Spend breakdown by GL code and property."""
    reporting = ReportingService()
    show_all_gl = request.GET.get("gl") == "all"
    show_all_prop = request.GET.get("prop") == "all"

    spend_by_gl_all = reporting.spend_by_gl()
    gl_total_count = len(spend_by_gl_all)
    if spend_by_gl_all:
        max_amount = max(row["total_amount"] for row in spend_by_gl_all)
        for row in spend_by_gl_all:
            row["bar_pct"] = int((row["total_amount"] / max_amount) * 100) if max_amount else 0
    spend_by_gl = spend_by_gl_all if show_all_gl else spend_by_gl_all[:_REPORTS_GL_DEFAULT]

    items_by_property_all = reporting.items_by_property()
    prop_total_count = len(items_by_property_all)
    if items_by_property_all:
        max_prop = max(row["total_amount"] for row in items_by_property_all)
        for row in items_by_property_all:
            row["bar_pct"] = int((row["total_amount"] / max_prop) * 100) if max_prop else 0
    items_by_property = items_by_property_all if show_all_prop else items_by_property_all[:_REPORTS_PROP_DEFAULT]

    return render(request, "invoices/reports.html", {
        "stats": reporting.dashboard_stats(),
        "spend_by_gl": spend_by_gl,
        "gl_total_count": gl_total_count,
        "gl_showing_all": show_all_gl,
        "gl_default_cap": _REPORTS_GL_DEFAULT,
        "items_by_property": items_by_property,
        "prop_total_count": prop_total_count,
        "prop_showing_all": show_all_prop,
        "prop_default_cap": _REPORTS_PROP_DEFAULT,
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

    default_dir = "desc" if sort_col == "confidence" else "asc"
    sort_dir = request.GET.get("dir", default_dir)
    if sort_dir not in ("asc", "desc"):
        sort_dir = default_dir
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
    gl_accounts = list(GLAccount.objects.order_by("code"))

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


def reference_data_view(request: HttpRequest) -> HttpResponse:
    """CRUD UI for DB-backed GL accounts and property references."""
    gl_edit_id = request.GET.get("gl_edit")
    property_edit_id = request.GET.get("property_edit")

    gl_instance = GLAccount.objects.filter(pk=gl_edit_id).first() if gl_edit_id else None
    property_instance = (
        PropertyReference.objects.filter(pk=property_edit_id).first()
        if property_edit_id else None
    )

    gl_form = GLAccountForm(instance=gl_instance, prefix="gl")
    property_form = PropertyReferenceForm(instance=property_instance, prefix="property")

    if request.method == "POST":
        action = request.POST.get("action", "")
        reference_data = ReferenceDataSyncService()

        if action == "import_reference_data":
            before_gl = GLAccount.objects.count()
            before_prop = PropertyReference.objects.count()
            reference_data.sync_all(force=True)
            messages.success(
                request,
                (
                    f"Imported reference data from Excel. "
                    f"GL accounts: {before_gl} -> {GLAccount.objects.count()}, "
                    f"property references: {before_prop} -> {PropertyReference.objects.count()}."
                ),
            )
            return redirect("invoices:reference_data")

        if action == "save_gl":
            target = GLAccount.objects.filter(pk=request.POST.get("gl_id")).first()
            gl_form = GLAccountForm(request.POST, instance=target, prefix="gl")
            if gl_form.is_valid():
                record = gl_form.save()
                messages.success(request, f"Saved GL account {record.code}.")
                return redirect("invoices:reference_data")
            gl_instance = target

        elif action == "delete_gl":
            target = GLAccount.objects.filter(pk=request.POST.get("gl_id")).first()
            if target:
                code = target.code
                target.delete()
                messages.success(request, f"Deleted GL account {code}.")
            return redirect("invoices:reference_data")

        elif action == "save_property":
            target = PropertyReference.objects.filter(pk=request.POST.get("property_id")).first()
            property_form = PropertyReferenceForm(request.POST, instance=target, prefix="property")
            if property_form.is_valid():
                record = property_form.save()
                messages.success(request, f"Saved property reference {record.code}.")
                return redirect("invoices:reference_data")
            property_instance = target

        elif action == "delete_property":
            target = PropertyReference.objects.filter(pk=request.POST.get("property_id")).first()
            if target:
                code = target.code
                target.delete()
                messages.success(request, f"Deleted property reference {code}.")
            return redirect("invoices:reference_data")

    return render(request, "invoices/reference_data.html", {
        "gl_accounts": GLAccount.objects.order_by("code"),
        "property_references": PropertyReference.objects.order_by("code"),
        "gl_form": gl_form,
        "property_form": property_form,
        "gl_edit_id": int(gl_instance.id) if gl_instance else None,
        "property_edit_id": int(property_instance.id) if property_instance else None,
        "reference_file_count": len(ProjectDataCatalogService().list_reference_files()),
    })


def property_audit_view(request: HttpRequest) -> HttpResponse:
    """Interactive audit step for missing GL and property codes found on invoices."""
    gl_form = GLAccountForm(prefix="audit_gl")
    property_form = PropertyReferenceForm(prefix="audit_property")
    open_modal = ""

    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "create_gl_from_audit":
            gl_form = GLAccountForm(request.POST, prefix="audit_gl")
            open_modal = "gl"
            if gl_form.is_valid():
                record = gl_form.save()
                messages.success(request, f"Added GL code {record.code} from invoice audit.")
                return redirect("invoices:property_audit")

        elif action == "create_property_from_audit":
            property_form = PropertyReferenceForm(request.POST, prefix="audit_property")
            open_modal = "property"
            audit_code = (request.POST.get("audit_code") or "").strip().upper()
            if property_form.is_valid():
                record = property_form.save()
                updated = Invoice.objects.filter(
                    property_code_normalized=audit_code,
                    property_reference__isnull=True,
                ).update(property_reference=record)
                messages.success(
                    request,
                    f"Added property {record.code} and linked {updated} invoice(s) from audit code {audit_code or '(blank)'}.",
                )
                return redirect("invoices:property_audit")

        elif action == "assign_property_from_audit":
            audit_code = (request.POST.get("audit_code") or "").strip().upper()
            property_reference_id = request.POST.get("property_reference_id", "").strip()
            property_reference = PropertyReference.objects.filter(pk=property_reference_id).first()
            if not audit_code:
                messages.error(request, "Blank property codes cannot be linked automatically.")
                return redirect("invoices:property_audit")
            if property_reference is None:
                messages.error(request, "Choose a property to link before saving.")
                return redirect("invoices:property_audit")
            updated = Invoice.objects.filter(
                property_code_normalized=audit_code,
                property_reference__isnull=True,
            ).update(property_reference=property_reference)
            messages.success(
                request,
                f"Linked {updated} invoice(s) with property code {audit_code} to {property_reference.code}.",
            )
            return redirect("invoices:property_audit")

    context = _build_code_audit_context()
    context.update({
        "gl_form": gl_form,
        "property_form": property_form,
        "property_references": PropertyReference.objects.order_by("code"),
        "total_invoices": Invoice.objects.count(),
        "matched_property_invoices": Invoice.objects.filter(property_reference__isnull=False).count(),
        "open_modal": open_modal,
    })
    context["unmatched_property_invoices"] = (
        context["total_invoices"] - context["matched_property_invoices"]
    )
    return render(request, "invoices/property_audit.html", context)


def gl_codes_view(request: HttpRequest) -> HttpResponse:
    """Manage GL account codes used for invoice approval."""
    gl_form = GLAccountForm()
    gl_edit_id = None

    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "import_gl_csv":
            csv_file = request.FILES.get("csv_file")
            if not csv_file:
                messages.error(request, "No file selected.")
                return redirect("invoices:gl_codes")
            try:
                text = csv_file.read().decode("utf-8-sig")
                reader = csv.reader(io.StringIO(text))
                imported = 0
                for row in reader:
                    if len(row) < 2:
                        continue
                    code, description = row[0].strip(), row[1].strip()
                    if not code or not description:
                        continue
                    GLAccount.objects.update_or_create(
                        code=code,
                        defaults={
                            "description": description,
                            "in_review_range": ReferenceDataSyncService()._in_review_range(code),
                        },
                    )
                    imported += 1
                messages.success(request, f"Imported {imported} GL code(s) from CSV.")
            except Exception as exc:
                messages.error(request, f"Import failed: {exc}")
            return redirect("invoices:gl_codes")

        elif action == "save_gl":
            gl_id = request.POST.get("gl_id") or None
            target = GLAccount.objects.filter(pk=gl_id).first() if gl_id else None
            gl_edit_id = target.pk if target else None
            gl_form = GLAccountForm(request.POST, instance=target)
            if gl_form.is_valid():
                record = gl_form.save()
                messages.success(request, f"Saved GL code {record.code}.")
                return redirect("invoices:gl_codes")

        elif action == "delete_gl":
            gl_id = request.POST.get("gl_id") or None
            target = GLAccount.objects.filter(pk=gl_id).first() if gl_id else None
            if target:
                code = target.code
                target.delete()
                messages.success(request, f"Deleted GL code {code}.")
            return redirect("invoices:gl_codes")

    audit = _build_code_audit_context()
    return render(request, "invoices/gl_codes.html", {
        "gl_accounts": GLAccount.objects.order_by("code"),
        "gl_form": gl_form,
        "gl_edit_id": gl_edit_id,
        "gl_audit_missing_count": audit["gl_audit_missing_count"],
        "gl_audit_missing_invoices": audit["gl_audit_missing_invoices"],
    })


def properties_view(request: HttpRequest) -> HttpResponse:
    """Manage property codes used to validate invoices."""
    prop_form = PropertyReferenceForm()
    prop_edit_id = None

    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "import_property_csv":
            csv_file = request.FILES.get("csv_file")
            if not csv_file:
                messages.error(request, "No file selected.")
                return redirect("invoices:properties")
            try:
                text = csv_file.read().decode("utf-8-sig")
                reader = csv.reader(io.StringIO(text))
                svc = ReferenceDataSyncService()
                imported = 0
                for row in reader:
                    if len(row) < 2:
                        continue
                    website_id, raw_code = row[0].strip(), row[1].strip()
                    if not raw_code:
                        continue
                    code = svc.normalize_property_code(raw_code)
                    PropertyReference.objects.update_or_create(
                        code=code,
                        defaults={"website_id": website_id},
                    )
                    imported += 1
                messages.success(request, f"Imported {imported} property reference(s) from CSV.")
            except Exception as exc:
                messages.error(request, f"Import failed: {exc}")
            return redirect("invoices:properties")

        elif action == "save_property":
            prop_id = request.POST.get("property_id") or None
            target = PropertyReference.objects.filter(pk=prop_id).first() if prop_id else None
            prop_edit_id = target.pk if target else None
            prop_form = PropertyReferenceForm(request.POST, instance=target)
            if prop_form.is_valid():
                record = prop_form.save()
                messages.success(request, f"Saved property {record.code}.")
                return redirect("invoices:properties")

        elif action == "delete_property":
            prop_id = request.POST.get("property_id") or None
            target = PropertyReference.objects.filter(pk=prop_id).first() if prop_id else None
            if target:
                code = target.code
                target.delete()
                messages.success(request, f"Deleted property {code}.")
            return redirect("invoices:properties")

    audit = _build_code_audit_context()
    return render(request, "invoices/properties.html", {
        "property_references": PropertyReference.objects.order_by("code"),
        "prop_form": prop_form,
        "prop_edit_id": prop_edit_id,
        "property_audit_missing_count": audit["property_audit_missing_count"],
        "property_audit_missing_invoices": audit["property_audit_missing_invoices"],
    })


def yardi_submit_view(request: HttpRequest) -> HttpResponse:
    """Preview and confirm a Yardi submission.

    GET  — shows which invoices are ready and which are incomplete.
    POST — runs the submission: writes JSON + audit PDF, deletes submitted
           invoices, and renders the success summary.
    """
    service = YardiSubmitService()

    if request.method == "POST":
        try:
            result = service.submit()
        except (ValueError, RuntimeError) as exc:
            messages.error(request, str(exc))
            return redirect("invoices:yardi_submit")
        return render(request, "invoices/yardi_submitted.html", {
            "result": result,
            "json_filename": result.json_path.name,
            "audit_filename": result.audit_path.name,
            "json_path_display": _display_path(result.json_path),
            "audit_path_display": _display_path(result.audit_path),
        })

    preview = service.preview()
    return render(request, "invoices/yardi_submit.html", preview)


def yardi_download_view(request: HttpRequest, filename: str) -> HttpResponse:
    """Stream a previously generated submission file as a download.

    Only files in OUTPUT_DIR are served; path traversal is rejected.
    """
    import mimetypes
    output_dir = settings.OUTPUT_DIR
    target = (output_dir / filename).resolve()

    # Safety: ensure the resolved path is still inside OUTPUT_DIR
    try:
        target.relative_to(output_dir.resolve())
    except ValueError:
        from django.http import Http404
        raise Http404

    if not target.exists():
        from django.http import Http404
        raise Http404

    content_type, _ = mimetypes.guess_type(str(target))
    content_type = content_type or "application/octet-stream"

    response = HttpResponse(content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{target.name}"'
    response["Content-Length"] = target.stat().st_size
    with target.open("rb") as fh:
        response.write(fh.read())
    return response


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
