"""Custom VisionInspection UI views.

This module provides the custom web interface for the VisionInspection application.
It is intentionally separate from the API/operator views in views.py.
"""

import logging
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from VisionInspection.models import (
    VisionInspectionEmployee,
    VisionInspectionLog,
    VisionInspectionPart,
    VisionInspectionPreset,
    VisionPartConfiguration,
    VisionInspectionState,
    VisionROI,
    VisionTrainingJob,
    VisionTriggerZone,
    VisionYOLOModel,
)
from VisionInspection.services import inspection_records as records
from VisionInspection.services import report_exports
from VisionInspection.services.access_control import has_permission, is_admin
from VisionInspection.services.part_configuration import common_roi_camera_error

logger = logging.getLogger(__name__)


def favicon(request):
    """Serve the station's compact brand mark for browsers requesting /favicon.ico."""
    return HttpResponse(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="6" fill="#0b1c3d"/>'
        '<path d="M16 5 27 16 16 27 5 16Z" fill="#60a5fa"/>'
        "</svg>",
        content_type="image/svg+xml",
    )


def _parts_for_filter(logs=None):
    """Parts offered in the part filter of the Dashboard / Report pages."""
    if logs is not None:
        part_numbers = list(
            logs.exclude(part_number__isnull=True)
            .exclude(part_number="")
            .values_list("part_number", flat=True)
            .distinct()
            .order_by("part_number")
        )
        names = dict(
            VisionInspectionPart.objects.filter(
                part_number__in=part_numbers
            ).values_list("part_number", "part_name")
        )
        return [
            {"part_number": number, "part_name": names.get(number) or ""}
            for number in part_numbers
        ]
    return [
        {"part_number": part.part_number, "part_name": part.part_name or ""}
        for part in VisionInspectionPart.objects.all().order_by("part_number")
    ]


def dashboard(request):
    """Airbag Stitch Inspection overview with live counts grouped by part.

    Every displayed number is aggregated from the inspection records stored in
    the database; the optional part filter restricts the summary cards and
    charts alike.
    """
    filters = records.read_filters(request.GET)
    from VisionInspection.services.access_control import scope_logs

    accessible_logs = scope_logs(request.user, VisionInspectionLog.objects.all())
    logs = records.filter_logs(accessible_logs, filters)
    summary = records.summarize(logs)

    can_view_master_data = has_permission(request.user, "view_master_data")
    parts = _parts_for_filter(
        None if can_view_master_data else accessible_logs
    )
    part_names = {part["part_number"]: part["part_name"] for part in parts}
    grouped_parts = list(
        logs.values("part_number")
        .annotate(
            total=Count("pk"),
            ok=Count("pk", filter=Q(result=records.RESULT_OK)),
            not_ok=Count("pk", filter=Q(result=records.RESULT_NOT_OK)),
        )
        .order_by("-total", "part_number")[:8]
    )
    chart_max = max((row["total"] for row in grouped_parts), default=0)
    classified_total = summary["ok"] + summary["not_ok"]
    part_rows = [
        {
            "part_number": row["part_number"] or "Unknown",
            "part_name": part_names.get(row["part_number"] or "", ""),
            "total": row["total"],
            "ok": row["ok"],
            "not_ok": row["not_ok"],
            "ok_width": round(100 * row["ok"] / chart_max, 1) if chart_max else 0,
            "not_ok_width": (
                round(100 * row["not_ok"] / chart_max, 1) if chart_max else 0
            ),
        }
        for row in grouped_parts
    ]
    trend_rows = []
    trend_ok_points = ""
    trend_not_ok_points = ""
    trend_has_results = False
    latest_timestamp = logs.order_by("-timestamp", "-pk").values_list(
        "timestamp", flat=True
    ).first()
    if latest_timestamp:
        current_tz = timezone.get_current_timezone()
        trend_end = timezone.localtime(latest_timestamp, current_tz).date()
        trend_start = trend_end - timedelta(days=13)
        trend_start_at = timezone.make_aware(
            datetime.combine(trend_start, time.min), current_tz
        )
        trend_end_at = timezone.make_aware(
            datetime.combine(trend_end + timedelta(days=1), time.min), current_tz
        )
        daily_results = (
            logs.filter(
                timestamp__gte=trend_start_at,
                timestamp__lt=trend_end_at,
            )
            .annotate(day=TruncDate("timestamp", tzinfo=current_tz))
            .values("day")
            .annotate(
                ok=Count("pk", filter=Q(result=records.RESULT_OK)),
                not_ok=Count("pk", filter=Q(result=records.RESULT_NOT_OK)),
            )
        )
        daily_by_date = {row["day"]: row for row in daily_results}
        trend_max = max(
            (
                max(row["ok"], row["not_ok"])
                for row in daily_by_date.values()
            ),
            default=0,
        )
        trend_has_results = trend_max > 0
        ok_coordinates = []
        not_ok_coordinates = []
        for index in range(14):
            day = trend_start + timedelta(days=index)
            result_row = daily_by_date.get(day, {})
            ok_count = result_row.get("ok", 0)
            not_ok_count = result_row.get("not_ok", 0)
            x = round(38 + index * (550 / 13), 1)
            ok_y = round(126 - (ok_count / trend_max * 96), 1) if trend_max else 126
            not_ok_y = (
                round(126 - (not_ok_count / trend_max * 96), 1)
                if trend_max
                else 126
            )
            ok_coordinates.append(f"{x},{ok_y}")
            not_ok_coordinates.append(f"{x},{not_ok_y}")
            trend_rows.append(
                {
                    "date": day,
                    "label": day.strftime("%d %b"),
                    "ok": ok_count,
                    "not_ok": not_ok_count,
                    "x": x,
                    "ok_y": ok_y,
                    "not_ok_y": not_ok_y,
                    "show_label": index % 2 == 0 or index == 13,
                }
            )
        trend_ok_points = " ".join(ok_coordinates)
        trend_not_ok_points = " ".join(not_ok_coordinates)
        trend_grid = [
            {
                "value": value,
                "y": round(126 - (value / trend_max * 96), 1) if trend_max else 126,
            }
            for value in sorted({0, round(trend_max / 2), trend_max})
        ]
    else:
        trend_grid = []

    show_recent_inspections = has_permission(
        request.user, "view_inspection_reports"
    )
    recent_rows = []
    if show_recent_inspections:
        recent_logs = (
            logs.select_related("session", "session__operator")
            .order_by("-timestamp", "-pk")[: records.RECENT_LIMIT]
        )
        recent_rows = records.build_rows(recent_logs, parts=part_names)
        for row in recent_rows:
            session = row["session"]
            row["employee_or_station"] = (
                row["employee_label"]
                if row["employee_code"]
                else (
                    (session.employee_code if session else "")
                    or (
                        session.operator.get_username()
                        if session and session.operator_id
                        else ""
                    )
                    or (session.station_id if session else "")
                    or "—"
                )
            )

    context = {
        "sidebar_active": "dashboard",
        "filters": filters,
        "selected_part": filters["part"],
        "parts": parts,
        "summary": summary,
        "classified_total": classified_total,
        "donut_ok_degrees": (
            round(360 * summary["ok"] / summary["total"], 1)
            if summary["total"]
            else 0
        ),
        "donut_classified_degrees": (
            round(360 * classified_total / summary["total"], 1)
            if summary["total"]
            else 0
        ),
        "part_rows": part_rows,
        "trend_rows": trend_rows,
        "trend_ok_points": trend_ok_points,
        "trend_not_ok_points": trend_not_ok_points,
        "trend_ok_total": sum(point["ok"] for point in trend_rows),
        "trend_not_ok_total": sum(point["not_ok"] for point in trend_rows),
        "trend_has_results": trend_has_results,
        "trend_grid": trend_grid,
        "trend_period_start": trend_start if latest_timestamp else None,
        "trend_period_end": trend_end if latest_timestamp else None,
        "recent_rows": recent_rows,
        "show_recent_inspections": show_recent_inspections,
        "can_view_reports": show_recent_inspections,
        "chart_max": chart_max,
        "chart_mid": max(1, round(chart_max / 2)) if chart_max else 0,
    }
    return render(request, "VisionInspection/ui/dashboard.html", context)


# --------------------------------------------------------------------------- #
#  Master — Employee / Part / YOLO / Preset
# --------------------------------------------------------------------------- #


def employee_list(request):
    q = request.GET.get("q", "")
    is_active = request.GET.get("is_active", "")
    employees = VisionInspectionEmployee.objects.all()
    if q:
        employees = employees.filter(
            Q(employee_code__icontains=q) | Q(employee_name__icontains=q)
        )
    if is_active in ("1", "true", "True"):
        employees = employees.filter(is_active=True)
    elif is_active in ("0", "false", "False"):
        employees = employees.filter(is_active=False)
    employees = employees.order_by("employee_name")
    context = {
        "employees": employees,
        "q": q,
        "is_active": is_active,
        "sidebar_active": "employees",
        "can_manage_employees": (
            request.user.is_authenticated
            and request.user.is_active
            and (
                is_admin(request.user)
                or has_permission(request.user, "create_master_data")
                or has_permission(request.user, "change_master_data")
            )
        ),
    }
    return render(request, "VisionInspection/ui/employee_list.html", context)


def part_list(request):
    q = request.GET.get("q", "")
    is_active = request.GET.get("is_active", "")
    parts = VisionInspectionPart.objects.all()
    if not request.user.is_authenticated:
        parts = parts.filter(is_active=True)
    if q:
        parts = parts.filter(Q(part_number__icontains=q) | Q(part_name__icontains=q))
    if is_active in ("1", "true", "True"):
        parts = parts.filter(is_active=True)
    elif is_active in ("0", "false", "False"):
        parts = parts.filter(is_active=False)
    parts = parts.order_by("part_number")
    context = {
        "parts": parts,
        "q": q,
        "is_active": is_active,
        "sidebar_active": "parts",
        "can_manage_parts": (
            request.user.is_authenticated
            and request.user.is_active
            and (
                is_admin(request.user)
                or has_permission(request.user, "create_master_data")
                or has_permission(request.user, "change_master_data")
            )
        ),
    }
    return render(request, "VisionInspection/ui/part_list.html", context)


def part_vision_configuration(request, part_id):
    part = get_object_or_404(VisionInspectionPart, pk=part_id)
    configuration = (
        VisionPartConfiguration.objects.filter(part=part)
        .order_by("-version")
        .first()
    )
    return render(
        request,
        "VisionInspection/ui/part_vision_configuration.html",
        {
            "part": part,
            "configuration": configuration,
            "sidebar_active": "parts",
        },
    )


def part_roi_setup(request, part_id):
    """Dedicated ROI setup screen for the ONE common ROI of a part.

    This is the only place where an ROI can be drawn or changed.  The operator
    Inspection screen never shows ROI editing controls or coordinates.
    """
    part = get_object_or_404(VisionInspectionPart, pk=part_id)
    configuration = (
        VisionPartConfiguration.objects.filter(part=part)
        .order_by("-version")
        .first()
    )
    from VisionInspection.services.camera_service import get_camera_capabilities
    from VisionInspection.services.part_configuration import active_roi

    capabilities = get_camera_capabilities()
    # Camera defaults come from the configuration itself (never invented).
    front_source = getattr(configuration, "front_camera_source", "OPENCV")
    back_source = getattr(configuration, "back_camera_source", "OPENCV")
    back_only = bool(configuration and configuration.requires_back and not configuration.requires_front)
    compatibility_error = common_roi_camera_error(configuration)
    if configuration is None:
        compatibility_error = (
            "Save a model configuration before configuring a common ROI."
        )
    selected_source = back_source if back_only else front_source
    selected_index = (
        getattr(configuration, "back_camera_index", 1)
        if back_only
        else getattr(configuration, "front_camera_index", 0)
    )
    return render(
        request,
        "VisionInspection/ui/roi_setup.html",
        {
            "part": part,
            "configuration": configuration,
            "roi": active_roi(configuration),
            "sidebar_active": "roi_setup",
            "camera_capabilities": capabilities,
            "camera_source": selected_source,
            "camera_index": selected_index,
            "front_camera_source": front_source,
            "front_camera_index": getattr(configuration, "front_camera_index", 0),
            "back_camera_source": back_source,
            "back_camera_index": getattr(configuration, "back_camera_index", 1),
            "camera_compatibility_error": compatibility_error,
        },
    )


def yolo_model_list(request):
    q = request.GET.get("q", "")
    is_active = request.GET.get("is_active", "")
    models = VisionYOLOModel.objects.all()
    if q:
        models = models.filter(
            Q(name__icontains=q) | Q(version__icontains=q) | Q(part_class__icontains=q)
        )
    if is_active in ("1", "true", "True"):
        models = models.filter(is_active=True)
    elif is_active in ("0", "false", "False"):
        models = models.filter(is_active=False)
    models = models.order_by("-created_at")
    context = {
        "models": models,
        "q": q,
        "is_active": is_active,
        "sidebar_active": "yolo_models",
    }
    return render(request, "VisionInspection/ui/yolo_model_list.html", context)


def preset_list(request):
    q = request.GET.get("q", "")
    presets = VisionInspectionPreset.objects.all()
    if q:
        presets = presets.filter(Q(name__icontains=q) | Q(part_number__icontains=q))
    presets = presets.order_by("-created_at")
    context = {
        "presets": presets,
        "q": q,
        "sidebar_active": "presets",
    }
    return render(request, "VisionInspection/ui/preset_list.html", context)


# --------------------------------------------------------------------------- #
#  Vision Inspection — State / Logs / ROI / Trigger Zone
# --------------------------------------------------------------------------- #


def state_list(request):
    state = VisionInspectionState.objects.first()
    return render(
        request,
        "VisionInspection/ui/state_list.html",
        {"state": state, "sidebar_active": "state"},
    )


def log_list(request):
    logs = VisionInspectionLog.objects.all()
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    employee = request.GET.get("employee", "")
    part_number = request.GET.get("part_number", "")
    result = request.GET.get("result", "")

    if date_from:
        try:
            logs = logs.filter(
                timestamp__date__gte=datetime.strptime(date_from, "%Y-%m-%d").date()
            )
        except ValueError:
            pass
    if date_to:
        try:
            logs = logs.filter(
                timestamp__date__lte=datetime.strptime(date_to, "%Y-%m-%d").date()
            )
        except ValueError:
            pass
    if employee:
        logs = logs.filter(
            Q(employee_code__icontains=employee) | Q(employee_name__icontains=employee)
        )
    if part_number:
        logs = logs.filter(part_number__icontains=part_number)
    if result:
        logs = logs.filter(result=result)

    logs = logs.order_by("-timestamp")[:500]
    context = {
        "logs": logs,
        "date_from": date_from,
        "date_to": date_to,
        "employee": employee,
        "part_number": part_number,
        "result": result,
        "sidebar_active": "logs",
    }
    return render(request, "VisionInspection/ui/log_list.html", context)


def roi_list(request):
    q = request.GET.get("q", "")
    preset_id = request.GET.get("preset_id", "")
    rois = VisionROI.objects.all().select_related("preset")
    if q:
        rois = rois.filter(Q(roi_name__icontains=q) | Q(expected_class__icontains=q))
    if preset_id:
        rois = rois.filter(preset_id=preset_id)
    rois = rois.order_by("preset", "sequence")
    context = {
        "rois": rois,
        "q": q,
        "preset_id": preset_id,
        "presets": VisionInspectionPreset.objects.all(),
        "sidebar_active": "rois",
    }
    return render(request, "VisionInspection/ui/roi_list.html", context)


def trigger_zone_list(request):
    zones = VisionTriggerZone.objects.all().select_related("preset")
    context = {
        "zones": zones,
        "sidebar_active": "trigger_zones",
    }
    return render(request, "VisionInspection/ui/trigger_zone_list.html", context)


# --------------------------------------------------------------------------- #
#  AI / Models
# --------------------------------------------------------------------------- #


def training_job_list(request):
    jobs = VisionTrainingJob.objects.all().order_by("-created_at")
    context = {
        "jobs": jobs,
        "sidebar_active": "training_jobs",
    }
    return render(request, "VisionInspection/ui/training_job_list.html", context)


# --------------------------------------------------------------------------- #
#  Reports
# --------------------------------------------------------------------------- #


def _report_employees():
    """Employees offered in the report filter.

    The employee master is the source; codes that appear on stored records
    without a master entry (e.g. the station identifier) are listed as well so
    no real record becomes unreachable.
    """
    options = []
    seen = set()
    for employee in VisionInspectionEmployee.objects.all().order_by("employee_code"):
        label = f"{employee.employee_code} — {employee.employee_name}"
        options.append({"code": employee.employee_code, "label": label})
        seen.add(employee.employee_code.casefold())
    for code in (
        VisionInspectionLog.objects.exclude(employee_code__isnull=True)
        .exclude(employee_code="")
        .values_list("employee_code", flat=True)
        .distinct()
        .order_by("employee_code")
    ):
        if code.casefold() in seen:
            continue
        options.append({"code": code, "label": code})
        seen.add(code.casefold())
    return options


def _page_links(page_obj, window=2):
    """Page numbers for the report pagination — ``None`` marks an ellipsis."""
    total = page_obj.paginator.num_pages
    current = page_obj.number
    if total <= 9:
        return list(range(1, total + 1))
    pages = {1, total}
    pages.update(range(max(1, current - window), min(total, current + window) + 1))

    links = []
    previous = 0
    for number in sorted(pages):
        if number - previous > 1:
            links.append(None)
        links.append(number)
        previous = number
    return links


def reports(request):
    """Inspection Report — filtered inspection records, summary and exports.

    Filters are applied on the backend, the summary cards and the table always
    use the same selection, and the newest records come first.
    """
    filters = records.read_filters(request.GET)
    from VisionInspection.services.access_control import scope_logs
    logs = scope_logs(request.user, records.filter_logs(None, filters))
    ordered = logs.order_by("-timestamp", "-pk")

    paginator = Paginator(ordered, records.read_page_size(request.GET))
    page_obj = paginator.get_page(request.GET.get("page"))
    rows = records.build_rows(page_obj.object_list)

    result_options = [
        {"value": "", "label": "All Results"},
        {"value": records.RESULT_OK, "label": "OK"},
        {"value": records.RESULT_NOT_OK, "label": "NOT OK"},
        {"value": records.RESULT_INCOMPLETE_ERROR, "label": "Incomplete / Error"},
    ]
    side_result_options = [
        {"value": "", "label": "All"},
        {"value": records.RESULT_OK, "label": "OK"},
        {"value": records.RESULT_NOT_OK, "label": "NOT OK"},
        {"value": records.RESULT_INCOMPLETE_ERROR, "label": "Incomplete / Error"},
    ]

    context = {
        "sidebar_active": "reports",
        "filters": filters,
        "filters_active": bool(records.active_filters(filters)),
        "page_sizes": records.PAGE_SIZES,
        "page_size": paginator.per_page,
        "parts": _parts_for_filter(),
        "employees": _report_employees(),
        "result_options": result_options,
        "side_result_options": side_result_options,
        "summary": records.summarize(logs),
        "recent_rows": rows,
        "print_rows": records.build_rows(list(ordered)),
        "page_obj": page_obj,
        "page_links": _page_links(page_obj),
        "page_first": page_obj.start_index if page_obj.object_list else 0,
        "page_last": page_obj.end_index if page_obj.object_list else 0,
        "page_total": paginator.count,
        "export_query": records.query_string(filters),
    }
    return render(request, "VisionInspection/ui/reports.html", context)


def _report_export_rows(request):
    """Filtered export data shared by the Excel and PDF exports."""
    filters = records.read_filters(request.GET)
    from VisionInspection.services.access_control import scope_logs
    logs = scope_logs(request.user, records.filter_logs(None, filters)).order_by("-timestamp", "-pk")
    rows = records.export_rows(records.build_rows(list(logs)))
    summary = records.summarize(scope_logs(request.user, records.filter_logs(None, filters)))
    meta = report_exports.meta_lines(filters, summary, len(rows))
    return filters, meta, rows, summary


def report_export_excel(request):
    """Export the records matching the active filters as an Excel workbook."""
    filters, meta, rows, summary = _report_export_rows(request)
    logger.info(
        "Inspection report Excel export — %d records, filters=%s",
        len(rows), records.active_filters(filters),
    )
    return report_exports.excel_response(
        "Inspection Report", meta, records.export_headers(), rows
    )


def report_export_pdf(request):
    """Export the records matching the active filters as a PDF document."""
    filters, meta, rows, summary = _report_export_rows(request)
    logger.info(
        "Inspection report PDF export — %d records, filters=%s",
        len(rows), records.active_filters(filters),
    )
    return report_exports.pdf_response(
        "Inspection Report",
        meta,
        records.export_headers(),
        report_exports.COLUMN_WEIGHTS,
        rows,
    )


# --------------------------------------------------------------------------- #
#  Settings
# --------------------------------------------------------------------------- #


def settings_page(request):
    """Render the authorized server-side local storage manager."""
    from VisionInspection.services import storage_service

    can_browse = has_permission(request.user, "view_storage_settings")
    can_operate = has_permission(request.user, "change_storage_settings")
    context = {
        "storage_roots": storage_service.storage_manager_roots() if can_browse else [],
        "can_browse_storage": can_browse,
        "can_manage_storage": can_operate,
        "can_manage_storage_roots": is_admin(request.user),
        "sidebar_active": "settings",
    }
    return render(request, "VisionInspection/ui/settings.html", context)


def storage_settings(request):
    """Storage destination and historical image migration controls."""
    from django.middleware.csrf import get_token
    from VisionInspection.services.storage_service import storage_state

    return render(
        request,
        "VisionInspection/ui/storage_settings.html",
        {
            "sidebar_active": "storage",
            "storage_state": storage_state(),
            "csrf_token": get_token(request),
            "can_change_storage": (
                is_admin(request.user)
                or has_permission(request.user, "change_storage_settings")
            ),
        },
    )


def _django_version():
    try:
        import django

        return django.get_version()
    except Exception:
        return "unknown"


def _admin_available(get_resolver):
    try:
        get_resolver().reverse("admin:index")
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
#  Inspection Traceability
# --------------------------------------------------------------------------- #

_RESULT_BADGE = {
    "OK": "vi-badge-ok",
    "NOT_OK": "vi-badge-not-ok",
    "ERROR": "vi-badge-error",
    "INCOMPLETE": "vi-badge-pending",
}


def _log_has_evidence(log) -> bool:
    """True when the record has a stored original inspection image."""
    return bool(getattr(log, "inspection_image", None))


def traceability(request):
    """Searchable, paginated Inspection Traceability register.

    Shows every finalized inspection event with the ROI configuration that was
    used and whether evidence was stored.  Results are pulled from the real
    ``VisionInspectionLog`` table — nothing is fabricated.
    """
    q = request.GET.get("q", "").strip()
    result = request.GET.get("result", "").strip().upper()
    page = request.GET.get("page", 1)

    from VisionInspection.services.access_control import scope_logs, can_view_log
    logs = scope_logs(request.user, VisionInspectionLog.objects.all())
    if q:
        logs = logs.filter(
            Q(part_number__icontains=q)
            | Q(employee_code__icontains=q)
            | Q(preset_name__icontains=q)
        )
    if result in ("OK", "NOT_OK", "INCOMPLETE", "ERROR"):
        logs = logs.filter(result=result)

    paginator = Paginator(logs.order_by("-timestamp"), 15)
    try:
        page_obj = paginator.get_page(page)
    except Exception:
        page_obj = paginator.get_page(1)

    context = {
        "sidebar_active": "traceability",
        "page_obj": page_obj,
        "logs": page_obj.object_list,
        "total": paginator.count,
        "q": q,
        "result": result,
        "badge_map": _RESULT_BADGE,
    }
    return render(request, "VisionInspection/ui/traceability.html", context)


def traceability_detail(request, inspection_id):
    """Detail view for a single inspection with whatever evidence exists.

    Only real stored data is displayed: the original inspection image (if the
    workflow saved it), the ROI configuration snapshot used at inspection time,
    prediction metadata, and the model name/version.  Cropped-ROI and annotated
    images are only referenced when they were actually captured; otherwise their
    absence is stated plainly rather than substituting another image.
    """
    from django.core.exceptions import PermissionDenied
    from django.utils.http import url_has_allowed_host_and_scheme
    from VisionInspection.services.access_control import can_view_log
    log = get_object_or_404(VisionInspectionLog.objects.select_related("session"), pk=inspection_id)
    if not can_view_log(request.user, log):
        raise PermissionDenied
    from VisionInspection.services.storage_service import image_url, metadata_for_field

    def evidence_reference(instance, field_name, fallback_timestamp=None):
        metadata = metadata_for_field(instance, field_name)
        url = image_url(instance, field_name)
        filename = ""
        storage_path = ""
        storage_backend = ""
        captured_at = fallback_timestamp
        if metadata:
            filename = metadata.relative_path.replace("\\", "/").rsplit("/", 1)[-1]
            captured_at = metadata.captured_at or captured_at
            storage_backend = metadata.get_storage_backend_display()
            if metadata.storage_backend == "NETWORK" and metadata.destination_id:
                destination = metadata.destination
                path_parts = [
                    destination.destination_directory.strip("/\\"),
                    metadata.relative_path.replace("\\", "/").strip("/"),
                ]
                storage_path = destination.get_full_remote_path().rstrip("/\\")
                suffix = "/".join(part for part in path_parts if part)
                if suffix:
                    storage_path = f"{storage_path}/{suffix}"
            else:
                storage_path = str(metadata.get_resolved_path())
        return {
            "url": url,
            "download_url": f"{url}?download=1" if url else "",
            "filename": filename,
            "captured_at": captured_at,
            "storage_path": storage_path,
            "storage_backend": storage_backend,
        }

    # ROI configuration snapshot preserved with this record.
    roi_config = []
    for roi in (log.roi_results or []):
        roi_config.append(
            {
                "roi_name": roi.get("roi_name", "ROI"),
                "result": roi.get("result", ""),
                "details": roi.get("details", "") or roi.get("expected", ""),
            }
        )

    session = log.session
    if session is None and log.preset_name and log.preset_name.startswith("SESSION#"):
        from VisionInspection.models import VisionInspectionSession
        session = VisionInspectionSession.objects.filter(session_id=log.preset_name[8:]).first()
    sides = []
    if session:
        for side, title in (("front", "Front"), ("back", "Back")):
            completed_at = getattr(session, f"{side}_completed_at", None)
            original = evidence_reference(session, f"{side}_image", completed_at)
            annotated = evidence_reference(session, f"{side}_annotated", completed_at)
            roi_image = evidence_reference(session, f"{side}_roi_image", completed_at)
            sides.append({
                "key": side, "title": title,
                "result": getattr(session, f"{side}_verdict", "") or "INCOMPLETE",
                "status": getattr(session, f"{side}_status", "") or "INCOMPLETE",
                "captured_at": original["captured_at"],
                "detections": getattr(session, f"{side}_detections", []) or [],
                "results": [
                    {
                        **item,
                        "stitch_label": item.get("stitch") or item.get("class") or "?",
                        "actual_label": item.get("actual_count", item.get("count", "?")),
                        "expected_label": item.get("expected_count", item.get("expected", "?")),
                    }
                    for item in (getattr(session, f"{side}_results", []) or [])
                    if isinstance(item, dict)
                ],
                "original_url": original["url"],
                "original_download_url": original["download_url"],
                "original_filename": original["filename"],
                "original_captured_at": original["captured_at"],
                "original_storage_path": original["storage_path"],
                "original_storage_backend": original["storage_backend"],
                "annotated_url": annotated["url"],
                "annotated_filename": annotated["filename"],
                "annotated_captured_at": annotated["captured_at"],
                "annotated_storage_path": annotated["storage_path"],
                "roi_url": roi_image["url"],
                "roi_filename": roi_image["filename"],
                "roi_captured_at": roi_image["captured_at"],
                "roi_storage_path": roi_image["storage_path"],
            })
    detections = log.detected_objects or []
    evidence = {"original": bool(log.inspection_image), "cropped_roi": False, "annotated": False}
    legacy_image = evidence_reference(log, "inspection_image", log.timestamp)
    model_configured = bool(log.yolo_model_name)
    context = {
        "sidebar_active": "traceability", "log": log, "session": session,
        "inspection_image_url": legacy_image["url"],
        "inspection_image_download_url": legacy_image["download_url"],
        "inspection_image_filename": legacy_image["filename"],
        "inspection_image_captured_at": legacy_image["captured_at"],
        "inspection_image_storage_path": legacy_image["storage_path"],
        "result_badge": _RESULT_BADGE.get(log.result, "vi-badge-skip"),
        "roi_config": roi_config, "detections": detections,
        "detections_count": len(detections), "evidence": evidence,
        "model_configured": model_configured, "part_bbox": log.part_bbox,
        "trigger_zone": log.trigger_zone_polygon, "sides": sides,
        "back_url": request.GET.get("next", ""),
        "part": VisionInspectionPart.objects.filter(part_number=log.part_number).first(),
        "employee": VisionInspectionEmployee.objects.filter(employee_code=log.employee_code).first(),
        "employee_display": (
            (VisionInspectionEmployee.objects.filter(employee_code=log.employee_code).values_list("employee_name", flat=True).first() or "")
            or (session.operator.get_full_name() if session and session.operator_id else "")
        ),
    }
    requested_back = request.GET.get("next", "")
    if requested_back and url_has_allowed_host_and_scheme(requested_back, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        context["back_url"] = requested_back
    else:
        context["back_url"] = reverse("VisionInspection:ui-reports")
    return render(request, "VisionInspection/ui/traceability_detail.html", context)
