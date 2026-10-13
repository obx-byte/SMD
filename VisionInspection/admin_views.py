"""Admin views for the VisionInspection application."""

import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.shortcuts import render

from VisionInspection.models import (
    VisionInspectionEmployee,
    VisionInspectionLog,
    VisionInspectionPart,
    VisionInspectionPreset,
    VisionInspectionState,
    VisionROI,
    VisionTrainingJob,
    VisionTriggerZone,
    VisionYOLOModel,
)

logger = logging.getLogger(__name__)


def _get_dashboard_counts():
    return {
        "employees": VisionInspectionEmployee.objects.filter(is_active=True).count(),
        "parts": VisionInspectionPart.objects.filter(is_active=True).count(),
        "presets": VisionInspectionPreset.objects.filter(is_active=True).count(),
        "logs": VisionInspectionLog.objects.count(),
        "state": VisionInspectionState.objects.first(),
        "training_jobs": VisionTrainingJob.objects.count(),
    }


def dashboard(request):
    counts = _get_dashboard_counts()
    counts["sidebar_active"] = "dashboard"
    return render(request, "VisionInspection/admin/dashboard.html", counts)


# --------------------------------------------------------------------------- #
# Master
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
    }
    return render(request, "VisionInspection/admin/employee_list.html", context)


def part_list(request):
    q = request.GET.get("q", "")
    is_active = request.GET.get("is_active", "")
    parts = VisionInspectionPart.objects.all()
    if q:
        parts = parts.filter(
            Q(part_number__icontains=q) | Q(part_name__icontains=q)
        )
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
    }
    return render(request, "VisionInspection/admin/part_list.html", context)


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
    return render(request, "VisionInspection/admin/yolo_model_list.html", context)


def preset_list(request):
    q = request.GET.get("q", "")
    presets = VisionInspectionPreset.objects.all()
    if q:
        presets = presets.filter(
            Q(name__icontains=q) | Q(part_number__icontains=q)
        )
    presets = presets.order_by("-created_at")
    context = {
        "presets": presets,
        "q": q,
        "sidebar_active": "presets",
    }
    return render(request, "VisionInspection/admin/preset_list.html", context)


# --------------------------------------------------------------------------- #
# Vision Inspection
# --------------------------------------------------------------------------- #


def state_list(request):
    state = VisionInspectionState.objects.first()
    return render(request, "VisionInspection/admin/state_list.html", {"state": state, "sidebar_active": "state"})


def log_list(request):
    logs = VisionInspectionLog.objects.all()
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    employee = request.GET.get("employee", "")
    part_number = request.GET.get("part_number", "")
    result = request.GET.get("result", "")

    if date_from:
        try:
            logs = logs.filter(timestamp__date__gte=datetime.strptime(date_from, "%Y-%m-%d").date())
        except ValueError:
            pass
    if date_to:
        try:
            logs = logs.filter(timestamp__date__lte=datetime.strptime(date_to, "%Y-%m-%d").date())
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
    return render(request, "VisionInspection/admin/log_list.html", context)


def roi_list(request):
    q = request.GET.get("q", "")
    preset_id = request.GET.get("preset_id", "")
    rois = VisionROI.objects.all().select_related("preset")
    if q:
        rois = rois.filter(
            Q(roi_name__icontains=q) | Q(expected_class__icontains=q)
        )
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
    return render(request, "VisionInspection/admin/roi_list.html", context)


def trigger_zone_list(request):
    zones = VisionTriggerZone.objects.all().select_related("preset")
    context = {
        "zones": zones,
        "sidebar_active": "trigger_zones",
    }
    return render(request, "VisionInspection/admin/trigger_zone_list.html", context)


# --------------------------------------------------------------------------- #
# AI / Model
# --------------------------------------------------------------------------- #


def training_job_list(request):
    jobs = VisionTrainingJob.objects.all().order_by("-created_at")
    context = {
        "jobs": jobs,
        "sidebar_active": "training_jobs",
    }
    return render(request, "VisionInspection/admin/training_job_list.html", context)


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


def reports(request):
    logs = VisionInspectionLog.objects.all()
    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")
    employee = request.GET.get("employee", "")
    part_number = request.GET.get("part_number", "")
    result = request.GET.get("result", "")
    preset = request.GET.get("preset", "")

    if date_from:
        try:
            logs = logs.filter(timestamp__date__gte=datetime.strptime(date_from, "%Y-%m-%d").date())
        except ValueError:
            pass
    if date_to:
        try:
            logs = logs.filter(timestamp__date__lte=datetime.strptime(date_to, "%Y-%m-%d").date())
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
    if preset:
        logs = logs.filter(preset_name__icontains=preset)

    total = logs.count()
    ok_count = logs.filter(result="OK").count()
    not_ok_count = logs.filter(result="NOT_OK").count()
    error_count = logs.filter(result="ERROR").count()
    ok_pct = round((ok_count / total) * 100, 1) if total else 0.0
    not_ok_pct = round((not_ok_count / total) * 100, 1) if total else 0.0

    context = {
        "logs": logs[:200],
        "total": total,
        "ok_count": ok_count,
        "not_ok_count": not_ok_count,
        "error_count": error_count,
        "ok_pct": ok_pct,
        "not_ok_pct": not_ok_pct,
        "date_from": date_from,
        "date_to": date_to,
        "employee": employee,
        "part_number": part_number,
        "result": result,
        "preset": preset,
        "sidebar_active": "reports",
    }
    return render(request, "VisionInspection/admin/reports.html", context)


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #


def system_status(request):
    return render(
        request,
        "VisionInspection/admin/system_status.html",
        {
            "django_version": __import__("django").get_version(),
            "python_version": __import__("sys").version,
            "debug": settings.DEBUG,
            "sidebar_active": "system_status",
        },
    )


def camera_status(request):
    return render(request, "VisionInspection/admin/camera_status.html", {"sidebar_active": "camera_status"})


def settings_view(request):
    relevant = {}
    for key in [
        "VISION_INSPECTION_DEFAULT_PLANT",
        "VISION_INSPECTION_CAMERA_INDEX",
        "VISION_INSPECTION_YOLO_MODEL_PATH",
        "VISION_INSPECTION_INFERENCE_FPS",
        "VISION_INSPECTION_DEFAULT_STABLE_FRAMES",
        "VISION_INSPECTION_TRIGGER_COOLDOWN",
    ]:
        relevant[key] = getattr(settings, key, None)
    return render(request, "VisionInspection/admin/settings.html", {"settings": relevant, "sidebar_active": "settings"})
