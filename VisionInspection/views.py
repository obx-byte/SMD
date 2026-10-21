"""HTTP views for the VisionInspection application.

This module contains three kinds of views:

1. **Page views** — render the operator/setup templates (live inspection,
   inspection master, configuration, preset and history pages).
2. **JSON API views** — master-data CRUD (employees, parts, presets, ROIs,
   trigger zones, YOLO models), camera control and the inspection workflows.
3. **Workflow endpoints** — thin, stateless wrappers over the singleton
   services in ``VisionInspection.services``.

Design rules followed throughout:

* Responses are JSON with a stable shape; validation failures return HTTP 400
  and a ``fields`` map so the UI can highlight the offending inputs.
* Nothing is fabricated: when the camera is off, a model is missing, a branch
  is not configured or a broker is unavailable, the *real* reason is returned
  instead of a plausible-looking value.
* Camera capture / inference live in the worker threads of the services, so
  request handlers never block on device I/O.
"""

import base64
import binascii
import io
import json
import logging
import math
import os
import time
import uuid
from pathlib import Path

from django.core.paginator import Paginator
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import (
    Http404,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, render
from django.utils.text import get_valid_filename
from django.views.decorators.http import require_GET, require_POST

from VisionInspection.models import (
    VisionInspectionCameraConfig,
    VisionInspectionEmployee,
    VisionInspectionLog,
    VisionInspectionMasterROI,
    VisionInspectionPart,
    VisionInspectionPreset,
    VisionInspectionSession,
    VisionROI,
    VisionClassConfiguration,
    VisionPartConfiguration,
    VisionPartModel,
    VisionPartROI,
    VisionTrainingJob,
    VisionTriggerZone,
    VisionYOLOModel,
    normalize_token,
)
from VisionInspection.services.camera_service import (
    CAMERA_SOURCES,
    CAMERA_STATES,
    CameraService,
    get_camera_capabilities,
)
from VisionInspection.services.continuous_inspection import ContinuousInspectionEngine
from VisionInspection.services.inspection_workflow import InspectionWorkflow
from VisionInspection.services.part_configuration import (
    active_roi,
    common_roi_camera_error,
    validate_configuration,
    validate_data_yaml,
)
from VisionInspection.services.model_registry import (
    ModelLoadError,
    ModelRuntimeUnavailableError,
    registry,
)
from VisionInspection.services.model_paths import (
    ModelPathResolutionError,
    model_storage_root,
    resolve_model_path,
)
from VisionInspection.services.yolo_service import YOLOModelService

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 15
MAX_PAGE_SIZE = 500


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #


def _json_response(payload, status=200):
    """Return a JSON response that accepts lists as well as dicts."""
    return JsonResponse(payload, status=status, safe=False)


def _body_json(request):
    """Parse a JSON request body, tolerating form posts and empty bodies."""
    if request.method in ("POST", "PUT", "PATCH") and request.body:
        try:
            data = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    if request.POST:
        return {key: request.POST.get(key) for key in request.POST.keys()}
    return {}


def _clean(value):
    return str(value or "").strip()


def _as_int(value, default=0):
    try:
        return int(float(value))
    except (OverflowError, TypeError, ValueError):
        return default


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pagination_params(request):
    page = _as_int(request.GET.get("page"), 1) or 1
    page_size = _as_int(request.GET.get("page_size"), DEFAULT_PAGE_SIZE) or DEFAULT_PAGE_SIZE
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    return page, page_size


def _error(message, fields=None, status=400):
    payload = {"ok": False, "error": message}
    if fields:
        payload["fields"] = fields
    return _json_response(payload, status=status)


def _active_model():
    """The single active inspection model, or ``None`` (never fabricated)."""
    return (
        VisionYOLOModel.objects.filter(is_active=True)
        .order_by("-updated_at")
        .first()
    )


def _parts_for_select():
    """Parts as ``{part_number, part_desc}`` for the UI <select> controls."""
    return [
        {
            "part_number": p.part_number,
            "part_desc": p.part_name or "",
            "is_active": p.is_active,
        }
        for p in VisionInspectionPart.objects.all().order_by("part_number")
    ]


def _workflow():
    return InspectionWorkflow()


def _camera_config_for(view):
    """Active one-time camera ROI configuration for *view* (or ``None``)."""
    return (
        VisionInspectionCameraConfig.objects.filter(view=view, is_active=True)
        .order_by("-version")
        .first()
    )


def _roi_pixels_from_config(config):
    """Pixel ROI (x, y, w, h, ref_w, ref_h) for a camera configuration."""
    if config is None:
        return None
    return {
        "x": round(config.roi_x * config.frame_width),
        "y": round(config.roi_y * config.frame_height),
        "width": round(config.roi_w * config.frame_width),
        "height": round(config.roi_h * config.frame_height),
        "ref_width": int(config.frame_width),
        "ref_height": int(config.frame_height),
    }


def _roi_pixels_from_part(part_number, view):
    """Pixel ROI for a saved per-part ``VisionInspectionMasterROI``."""
    roi = VisionInspectionMasterROI.objects.filter(
        part_number=part_number, view=view, is_active=True
    ).first()
    if roi is None:
        return None
    return {
        "x": round(roi.x * roi.ref_width),
        "y": round(roi.y * roi.ref_height),
        "width": round(roi.width * roi.ref_width),
        "height": round(roi.height * roi.ref_height),
        "ref_width": int(roi.ref_width),
        "ref_height": int(roi.ref_height),
    }


def _resolve_roi(part_number, view):
    """Resolve the ROI for *view*: camera config first, then per-part ROI.

    Returns ``(pixels_or_None, source_or_None)``.
    """
    pixels = _roi_pixels_from_config(_camera_config_for(view))
    if pixels is not None:
        return pixels, "config"
    pixels = _roi_pixels_from_part(part_number, view)
    if pixels is not None:
        return pixels, "part"
    return None, None


def _camera_frame_or_none():
    """Return the latest camera frame, or ``None`` when unavailable."""
    ok, frame = CameraService.get_frame()
    if not ok or frame is None:
        return None
    return frame


def _jpeg_bytes(frame, quality=90):
    """Encode a BGR frame as JPEG bytes (``None`` on failure)."""
    import cv2

    try:
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("JPEG encode failed: %s", exc)
        return None
    return buf.tobytes() if ok else None


def _jpeg_data_url(frame, quality=90):
    jpeg = _jpeg_bytes(frame, quality)
    if jpeg is None:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


def _decode_image_payload(value):
    """Decode an inline image payload (data URL or base64) to bytes."""
    raw = _clean(value)
    if not raw:
        return None
    if raw.startswith("data:"):
        _, _, payload = raw.partition(",")
        raw = payload
    try:
        return base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError):
        return None


def _image_dimensions(jpeg_bytes):
    """Decode JPEG bytes and return ``(width, height)`` or ``(None, None)``."""
    if not jpeg_bytes:
        return None, None
    try:
        import cv2
        import numpy as np

        array = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if image is None:
            return None, None
        height, width = image.shape[:2]
        return int(width), int(height)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not decode image dimensions: %s", exc)
        return None, None


def _resolution_payload():
    """Camera resolution, falling back to the active configuration."""
    width, height = CameraService.get_resolution()
    if width and height:
        return {"width": int(width), "height": int(height)}
    config = _camera_config_for("front")
    if config is not None:
        return {"width": int(config.frame_width), "height": int(config.frame_height)}
    return {"width": 0, "height": 0}


def _preset_payload(preset):
    return {
        "id": preset.id,
        "part_number": preset.part_number,
        "name": preset.name,
        "version": preset.version,
        "part_class": preset.part_class,
        "camera_index": preset.camera_index,
        "confidence_threshold": preset.confidence_threshold,
        "stable_frames": preset.stable_frames,
        "trigger_cooldown": preset.trigger_cooldown,
        "yolo_model_id": preset.yolo_model_id,
        "yolo_model": (preset.yolo_model.name if preset.yolo_model else None),
        "is_active": preset.is_active,
        "master_image": (preset.master_image.url if preset.master_image else ""),
        "created_at": preset.created_at.isoformat() if preset.created_at else None,
    }


def _job_payload(job):
    return {
        "job_id": job.pk,
        "training_project": job.training_project,
        "celery_task_id": job.celery_task_id,
        "status": job.status,
        "progress": job.progress,
        "current_epoch": job.current_epoch,
        "total_epochs": job.total_epochs,
        "message": job.message,
        "precision": job.precision,
        "recall": job.recall,
        "map50": job.map50,
        "map50_95": job.map50_95,
        "model_path": job.model_path,
        "model_version": job.model_version,
        "error_message": job.error_message,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


def _training_backend_available():
    """True when Celery (and its Redis broker) can be imported/configured."""
    try:
        import celery  # noqa: F401
    except Exception:
        return False
    try:
        from VisionInspection.tasks import train_yolo_model  # noqa: F401
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------- #
#  Page views
# --------------------------------------------------------------------------- #


def _station_id():
    """Station identifier recorded on this deployment (never an invented user)."""
    from django.conf import settings

    return _clean(getattr(settings, "VISION_INSPECTION_STATION_ID", ""))


def inspection_view(request):
    """Airbag Stitch Inspection workspace (live camera + workflow)."""
    recent = VisionInspectionLog.objects.all().order_by("-timestamp")[:12]
    context = {
        "sidebar_active": "inspection",
        "station_id": _station_id(),
        "csrf_token": get_token(request),
        "parts": _parts_for_select(),
        "camera_capabilities": get_camera_capabilities(),
        "active_model": _active_model(),
        "recent_inspections": recent,
        "selected_part": _clean(request.GET.get("part_number")),
    }
    return render(request, "VisionInspection/inspection.html", context)


def inspection_master_view(request, part_number=""):
    """Continuous ROI inspection master page (front/back camera views)."""
    selected = _clean(part_number) or _clean(request.GET.get("part_number"))
    if selected and not VisionInspectionPart.objects.filter(
        part_number__iexact=selected
    ).exists():
        raise Http404("Part number not found.")
    context = {
        "sidebar_active": "inspection_master",
        "station_id": _station_id(),
        "parts": _parts_for_select(),
        "camera_capabilities": get_camera_capabilities(),
        "selected_part": selected,
    }
    return render(request, "VisionInspection/inspection_master.html", context)


def configuration_view(request):
    """Tabbed configuration page (preset / master image / trigger / ROI / YOLO)."""
    context = {
        "sidebar_active": "presets",
        "presets": VisionInspectionPreset.objects.all().order_by("-created_at"),
        "preset_id": _clean(request.GET.get("preset_id")),
    }
    return render(request, "VisionInspection/configuration.html", context)


def master_image_view(request):
    """Master image setup — the configuration page for a specific preset."""
    return configuration_view(request)


def trigger_zone_config_view(request, preset_id):
    """Trigger-zone setup for *preset_id* (configuration page, preset selected)."""
    get_object_or_404(VisionInspectionPreset, pk=preset_id)
    request.GET = request.GET.copy()
    request.GET["preset_id"] = str(preset_id)
    return configuration_view(request)


def roi_config_list(request, preset_id):
    """ROI setup for *preset_id* (configuration page, preset selected)."""
    get_object_or_404(VisionInspectionPreset, pk=preset_id)
    request.GET = request.GET.copy()
    request.GET["preset_id"] = str(preset_id)
    return configuration_view(request)


def preset_detail(request, preset_id):
    """Preset detail page — shows the single requested preset."""
    preset = get_object_or_404(VisionInspectionPreset, pk=preset_id)
    context = {
        "sidebar_active": "presets",
        "preset": preset,
        "presets": [preset],
    }
    return render(request, "VisionInspection/preset.html", context)


def history_view(request):
    """Inspection history list."""
    date_from = _clean(request.GET.get("date_from"))
    date_to = _clean(request.GET.get("date_to"))
    part_number = _clean(request.GET.get("part_number"))
    result = _clean(request.GET.get("result")).upper()

    from VisionInspection.services.access_control import scope_logs, can_view_log
    logs = scope_logs(request.user, VisionInspectionLog.objects.all())
    if part_number:
        logs = logs.filter(part_number__icontains=part_number)
    if result in ("OK", "NOT_OK", "ERROR"):
        logs = logs.filter(result=result)
    if date_from:
        logs = logs.filter(timestamp__date__gte=date_from)
    if date_to:
        logs = logs.filter(timestamp__date__lte=date_to)

    context = {
        "sidebar_active": "history",
        "logs": logs.order_by("-timestamp")[:500],
        "log": None,
        "roi_results": [],
        "detected_objects": [],
        "date_from": date_from,
        "date_to": date_to,
        "part_number": part_number,
        "result": result,
    }
    return render(request, "VisionInspection/history.html", context)


def history_detail(request, inspection_id):
    """Inspection history detail — only the evidence that was really stored."""
    from django.core.exceptions import PermissionDenied
    from VisionInspection.services.access_control import can_view_log
    log = get_object_or_404(VisionInspectionLog.objects.select_related("session"), pk=inspection_id)
    if not can_view_log(request.user, log):
        raise PermissionDenied
    from VisionInspection.services.storage_service import image_url

    context = {
        "sidebar_active": "history",
        "logs": [log],
        "log": log,
        "inspection_image_url": image_url(log, "inspection_image"),
        "roi_results": _history_roi_rows(log),
        "detected_objects": _history_detection_rows(log),
        "date_from": "",
        "date_to": "",
        "part_number": "",
        "result": "",
    }
    return render(request, "VisionInspection/history.html", context)


def _history_roi_rows(log):
    """ROI rows for the history template, in the shape it renders.

    Workflow-saved records store per-side results (``{"side": ..., "results":
    [...]}``); flat ROI records store ``roi_name``/``result``/``details``.
    Both are rendered truthfully — an unruled stitch stays unruled.
    """
    rows = []
    for entry in log.roi_results or []:
        if not isinstance(entry, dict):
            continue
        if "results" in entry:
            side = entry.get("side") or ""
            for item in entry.get("results") or []:
                if not isinstance(item, dict):
                    continue
                stitch = item.get("stitch", "—")
                rows.append(
                    {
                        "roi_name": f"{side} · {stitch}" if side else str(stitch),
                        "result": item.get("status", ""),
                        "details": item.get("measurement") or "—",
                    }
                )
        else:
            rows.append(
                {
                    "roi_name": entry.get("roi_name", "—"),
                    "result": entry.get("result", ""),
                    "details": entry.get("details") or entry.get("expected") or "—",
                }
            )
    return rows


def _history_detection_rows(log):
    """Detection rows for the history template (YOLO bbox rows pass through)."""
    rows = []
    for item in log.detected_objects or []:
        if not isinstance(item, dict):
            continue
        if "x1" in item and "class" in item:
            rows.append(item)
        elif "stitch" in item:
            rows.append(
                {
                    "class": item.get("stitch", "—"),
                    "confidence": item.get("confidence", 0),
                    "x1": "—",
                    "y1": "—",
                    "x2": "—",
                    "y2": "—",
                }
            )
    return rows


# --------------------------------------------------------------------------- #
#  Employee master (JSON CRUD + validation)
# --------------------------------------------------------------------------- #


@require_GET
def api_employee_list(request):
    """Paginated employee master list (consumed by masters.js)."""
    query = _clean(request.GET.get("q"))
    is_active = _clean(request.GET.get("is_active"))

    employees = VisionInspectionEmployee.objects.all()
    if query:
        employees = employees.filter(
            employee_code__icontains=query
        ) | employees.filter(employee_name__icontains=query)
        employees = employees.distinct()
    if is_active in ("1", "true", "True"):
        employees = employees.filter(is_active=True)
    elif is_active in ("0", "false", "False"):
        employees = employees.filter(is_active=False)

    page, page_size = _pagination_params(request)
    paginator = Paginator(employees.order_by("employee_name"), page_size)
    page_obj = paginator.get_page(page)

    items = [
        {
            "id": e.id,
            "employee_code": e.employee_code,
            "employee_name": e.employee_name,
            "department": e.department or "",
            "designation": e.designation or "",
            "is_active": e.is_active,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in page_obj.object_list
    ]
    return _json_response(
        {
            "items": items,
            "total": paginator.count,
            "pages": paginator.num_pages,
            "page": page_obj.number,
            "page_size": page_size,
        }
    )


def _validate_employee(data, instance=None):
    """Return a ``fields`` map of validation problems (empty when valid)."""
    fields = {}
    code = _clean(data.get("employee_code"))
    name = _clean(data.get("employee_name"))

    if not code:
        fields["employee_code"] = "Employee code is required."
    elif len(code) > 50:
        fields["employee_code"] = "Employee code must be at most 50 characters."
    else:
        clash = VisionInspectionEmployee.objects.filter(employee_code__iexact=code)
        if instance is not None:
            clash = clash.exclude(pk=instance.pk)
        if clash.exists():
            fields["employee_code"] = "This employee code is already registered."

    if not name:
        fields["employee_name"] = "Employee name is required."
    elif len(name) > 100:
        fields["employee_name"] = "Employee name must be at most 100 characters."
    else:
        clash = VisionInspectionEmployee.objects.filter(
            name_normalized=normalize_token(name)
        )
        if instance is not None:
            clash = clash.exclude(pk=instance.pk)
        if clash.exists():
            fields["employee_name"] = "An employee with this name already exists."

    return fields


@require_POST
def employee_create(request):
    data = _body_json(request)
    fields = _validate_employee(data)
    if fields:
        return _error("Validation failed.", fields)

    try:
        employee = VisionInspectionEmployee.objects.create(
            employee_code=_clean(data.get("employee_code")),
            employee_name=_clean(data.get("employee_name")),
            department=_clean(data.get("department")) or None,
            designation=_clean(data.get("designation")) or None,
        )
    except IntegrityError as exc:
        return _error("Could not save employee.", _integrity_fields(exc, "employee"))
    return _json_response(
        {
            "ok": True,
            "id": employee.id,
            "employee_code": employee.employee_code,
            "message": "Employee created.",
        },
        status=201,
    )


@require_POST
def employee_update(request, employee_id):
    employee = get_object_or_404(VisionInspectionEmployee, pk=employee_id)
    data = _body_json(request)
    fields = _validate_employee(data, instance=employee)
    if fields:
        return _error("Validation failed.", fields)

    employee.employee_code = _clean(data.get("employee_code"))
    employee.employee_name = _clean(data.get("employee_name"))
    employee.department = _clean(data.get("department")) or None
    employee.designation = _clean(data.get("designation")) or None
    try:
        employee.save()
    except IntegrityError as exc:
        return _error("Could not update employee.", _integrity_fields(exc, "employee"))
    return _json_response(
        {"ok": True, "id": employee.id, "message": "Employee updated."}
    )


@require_POST
def api_employee_validate(request):
    """Validate a scanned employee code for the inspection flow."""
    data = _body_json(request)
    code = _clean(data.get("employee_code"))
    if not code:
        return _error("Employee code is required.")
    employee = VisionInspectionEmployee.objects.filter(
        employee_code__iexact=code, is_active=True
    ).first()
    if employee is None:
        return _json_response(
            {"valid": False, "message": "Unknown or inactive employee code."},
            status=200,
        )
    return _json_response(
        {
            "valid": True,
            "employee": {
                "employee_code": employee.employee_code,
                "employee_name": employee.employee_name,
                "department": employee.department or "",
                "designation": employee.designation or "",
            },
        }
    )


# --------------------------------------------------------------------------- #
#  Part master (JSON CRUD + validation)
# --------------------------------------------------------------------------- #


@require_GET
def api_part_list(request):
    """Paginated part master list (consumed by masters.js)."""
    query = _clean(request.GET.get("q"))
    is_active = _clean(request.GET.get("is_active"))

    parts = VisionInspectionPart.objects.all()
    if not request.user.is_authenticated:
        parts = parts.filter(is_active=True)
    if query:
        parts = parts.filter(part_number__icontains=query) | parts.filter(
            part_name__icontains=query
        )
        parts = parts.distinct()
    if is_active in ("1", "true", "True"):
        parts = parts.filter(is_active=True)
    elif is_active in ("0", "false", "False"):
        parts = parts.filter(is_active=False)

    page, page_size = _pagination_params(request)
    paginator = Paginator(parts.order_by("part_number"), page_size)
    page_obj = paginator.get_page(page)

    items = [
        {
            "id": p.id,
            "part_number": p.part_number,
            "part_name": p.part_name,
            "part_desc": p.part_name,
            "description": p.description or "",
            "is_active": p.is_active,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in page_obj.object_list
    ]
    return _json_response(
        {
            "items": items,
            "total": paginator.count,
            "pages": paginator.num_pages,
            "page": page_obj.number,
            "page_size": page_size,
        }
    )


def _validate_part(data, instance=None):
    fields = {}
    number = _clean(data.get("part_number"))
    name = _clean(data.get("part_name"))

    if not number:
        fields["part_number"] = "Part number is required."
    elif len(number) > 40:
        fields["part_number"] = "Part number must be at most 40 characters."
    else:
        clash = VisionInspectionPart.objects.filter(part_number__iexact=number)
        if instance is not None:
            clash = clash.exclude(pk=instance.pk)
        if clash.exists():
            fields["part_number"] = "A part with this number already exists."

    if not name:
        fields["part_name"] = "Part name is required."
    elif len(name) > 200:
        fields["part_name"] = "Part name must be at most 200 characters."

    return fields


@require_POST
def part_create(request):
    data = _body_json(request)
    fields = _validate_part(data)
    if fields:
        return _error("Validation failed.", fields)

    try:
        part = VisionInspectionPart.objects.create(
            part_number=_clean(data.get("part_number")),
            part_name=_clean(data.get("part_name")),
            description=_clean(data.get("description")) or None,
        )
    except IntegrityError as exc:
        return _error("Could not save part.", _integrity_fields(exc, "part"))
    return _json_response(
        {
            "ok": True,
            "id": part.id,
            "part_number": part.part_number,
            "message": "Part created.",
        },
        status=201,
    )


@require_POST
def part_update(request, part_id):
    part = get_object_or_404(VisionInspectionPart, pk=part_id)
    data = _body_json(request)
    fields = _validate_part(data, instance=part)
    if fields:
        return _error("Validation failed.", fields)

    part.part_number = _clean(data.get("part_number"))
    part.part_name = _clean(data.get("part_name"))
    part.description = _clean(data.get("description")) or None
    try:
        part.save()
    except IntegrityError as exc:
        return _error("Could not update part.", _integrity_fields(exc, "part"))
    return _json_response(
        {"ok": True, "id": part.id, "message": "Part updated."}
    )


@require_POST
def api_part_validate(request):
    """Validate a scanned part number and report its active preset."""
    data = _body_json(request)
    number = _clean(data.get("part_number"))
    if not number:
        return _error("Part number is required.")
    part = VisionInspectionPart.objects.filter(part_number__iexact=number).first()
    if part is None:
        return _json_response(
            {"valid": False, "message": "Part number is not registered."},
            status=200,
        )
    preset = (
        VisionInspectionPreset.objects.filter(
            part_number__iexact=part.part_number, is_active=True
        )
        .order_by("-version")
        .first()
    )
    return _json_response(
        {
            "valid": True,
            "part": {
                "part_number": part.part_number,
                "part_name": part.part_name,
            },
            "has_preset": preset is not None,
            "preset": (_preset_payload(preset) if preset else None),
        }
    )


@require_GET
def api_part_configuration(request, part_id):
    """Configuration summary for a part number (used by configuration.js)."""
    part = VisionInspectionPart.objects.filter(part_number__iexact=part_id).first()
    if part is None:
        return _json_response(
            {"has_config": False, "message": "Part number is not registered."},
            status=404,
        )
    preset = (
        VisionInspectionPreset.objects.filter(part_number__iexact=part.part_number)
        .order_by("-is_active", "-version")
        .first()
    )
    if preset is None:
        return _json_response(
            {"has_config": False, "part_number": part.part_number}
        )
    payload = _preset_payload(preset)
    payload.update(
        {
            "has_config": True,
            "part_number": part.part_number,
            "part_name": part.part_name,
            "expected_part_class": preset.part_class,
            "camera": preset.camera_index,
            "inspection_rules": {
                "trigger_cooldown": preset.trigger_cooldown,
                "stable_frames": preset.stable_frames,
            },
            "master_width": (preset.master_image.width if preset.master_image else 0),
            "master_height": (preset.master_image.height if preset.master_image else 0),
        }
    )
    return _json_response(payload)


@require_GET
def parts_list_api(request):
    """Flat part list for the configuration-page dropdowns."""
    return _json_response({"parts": _parts_for_select()})


# --------------------------------------------------------------------------- #
#  Preset master (JSON)
# --------------------------------------------------------------------------- #


@require_GET
def preset_list_api(request):
    part_number = _clean(request.GET.get("part_number"))
    presets = VisionInspectionPreset.objects.all()
    if part_number:
        presets = presets.filter(part_number__iexact=part_number)
    presets = sorted(
        presets.order_by("-created_at"),
        key=lambda p: (p.part_number, -p.version),
    )
    return _json_response(
        {"presets": [_preset_payload(p) for p in presets]}
    )


@require_POST
def create_preset(request):
    """Create the next preset version for a part number."""
    data = _body_json(request)
    part_number = _clean(data.get("part_number"))
    name = _clean(data.get("name")) or f"{part_number} Preset"
    part_class = _clean(data.get("part_class"))

    fields = {}
    if not part_number:
        fields["part_number"] = "Part number is required."
    if not part_class:
        fields["part_class"] = "Part class is required."
    if fields:
        return _error("Validation failed.", fields)

    part = VisionInspectionPart.objects.filter(part_number__iexact=part_number).first()
    if part is None:
        return _error(
            "Part number is not registered in the part master.",
            {"part_number": "Register the part number before creating a preset."},
        )

    highest = (
        VisionInspectionPreset.objects.filter(part_number=part.part_number)
        .order_by("-version")
        .values_list("version", flat=True)
        .first()
    )
    model_id = _as_int(data.get("yolo_model_id"), 0) or None
    if model_id is not None and not VisionYOLOModel.objects.filter(pk=model_id).exists():
        model_id = None

    preset = VisionInspectionPreset.objects.create(
        part_number=part.part_number,
        name=name,
        version=(highest or 0) + 1,
        part_class=part_class,
        camera_index=_as_int(data.get("camera_index"), 0),
        confidence_threshold=_as_float(data.get("confidence_threshold"), 0.5),
        stable_frames=_as_int(data.get("stable_frames"), 3),
        trigger_cooldown=_as_float(data.get("trigger_cooldown"), 2.0),
        yolo_model_id=model_id,
        is_active=False,
    )
    return _json_response(
        {
            "ok": True,
            "preset_id": preset.id,
            "version": preset.version,
            "message": f"Preset v{preset.version} saved.",
        },
        status=201,
    )


@require_POST
def activate_preset(request, preset_id):
    """Activate *preset_id* (and deactivate siblings for the same part)."""
    preset = get_object_or_404(VisionInspectionPreset, pk=preset_id)
    with transaction.atomic():
        VisionInspectionPreset.objects.filter(
            part_number=preset.part_number, is_active=True
        ).exclude(pk=preset.pk).update(is_active=False)
        preset.is_active = True
        preset.save()
    return _json_response(
        {
            "ok": True,
            "preset_id": preset.id,
            "message": f"Preset v{preset.version} activated.",
        }
    )


# --------------------------------------------------------------------------- #
#  Master image / trigger zone / ROI configuration
# --------------------------------------------------------------------------- #


@require_POST
def save_master_image(request):
    """Store the captured master (reference) image for a preset."""
    data = _body_json(request)
    preset_id = _as_int(data.get("preset_id"), 0)
    preset = get_object_or_404(VisionInspectionPreset, pk=preset_id)

    jpeg = _decode_image_payload(data.get("image"))
    if jpeg is None:
        return _error(
            "No image was captured. Start the camera and capture a master image "
            "before saving."
        )

    from django.core.files.base import ContentFile

    width, height = _image_dimensions(jpeg)
    if not width or not height:
        width = _as_int(data.get("width"), 0) or 0
        height = _as_int(data.get("height"), 0) or 0
    if not width or not height:
        return _error("Could not read the captured image dimensions.")

    preset.master_image.save(
        f"master_preset_{preset.id}.jpg", ContentFile(jpeg), save=True
    )
    return _json_response(
        {
            "ok": True,
            "message": "Master image saved.",
            "master_image": preset.master_image.url,
            "width": width,
            "height": height,
        }
    )


def _normalize_polygon(points, ref_width, ref_height):
    """Normalise corner points to 0..1 against the reference frame."""
    ref_width = float(ref_width or 0)
    ref_height = float(ref_height or 0)
    normalized = []
    for point in list(points)[:4]:
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError, IndexError):
            return None
        if ref_width <= 0 or ref_height <= 0:
            normalized.append([x, y])
        elif abs(x) > 1.0 or abs(y) > 1.0:
            normalized.append([x / ref_width, y / ref_height])
        else:
            normalized.append([x, y])
    return normalized if len(normalized) == 4 else None


@require_POST
def save_trigger_zone(request):
    """Save the four-corner trigger zone for a preset (replaces any existing)."""
    data = _body_json(request)
    preset_id = _as_int(data.get("preset_id"), 0)
    preset = get_object_or_404(VisionInspectionPreset, pk=preset_id)

    points = data.get("polygon") or data.get("points")
    if not points:
        points = [data.get(k) for k in ("p1", "p2", "p3", "p4")]
    if not points or len(points) != 4:
        return _error("A four-corner polygon is required.")

    ref_width = _as_float(data.get("reference_width") or data.get("width"), 0)
    ref_height = _as_float(data.get("reference_height") or data.get("height"), 0)
    polygon = _normalize_polygon(points, ref_width, ref_height)
    if polygon is None:
        return _error("The trigger zone polygon could not be interpreted.")

    master_width = preset.master_image.width if preset.master_image else 0
    master_height = preset.master_image.height if preset.master_image else 0
    if not ref_width:
        ref_width = master_width
    if not ref_height:
        ref_height = master_height

    zone, _created = VisionInspectionTriggerZone.objects.update_or_create(
        preset=preset,
        defaults={
            "p1": polygon[0],
            "p2": polygon[1],
            "p3": polygon[2],
            "p4": polygon[3],
            "reference_width": ref_width,
            "reference_height": ref_height,
        },
    )
    return _json_response(
        {
            "ok": True,
            "message": "Trigger zone saved.",
            "trigger_zone_id": zone.pk,
            "polygon": zone.polygon,
        }
    )


def _roi_query_params(data):
    """Extract shared ROI fields from a request payload."""
    points = data.get("polygon") or data.get("points")
    if not points:
        points = [data.get(k) for k in ("p1", "p2", "p3", "p4")]
    if not points or len(points) != 4:
        return None, {"polygon": "A four-corner polygon is required."}
    return points, {}


@require_POST
def create_roi(request):
    """Create a polygon ROI area for a preset."""
    data = _body_json(request)
    preset_id = _as_int(data.get("preset_id"), 0)
    preset = get_object_or_404(VisionInspectionPreset, pk=preset_id)

    points, errors = _roi_query_params(data)
    if errors:
        return _error("Validation failed.", errors)

    ref_width = _as_float(data.get("reference_width") or data.get("width"), 0)
    ref_height = _as_float(data.get("reference_height") or data.get("height"), 0)
    polygon = _normalize_polygon(points, ref_width, ref_height)
    if polygon is None:
        return _error("The ROI polygon could not be interpreted.")

    expected_class = _clean(data.get("expected_class"))
    if not expected_class:
        return _error(
            "Validation failed.", {"expected_class": "An expected class is required."}
        )

    next_sequence = (
        VisionROI.objects.filter(preset=preset).order_by("-sequence").first()
    )
    roi = VisionROI.objects.create(
        preset=preset,
        roi_name=_clean(data.get("roi_name")) or "ROI",
        sequence=(next_sequence.sequence + 1 if next_sequence else 1),
        p1=polygon[0],
        p2=polygon[1],
        p3=polygon[2],
        p4=polygon[3],
        normalized_coordinates={
            "polygon": polygon,
            "ref_w": int(ref_width or 0),
            "ref_h": int(ref_height or 0),
        },
        expected_class=expected_class,
        min_count=_as_int(data.get("min_count"), 0),
        max_count=_as_int(data.get("max_count"), 0),
        confidence_threshold=_as_float(data.get("confidence_threshold"), 0.5),
        inspection_rule=_clean(data.get("inspection_rule")) or "count",
    )
    return _json_response(
        {"ok": True, "roi_id": roi.pk, "message": "ROI created."}, status=201
    )


@require_POST
def update_roi(request, roi_id):
    roi = get_object_or_404(VisionROI, pk=roi_id)
    data = _body_json(request)

    updates = {}
    if "roi_name" in data:
        updates["roi_name"] = _clean(data.get("roi_name")) or roi.roi_name
    if "expected_class" in data:
        updates["expected_class"] = _clean(data.get("expected_class"))
    if "min_count" in data:
        updates["min_count"] = _as_int(data.get("min_count"), roi.min_count)
    if "max_count" in data:
        updates["max_count"] = _as_int(data.get("max_count"), roi.max_count)
    if "confidence_threshold" in data:
        updates["confidence_threshold"] = _as_float(
            data.get("confidence_threshold"), roi.confidence_threshold
        )
    if "inspection_rule" in data:
        updates["inspection_rule"] = _clean(data.get("inspection_rule")) or "count"
    if "is_active" in data:
        updates["is_active"] = str(data.get("is_active")).lower() in (
            "1",
            "true",
            "True",
        )

    points, errors = _roi_query_params(data) if data.get("polygon") else (None, {})
    if not errors and points:
        ref_width = _as_float(data.get("reference_width") or data.get("width"), 0)
        ref_height = _as_float(data.get("reference_height") or data.get("height"), 0)
        polygon = _normalize_polygon(points, ref_width, ref_height)
        if polygon is None:
            return _error("The ROI polygon could not be interpreted.")
        updates.update(
            {
                "p1": polygon[0],
                "p2": polygon[1],
                "p3": polygon[2],
                "p4": polygon[3],
                "normalized_coordinates": {
                    "polygon": polygon,
                    "ref_w": int(ref_width or 0),
                    "ref_h": int(ref_height or 0),
                },
            }
        )

    if not updates:
        return _error("No fields to update.")

    for field, value in updates.items():
        setattr(roi, field, value)
    roi.save()
    return _json_response(
        {"ok": True, "roi_id": roi.pk, "message": "ROI updated."}
    )


@require_POST
def delete_roi(request, roi_id):
    roi = get_object_or_404(VisionROI, pk=roi_id)
    roi.delete()
    return _json_response({"ok": True, "message": "ROI deleted."})


@require_POST
def reorder_rois(request):
    """Apply a new sequence to the ROIs of one preset."""
    data = _body_json(request)
    preset_id = _as_int(data.get("preset_id"), 0)
    order = data.get("order") or data.get("ids") or []
    if not isinstance(order, list) or not order:
        return _error("An ordered list of ROI ids is required.")

    rois = VisionROI.objects.filter(pk__in=[_as_int(i, 0) for i in order])
    if preset_id:
        rois = rois.filter(preset_id=preset_id)
    by_id = {roi.pk: roi for roi in rois}

    with transaction.atomic():
        for index, raw_id in enumerate(order, start=1):
            roi = by_id.get(_as_int(raw_id, 0))
            if roi is None:
                continue
            roi.sequence = index
            roi.save(update_fields=["sequence", "updated_at"])
    return _json_response({"ok": True, "message": "ROI order saved."})


# --------------------------------------------------------------------------- #
#  Camera control
# --------------------------------------------------------------------------- #


def camera_start(request):
    """Start the camera capture thread (OpenCV or Picamera2)."""
    source = _clean(request.GET.get("source")) or CAMERA_SOURCES["OPENCV"]
    index = _as_int(request.GET.get("camera_index"), 0)
    reuse_existing = request.GET.get("reuse_if_active") == "1"
    started = CameraService.start_camera(
        camera_index=index,
        source=source,
        reuse_existing=reuse_existing,
    )
    status = CameraService.get_status()
    current_source = CameraService.get_source()
    current_index = CameraService.get_camera_index()
    if reuse_existing and not started and status in (
        CAMERA_STATES["ON"],
        CAMERA_STATES["CONNECTING"],
    ) and (
        current_source != source
        or (
            source != CAMERA_SOURCES["PICAMERA"]
            and current_index != index
        )
    ):
        return _error(
            "The camera is already active with different settings. Stop the "
            "current workflow before changing its camera source or index.",
            status=409,
        )
    return _json_response(
        {
            "status": status,
            "state": CameraService.get_state(),
            "error": CameraService.get_error(),
            "source": current_source,
            "camera_index": current_index,
            "started_by_request": started,
            "reused": reuse_existing and not started and status in (
                CAMERA_STATES["ON"],
                CAMERA_STATES["CONNECTING"],
            ),
        }
    )


def camera_stop(request):
    CameraService.stop_camera()
    return _json_response(
        {
            "status": CameraService.get_status(),
            "state": CameraService.get_state(),
        }
    )


def camera_status(request):
    resolution = _resolution_payload()
    return _json_response(
        {
            "status": CameraService.get_status(),
            "state": CameraService.get_state(),
            "error": CameraService.get_error(),
            "source": CameraService.get_source(),
            "camera_index": CameraService.get_camera_index(),
            "width": resolution["width"],
            "height": resolution["height"],
            "model_configured": _active_model() is not None,
        }
    )


def camera_stream(request):
    """MJPEG live stream built from the service's latest-frame buffer."""

    def _frames():
        import cv2

        while True:
            ok, frame = CameraService.get_frame()
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            try:
                encoded, buffer = cv2.imencode(
                    ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80]
                )
            except Exception:  # pragma: no cover - defensive
                time.sleep(0.1)
                continue
            if not encoded:
                continue
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
            )
            time.sleep(0.03)

    return StreamingHttpResponse(
        _frames(), content_type="multipart/x-mixed-replace; boundary=frame"
    )


@require_POST
def camera_capture(request):
    """Capture a single frame and return it inline for the operator UI."""
    frame = _camera_frame_or_none()
    if frame is None:
        return _error(
            "No camera frame available. Start the camera first.", status=409
        )
    data_url = _jpeg_data_url(frame)
    if data_url is None:
        return _error("Could not encode the captured frame.", status=500)
    height, width = frame.shape[:2]
    return _json_response(
        {
            "ok": True,
            "image": data_url,
            "width": int(width),
            "height": int(height),
        }
    )


# --------------------------------------------------------------------------- #
#  Inspection Master — continuous ROI inspection
# --------------------------------------------------------------------------- #


@require_POST
def master_roi_save(request):
    """Save (or replace) the drawn ROI for a part + camera view."""
    data = _body_json(request)
    part_number = _clean(data.get("part_number"))
    view = _clean(data.get("camera_view")) or "front"
    if not part_number:
        return _error("Part number is required.")
    if view not in ("front", "back"):
        return _error("camera_view must be 'front' or 'back'.")

    x = _as_float(data.get("x"), 0)
    y = _as_float(data.get("y"), 0)
    width = _as_float(data.get("width"), 0)
    height = _as_float(data.get("height"), 0)
    ref_width = _as_int(data.get("ref_width"), 0)
    ref_height = _as_int(data.get("ref_height"), 0)
    if not ref_width or not ref_height:
        return _error("The reference frame dimensions are required.")
    if width <= 1 or height <= 1:
        return _error("The ROI is too small — draw a larger region.")
    if x + width > ref_width or y + height > ref_height:
        return _error("The ROI extends beyond the camera frame.")

    with transaction.atomic():
        VisionInspectionMasterROI.objects.filter(
            part_number=part_number, view=view
        ).update(is_active=False)
        roi = VisionInspectionMasterROI.objects.create(
            part_number=part_number,
            view=view,
            x=max(0.0, min(1.0, x / ref_width)),
            y=max(0.0, min(1.0, y / ref_height)),
            width=max(0.0, min(1.0, width / ref_width)),
            height=max(0.0, min(1.0, height / ref_height)),
            ref_width=ref_width,
            ref_height=ref_height,
            is_active=True,
        )

    # Keep the running engine's O(1) ROI cache in step with the saved ROI.
    ContinuousInspectionEngine.configure(
        part_number, x, y, width, height, ref_width, ref_height
    )

    return _json_response(
        {
            "ok": True,
            "part_number": part_number,
            "view": view,
            "x": round(x),
            "y": round(y),
            "width": round(width),
            "height": round(height),
            "ref_width": ref_width,
            "ref_height": ref_height,
            "normalized": {"x": roi.x, "y": roi.y, "width": roi.width, "height": roi.height},
            "message": "ROI saved.",
        }
    )


def master_roi_get(request):
    """Return the saved ROI for a part + view (camera config first)."""
    part_number = _clean(request.GET.get("part_number"))
    view = _clean(request.GET.get("camera_view")) or "front"
    if not part_number:
        return _error("Part number is required.")
    if view not in ("front", "back"):
        return _error("camera_view must be 'front' or 'back'.")

    pixels, source = _resolve_roi(part_number, view)
    if pixels is None:
        return _json_response(
            {
                "saved": False,
                "source": None,
                "part_number": part_number,
                "view": view,
                "message": "No ROI has been saved for this part and view yet.",
            }
        )

    # Mirror the resolved ROI into the running engine cache.
    ContinuousInspectionEngine.configure(
        part_number,
        pixels["x"],
        pixels["y"],
        pixels["width"],
        pixels["height"],
        pixels["ref_width"],
        pixels["ref_height"],
    )

    return _json_response(
        {
            "saved": True,
            "source": source,
            "part_number": part_number,
            "view": view,
            "x": pixels["x"],
            "y": pixels["y"],
            "width": pixels["width"],
            "height": pixels["height"],
            "ref_width": pixels["ref_width"],
            "ref_height": pixels["ref_height"],
            "normalized": {
                "x": pixels["x"] / (pixels["ref_width"] or 1),
                "y": pixels["y"] / (pixels["ref_height"] or 1),
                "width": pixels["width"] / (pixels["ref_width"] or 1),
                "height": pixels["height"] / (pixels["ref_height"] or 1),
            },
        }
    )


def _engine_status():
    status = ContinuousInspectionEngine.get_status()
    status["running"] = ContinuousInspectionEngine.is_running()
    status["ok"] = True
    return status


def master_inspection_start(request):
    """Start continuous ROI inspection for a part."""
    data = _body_json(request)
    part_number = _clean(data.get("part_number"))
    view = _clean(data.get("camera_view")) or "front"
    if not part_number:
        return _error("Part number is required.")

    pixels, source = _resolve_roi(part_number, view)
    if pixels is None:
        return _error(
            "No ROI is configured. Draw and save an ROI for this view first.",
            status=409,
        )

    config = _camera_config_for(view)
    if CameraService.get_status() != CAMERA_STATES["ON"]:
        CameraService.start_camera(
            camera_index=(config.camera_index if config else 0),
            source=(config.camera_source if config else CAMERA_SOURCES["OPENCV"]),
        )
        # Give the capture thread a moment to produce its first frame.
        for _ in range(30):
            if CameraService.get_status() == CAMERA_STATES["ON"]:
                break
            time.sleep(0.1)
    if CameraService.get_status() != CAMERA_STATES["ON"]:
        return _error(
            CameraService.get_error()
            or "The camera could not be started. Check the camera connection.",
            status=409,
        )

    model = _active_model()
    if model is not None and model.model_path:
        ContinuousInspectionEngine.attach_model(
            model.model_path,
            _as_float(
                getattr(model, "confidence_threshold", None),
                0.5,
            ),
        )

    status = ContinuousInspectionEngine.start(
        part_number=part_number,
        roi={
            "x": pixels["x"],
            "y": pixels["y"],
            "width": pixels["width"],
            "height": pixels["height"],
            "ref_width": pixels["ref_width"],
            "ref_height": pixels["ref_height"],
        },
    )
    payload = status
    payload["ok"] = True
    payload["running"] = ContinuousInspectionEngine.is_running()
    payload["roi_source"] = source
    if status.get("state") == "ERROR":
        payload["error"] = status.get("error") or "Inspection could not be started."
    return _json_response(payload)


@require_POST
def master_inspection_stop(request):
    ContinuousInspectionEngine.stop()
    return _json_response(_engine_status())


def api_master_inspection_status(request):
    """Live status of the continuous ROI inspection engine."""
    return _json_response(_engine_status())


def api_master_roi_preview(request):
    """Serve the latest cropped-ROI preview (JPEG) for the UI."""
    jpeg = ContinuousInspectionEngine.get_preview_jpeg()
    if not jpeg:
        return HttpResponse(status=404)
    return HttpResponse(jpeg, content_type="image/jpeg")


def api_master_camera_resolution(request):
    return _json_response(_resolution_payload())


# --------------------------------------------------------------------------- #
#  Camera configuration API (one-time setup, reused across parts)
# --------------------------------------------------------------------------- #


def api_camera_config(request):
    """Active ROI configuration per camera view (or ``null``)."""
    payload = {"front": None, "back": None}
    for view in ("front", "back"):
        config = _camera_config_for(view)
        if config is None:
            continue
        payload[view] = {
            "id": config.pk,
            "view": view,
            "camera_source": config.camera_source,
            "camera_index": config.camera_index,
            "frame_width": int(config.frame_width),
            "frame_height": int(config.frame_height),
            "x": round(config.roi_x * config.frame_width),
            "y": round(config.roi_y * config.frame_height),
            "width": round(config.roi_w * config.frame_width),
            "height": round(config.roi_h * config.frame_height),
            "version": config.version,
            "updated_by": config.updated_by,
        }
    return _json_response(payload)


@require_POST
def api_camera_config_save(request):
    """Save a versioned one-time camera ROI configuration for a view."""
    data = _body_json(request)
    view = _clean(data.get("view")) or "front"
    if view not in ("front", "back"):
        return _error("view must be 'front' or 'back'.")

    x = _as_float(data.get("x"), 0)
    y = _as_float(data.get("y"), 0)
    width = _as_float(data.get("width"), 0)
    height = _as_float(data.get("height"), 0)
    ref_width = _as_int(data.get("ref_width"), 0)
    ref_height = _as_int(data.get("ref_height"), 0)
    if not ref_width or not ref_height:
        return _error("The reference frame dimensions are required.")
    if width <= 1 or height <= 1:
        return _error("The ROI is too small — draw a larger region.")

    config = _workflow().save_camera_config(
        view,
        x,
        y,
        width,
        height,
        ref_width,
        ref_height,
        source=_clean(data.get("camera_source")) or CAMERA_SOURCES["OPENCV"],
        index=_as_int(data.get("camera_index"), 0),
        updated_by=_clean(data.get("updated_by")),
    )
    return _json_response(
        {
            "ok": True,
            "id": config.pk,
            "view": view,
            "version": config.version,
            "x": round(x),
            "y": round(y),
            "width": round(width),
            "height": round(height),
            "frame_width": ref_width,
            "frame_height": ref_height,
            "message": f"Camera configuration v{config.version} saved.",
        },
        status=201,
    )


# --------------------------------------------------------------------------- #
#  Inspection engine + polling state (state-machine workflow)
# --------------------------------------------------------------------------- #


def _inspection_engine():
    """Return the inspection-engine class (imported lazily)."""
    from VisionInspection.services import inspection_service

    return inspection_service.InspectionEngine


@require_POST
def start_inspection_engine(request):
    engine = _inspection_engine()
    engine.start()
    return _json_response({"ok": True, "state": engine.get_state()})


@require_POST
def stop_inspection_engine(request):
    engine = _inspection_engine()
    engine.stop()
    return _json_response({"ok": True, "state": engine.get_state()})


@require_GET
def api_inspection_state(request):
    """Current inspection state consumed by the operator UI."""
    engine = _inspection_engine()
    return _json_response(engine.get_state())


@require_POST
def api_reset_state(request):
    """Reset the persisted inspection state to the waiting-for-employee step."""
    from VisionInspection.models import VisionInspectionState

    engine = _inspection_engine()
    engine.stop()
    state, _created = VisionInspectionState.objects.get_or_create(pk=1)
    state.current_state = "WAITING_EMPLOYEE"
    state.employee_code = None
    state.employee_name = None
    state.employee_valid = False
    state.part_number = None
    state.part_valid = False
    state.preset_name = None
    state.preset_version = None
    state.camera_status = "OFF"
    state.error_message = None
    state.roi_results = []
    state.final_result = "PENDING"
    state.detected_objects = []
    state.last_inspection_id = None
    state.save()
    return _json_response({"ok": True, "message": "Inspection state reset."})


# --------------------------------------------------------------------------- #
#  Airbag Stitch Inspection workflow (barcode → result → save)
# --------------------------------------------------------------------------- #


@require_POST
def workflow_scan_part(request):
    """Step 1 — resolve the scanned part barcode."""
    data = _body_json(request)
    result = _workflow().scan_part(data.get("barcode"), operator=request.user)
    return _json_response(result, status=200 if result.get("ok") else 400)



from django.views.decorators.http import require_POST
from django.core.serializers.json import DjangoJSONEncoder
from django.http import JsonResponse
import json


@require_POST
def workflow_validate_model(request):
    """Step 2 — validate and load the inspection model."""

    data = _body_json(request)
    part_number = data.get("part_number")
    raw_model_id = data.get("model_id")
    if part_number is not None and not isinstance(part_number, str):
        return _json_response(
            {
                "ok": False,
                "state": "invalid_request",
                "error_code": "invalid_part_number",
                "message": "part_number must be a string barcode.",
            },
            status=400,
        )
    part_number = part_number.strip() if part_number is not None else None
    part_number = part_number or None
    if raw_model_id is not None:
        try:
            if isinstance(raw_model_id, bool):
                raise ValueError
            if isinstance(raw_model_id, str) and not raw_model_id.isdecimal():
                raise ValueError
            if not isinstance(raw_model_id, (int, str)):
                raise ValueError
            model_id = int(raw_model_id)
            if model_id < 1:
                raise ValueError
        except (TypeError, ValueError):
            return _json_response(
                {
                    "ok": False,
                    "state": "invalid_request",
                    "error_code": "invalid_model_id",
                    "message": "model_id must be a positive integer.",
                },
                status=400,
            )
    else:
        model_id = None
    if not _clean(part_number) and model_id is None:
        return _json_response(
            {
                "ok": False,
                "state": "invalid_request",
                "error_code": "validation_target_required",
                "message": "Provide a part_number or model_id to validate.",
            },
            status=400,
        )

    result = _workflow().validate_model(model_id, part_number)

    # Convert the result into JSON-safe data.
    def make_json_safe(value):
        if hasattr(value, "_meta"):
            # Django model instance -> dictionary of database fields.
            return {
                field.name: make_json_safe(
                    getattr(value, field.name)
                )
                for field in value._meta.concrete_fields
            }

        if isinstance(value, dict):
            return {
                str(key): make_json_safe(item)
                for key, item in value.items()
            }

        if isinstance(value, (list, tuple)):
            return [make_json_safe(item) for item in value]

        return value

    safe_result = make_json_safe(result)

    # Validate that the response can be serialized.
    # DjangoJSONEncoder handles dates, decimals, and UUIDs.
    safe_result = json.loads(
        json.dumps(safe_result, cls=DjangoJSONEncoder)
    )

    if not safe_result.get("ok"):
        safe_result["error_code"] = safe_result.get("state", "model_validation_failed")
        state = safe_result.get("state")
        if state == "part_not_found":
            status = 404
        elif state == "model_runtime_unavailable":
            status = 503
        else:
            status = 422
    else:
        status = 200

    return JsonResponse(
        safe_result,
        status=status,
        safe=False,
    )


@require_POST
def workflow_submit(request):
    """Step 3 — submit a part and open its inspection session."""
    data = _body_json(request)
    result = _workflow().submit(data.get("part_number"), data.get("model_id"), operator=request.user)
    return _json_response(result, status=200 if result.get("ok") else 400)


def workflow_progress(request, session_id):
    """Poll the live progress of an inspection session."""
    result = _workflow().get_progress(str(session_id))
    status = 200 if result.get("ok") else 404
    if result.get("ok") and result.get("inspection_status") in (
        "OKAY PART",
        "NOT OKAY PART",
        "ERROR",
    ):
        logger.info(
            "Inspection session status returned: session=%s status=%s",
            result.get("session_id"),
            result.get("inspection_status"),
        )
    return _json_response(result, status=status)


@require_POST
def workflow_activate_side(request):
    """Activate the session-configured camera for a side preview."""
    data = _body_json(request)
    session_id = data.get("session_id")
    side = data.get("side")
    if not session_id:
        return _error("A session_id is required.")
    result = _workflow().activate_side(str(session_id), str(side or ""))
    return _json_response(result, status=200 if result.get("ok") else 400)


@require_POST
def workflow_inspect_side(request, side):
    """Step 4/5 — inspect the front or back side of the submitted part."""
    if side not in ("front", "back"):
        return _error("side must be 'front' or 'back'.")
    data = _body_json(request)
    session_id = data.get("session_id")
    if not session_id:
        logger.warning(
            "Inspection request rejected: missing session_id (content_type=%s)",
            request.content_type,
        )
        return _error("A session_id is required.")
    try:
        session_id = str(uuid.UUID(str(session_id)))
    except (TypeError, ValueError, AttributeError):
        logger.warning(
            "Inspection request rejected: invalid session UUID side=%s value=%s",
            side,
            session_id,
        )
        return _error("A valid inspection session UUID is required.")
    logger.info(
        "Inspection request received: session=%s side=%s",
        session_id,
        side,
    )
    result = _workflow().run_side(session_id, side)
    if result.get("ok"):
        progress = result.get("progress") or {}
        logger.info(
            "Inspection API response returned: session=%s side=%s state=%s status=%s",
            session_id,
            side,
            result.get("state"),
            progress.get("inspection_status", ""),
        )
        return _json_response(result)
    state = result.get("state")
    if state == "error":
        status = 404
    elif state in (
        "session_inactive",
        "side_in_progress",
        "side_already_completed",
        "side_not_required",
        "incomplete",
    ):
        status = 409
    elif state in ("camera_error", "frame_unavailable"):
        status = 503
    elif state in ("model_invalid", "model_task_mismatch", "configuration_invalid"):
        status = 422
    elif state in ("front_error", "back_error"):
        status = 500
    else:
        status = 400
    logger.warning(
        "Inspection request failed: session=%s side=%s state=%s message=%s",
        session_id,
        side,
        state,
        result.get("message", ""),
    )
    logger.info(
        "Inspection API response returned: session=%s side=%s state=%s",
        session_id,
        side,
        state,
    )
    return _json_response(result, status=status)


@require_POST
def workflow_compute_result(request):
    """Step 8 — compute the overall result from both sides."""
    data = _body_json(request)
    session_id = data.get("session_id")
    if not session_id:
        return _error("A session_id is required.")
    result = _workflow().compute_result(str(session_id))
    return _json_response(result, status=200 if result.get("ok") else 400)


@require_POST
def workflow_save(request):
    """Step 9 — persist a completed session (idempotent)."""
    data = _body_json(request)
    session_id = data.get("session_id")
    if not session_id:
        return _error("A session_id is required.")
    result = _workflow().save(str(session_id))
    return _json_response(result, status=200 if result.get("ok") else 400)


@require_POST
def workflow_cancel(request):
    """Abandon an in-flight inspection session."""
    data = _body_json(request)
    session_id = data.get("session_id")
    if not session_id:
        return _error("A session_id is required.")
    result = _workflow().cancel(str(session_id))
    return _json_response(result, status=200 if result.get("ok") else 400)


def _discover_configurable_classes(model):
    try:
        info = registry.inspect(model.model_path, task="segment")
    except ModelLoadError:
        return None
    if not info["ok"] or info.get("task") != "segment":
        return None
    return info["classes"]


@require_GET
def api_part_vision_configuration(request, part_id):
    part = get_object_or_404(VisionInspectionPart, pk=part_id)
    configuration = (
        VisionPartConfiguration.objects.filter(part=part)
        .order_by("-version")
        .first()
    )
    models = [
        {
            "id": model.id,
            "name": model.name,
            "version": model.version or "1.0",
            "num_classes": model.num_classes,
        }
        for model in VisionYOLOModel.objects.all().order_by("name", "id")
    ]
    config_data = None
    if configuration:
        config_data = {
            "id": configuration.id,
            "version": configuration.version,
            "name": configuration.name,
            "inspection_mode": configuration.inspection_mode,
            "front_camera_source": configuration.front_camera_source,
            "front_camera_index": configuration.front_camera_index,
            "back_camera_source": configuration.back_camera_source,
            "back_camera_index": configuration.back_camera_index,
            "acceptance_rule": configuration.acceptance_rule,
            "status": configuration.status,
            "is_active": configuration.is_active,
            "validation_errors": configuration.validation_errors,
            "roi_configured": configuration.rois.filter(is_active=True).exists(),
            "models": [
                {
                    "id": association.model_id,
                    "name": association.display_name or association.model.name,
                    "version": association.version,
                    "enabled": association.is_enabled,
                    "task_type": association.task_type,
                    "discovered_classes": association.classes,
                    "classes": [
                        {
                            "class_id": rule.class_id,
                            "class_name": rule.class_name,
                            "enabled": rule.is_enabled,
                            "side": rule.side,
                            "expected_front": rule.expected_front,
                            "expected_back": rule.expected_back,
                            "front_threshold": rule.front_threshold,
                            "back_threshold": rule.back_threshold,
                            "priority": rule.priority,
                            "required": rule.is_required,
                            "acceptance_rule": rule.acceptance_rule,
                        }
                        for rule in association.class_configs.order_by("priority", "class_id")
                    ],
                }
                for association in configuration.part_models.select_related("model").all()
            ],
        }
    return _json_response({
        "part": {"id": part.id, "number": part.part_number, "name": part.part_name},
        "configuration": config_data,
        "available_models": models,
    })


@require_GET
def api_part_vision_preview(request, part_id):
    """Infer and render predictions from the shared camera's latest frame."""
    part = get_object_or_404(VisionInspectionPart, pk=part_id)
    side = _clean(request.GET.get("side", "FRONT")).upper()

    def conflict(message, configuration_id=None):
        logger.warning(
            "Prediction preview conflict: part_id=%s configuration_id=%s side=%s reason=%s",
            part.pk,
            configuration_id if configuration_id is not None else "unavailable",
            side.lower(),
            message,
        )
        return _error(message, status=409)

    if side not in (VisionPartConfiguration.SIDE_FRONT, VisionPartConfiguration.SIDE_BACK):
        return _error("side must be FRONT or BACK.")
    try:
        confidence_threshold = float(request.GET.get("confidence", "0.50"))
    except (TypeError, ValueError):
        return _error("confidence must be a number between 0.10 and 0.95.")
    if not 0.10 <= confidence_threshold <= 0.95:
        return _error("confidence must be between 0.10 and 0.95.")
    show_masks = request.GET.get("show_masks", "1").lower() not in (
        "0", "false", "no",
    )
    show_boxes = request.GET.get("show_boxes", "1").lower() not in (
        "0", "false", "no",
    )

    configuration = (
        VisionPartConfiguration.objects.filter(part=part)
        .order_by("-version")
        .first()
    )
    if configuration is None:
        return conflict("No Part Vision configuration is saved.")
    if (
        side == VisionPartConfiguration.SIDE_FRONT
        and not configuration.requires_front
    ) or (
        side == VisionPartConfiguration.SIDE_BACK
        and not configuration.requires_back
    ):
        return conflict(f"The {side.title()} side is not enabled.", configuration.pk)

    model_id = _as_int(request.GET.get("model_id"), 0)
    part_models = configuration.part_models.filter(
        is_enabled=True,
        task_type=VisionPartModel.TASK_SEGMENT,
    ).select_related("model")
    if model_id:
        part_models = part_models.filter(model_id=model_id)
    association = part_models.order_by("id").first()
    if association is None:
        return conflict("No enabled segmentation model matches the requested model.", configuration.pk)

    expected_source = (
        configuration.front_camera_source
        if side == VisionPartConfiguration.SIDE_FRONT
        else configuration.back_camera_source
    )
    expected_index = (
        configuration.front_camera_index
        if side == VisionPartConfiguration.SIDE_FRONT
        else configuration.back_camera_index
    )
    source_ids = {
        VisionPartConfiguration.SOURCE_OPENCV: CAMERA_SOURCES["OPENCV"],
        VisionPartConfiguration.SOURCE_RPI: CAMERA_SOURCES["PICAMERA"],
    }
    camera_state = CameraService.get_state()
    if camera_state != "RUNNING":
        return conflict(
            f"The configured camera is {camera_state.lower()}; waiting for it to produce a frame.",
            configuration.pk,
        )
    current_source = CameraService.get_source()
    current_index = CameraService.get_camera_index()
    if current_source != source_ids.get(expected_source) or (
        current_source != CAMERA_SOURCES["PICAMERA"]
        and current_index != expected_index
    ):
        return conflict(
            f"The running camera does not match the configured {side.title()} camera source and index.",
            configuration.pk,
        )

    frame_ok, frame, frame_meta = CameraService.get_frame_with_meta()
    if not frame_ok or frame is None:
        return conflict("The running camera has not produced a frame yet.", configuration.pk)
    frame_height, frame_width = frame.shape[:2]
    roi = active_roi(configuration)
    roi_rect = roi.pixel_rect(frame_width, frame_height) if roi else None
    if roi is None:
        logger.warning(
            "Prediction preview rejected: part_id=%s configuration_id=%s side=%s "
            "frame_seq=%s roi_status=missing",
            part.pk,
            configuration.pk,
            side.lower(),
            frame_meta.get("seq"),
        )
        return conflict(f"No compatible ROI is configured for the {side.title()} side.", configuration.pk)
    roi_values = (roi.x, roi.y, roi.width, roi.height)
    roi_is_valid = (
        all(math.isfinite(float(value)) for value in roi_values)
        and roi.ref_width > 0
        and roi.ref_height > 0
        and roi.x >= 0
        and roi.y >= 0
        and roi.width > 0
        and roi.height > 0
        and roi.x + roi.width <= roi.ref_width
        and roi.y + roi.height <= roi.ref_height
    )
    if not roi_is_valid or roi_rect is None:
        logger.warning(
            "Prediction preview rejected: part_id=%s configuration_id=%s side=%s "
            "frame_seq=%s roi_id=%s roi_status=invalid frame=%sx%s",
            part.pk,
            configuration.pk,
            side.lower(),
            frame_meta.get("seq"),
            roi.pk,
            frame_width,
            frame_height,
        )
        return conflict(
            f"The configured ROI for the {side.title()} side is invalid for this camera frame.",
            configuration.pk,
        )
    offset_x = roi_rect["x"]
    offset_y = roi_rect["y"]
    inference_frame = frame[
        offset_y : offset_y + roi_rect["height"],
        offset_x : offset_x + roi_rect["width"],
    ]
    logger.debug(
        "Prediction preview inference started: part_id=%s configuration_id=%s "
        "side=%s frame_seq=%s roi_id=%s model_path=%s",
        part.pk,
        configuration.pk,
        side.lower(),
        frame_meta.get("seq"),
        roi.pk,
        association.model.model_path,
    )

    try:
        detections, rendered_roi, inference_ms = registry.predict_segmentation(
            association.model.model_path,
            inference_frame,
            conf=confidence_threshold,
            show_masks=show_masks,
            show_boxes=show_boxes,
        )
    except ModelLoadError as exc:
        logger.exception(
            "Prediction preview inference failed: part_id=%s configuration_id=%s "
            "side=%s frame_seq=%s roi_id=%s model_path=%s",
            part.pk,
            configuration.pk,
            side.lower(),
            frame_meta.get("seq"),
            roi.pk,
            association.model.model_path,
        )
        return _error(str(exc), status=503)
    logger.debug(
        "Prediction preview inference completed: part_id=%s configuration_id=%s "
        "side=%s frame_seq=%s roi_id=%s detections=%s inference_ms=%.1f",
        part.pk,
        configuration.pk,
        side.lower(),
        frame_meta.get("seq"),
        roi.pk,
        len(detections),
        inference_ms,
    )

    try:
        output_frame = frame.copy()
        if rendered_roi.shape[:2] != inference_frame.shape[:2]:
            return _error(
                "The segmentation renderer returned a frame with unexpected dimensions.",
                status=503,
            )
        if roi_rect:
            output_frame[
                offset_y : offset_y + roi_rect["height"],
                offset_x : offset_x + roi_rect["width"],
            ] = rendered_roi
        else:
            output_frame = rendered_roi
        image = _jpeg_data_url(output_frame, quality=82)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        logger.exception("Could not render segmentation preview for part %s", part.pk)
        return _error(f"Could not render preview: {exc}", status=503)
    if image is None:
        return _error("Could not encode the preview frame.", status=503)

    counts = {}
    for detection in detections:
        class_name = str(detection["class"])
        counts[class_name] = counts.get(class_name, 0) + 1
    return _json_response(
        {
            "ok": True,
            "configuration_id": configuration.pk,
            "configuration_version": configuration.version,
            "model_id": association.model_id,
            "side": side,
            "frame_seq": frame_meta.get("seq"),
            "roi_applied": bool(roi_rect),
            "image": image,
            "detection_count": len(detections),
            "detections": [
                {
                    "class": str(detection["class"]),
                    "class_id": int(detection["class_id"]),
                    "confidence": float(detection["confidence"]),
                    "confidence_percent": round(
                        float(detection["confidence"]) * 100, 1
                    ),
                    "x1": int(detection["x1"]) + offset_x,
                    "y1": int(detection["y1"]) + offset_y,
                    "x2": int(detection["x2"]) + offset_x,
                    "y2": int(detection["y2"]) + offset_y,
                }
                for index, detection in enumerate(detections)
            ],
            "counts_by_class": counts,
            "inference_ms": inference_ms,
        }
    )


@require_GET
def api_yolo_model_classes(request, model_id):
    model = get_object_or_404(VisionYOLOModel, pk=model_id)
    try:
        info = registry.inspect(model.model_path, task="segment")
    except ModelRuntimeUnavailableError as exc:
        return _error(str(exc), status=503)
    except ModelLoadError as exc:
        return _error(str(exc), status=422)
    if not info["ok"] or info.get("task") != "segment" or not info["classes"]:
        return _error(
            info.get("error")
            or "The registered model is not a segmentation model with discoverable classes.",
            status=422,
        )
    return _json_response({"model_id": model.id, "classes": info["classes"]})


@require_POST
def part_model_upload(request):
    """Safely register a user-uploaded YOLO model after real class discovery."""
    upload = request.FILES.get("model_file")
    if upload is None:
        return _error("Choose a model file to upload.", {"model_file": "A .pt or .onnx file is required."})

    suffix = Path(upload.name or "").suffix.lower()
    if suffix not in (".pt", ".onnx"):
        return _error("Unsupported model file type.", {"model_file": "Choose a .pt or .onnx model."})

    max_size = int(getattr(settings, "VISION_MAX_MODEL_UPLOAD_BYTES", 1024 * 1024 * 1024))
    if upload.size <= 0 or upload.size > max_size:
        return _error("Model file size is not allowed.", {"model_file": f"Maximum size is {max_size // (1024 * 1024)} MB."}, status=413)

    model_root = model_storage_root()
    try:
        model_root.mkdir(parents=True, exist_ok=True)
        destination = (model_root / f"{uuid.uuid4().hex}{suffix}").resolve()
        if destination.parent != model_root:
            return _error("Model storage path is invalid.", status=400)

        written = 0
        with destination.open("xb") as model_file:
            for chunk in upload.chunks():
                written += len(chunk)
                if written > max_size:
                    raise ValueError("Model file exceeded the upload limit.")
                model_file.write(chunk)

        if written != upload.size:
            raise ValueError("Uploaded model size did not match the received data.")

        model_info = registry.inspect(str(destination), task="segment")
        if not model_info["ok"] or model_info.get("task") != "segment":
            raise ValueError(
                model_info.get("error")
                or "The uploaded model is not a segmentation model."
            )
        classes = model_info["classes"]
        if not classes:
            raise ValueError("The model did not expose class metadata.")

        model_name = get_valid_filename(Path(upload.name).stem)[:100] or "YOLO model"
        version = _clean(request.POST.get("version"))[:50]
        model = VisionYOLOModel.objects.create(
            name=model_name,
            model_path=str(destination),
            version=version or None,
            num_classes=len(classes),
            is_active=False,
        )
    except ModelRuntimeUnavailableError as exc:
        try:
            if "destination" in locals():
                destination.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove rejected uploaded model")
        return _error(str(exc), status=503)
    except ModelLoadError as exc:
        try:
            if "destination" in locals():
                destination.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove rejected uploaded model")
        return _error(str(exc), status=422)
    except (OSError, ValueError) as exc:
        try:
            if "destination" in locals():
                destination.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove rejected uploaded model")
        return _error(str(exc), status=422)
    except Exception:
        try:
            if "destination" in locals():
                destination.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove failed model upload")
        logger.exception("Model registration failed after upload")
        return _error("The model could not be registered. No configuration was saved.", status=500)

    return _json_response({
        "ok": True,
        "model": {
            "id": model.pk,
            "name": model.name,
            "version": model.version or "1.0",
            "num_classes": model.num_classes,
            "is_active": model.is_active,
        },
        "classes": classes,
        "message": "Model uploaded, validated, and discovered successfully.",
    }, status=201)


@require_POST
def api_part_vision_configuration_save(request, part_id):
    part = get_object_or_404(VisionInspectionPart, pk=part_id)
    if not part.is_active:
        return _error("Configuration cannot be changed for an inactive part.", status=409)

    data = _body_json(request)
    if data.get("confirm_changes") is not True:
        return _error("Confirm that you want to save a draft configuration.",
                      {"confirm_changes": "Confirmation is required."})

    modes = {choice[0] for choice in VisionPartConfiguration.MODE_CHOICES}
    sources = {choice[0] for choice in VisionPartConfiguration.SOURCE_CHOICES}
    rules = {choice[0] for choice in VisionPartConfiguration._meta.get_field("acceptance_rule").choices}
    mode = data.get("inspection_mode")
    if mode not in modes:
        return _error("Inspection mode is invalid.", {"inspection_mode": "Select a supported mode."})

    models_data = data.get("models")
    if not isinstance(models_data, list) or not models_data:
        return _error("Select at least one registered model.", {"models": "A model is required."})
    model_ids = [item.get("model_id") for item in models_data if isinstance(item, dict)]
    if len(model_ids) != len(models_data) or any(
        not isinstance(model_id, int) or isinstance(model_id, bool) or model_id < 1
        for model_id in model_ids
    ):
        return _error("Model selections are invalid.", {"models": "Select registered models only."})
    if len(set(model_ids)) != len(model_ids):
        return _error("Model selections are duplicated.", {"models": "Use unique model selections."})

    selected_models = list(VisionYOLOModel.objects.filter(pk__in=model_ids))
    if len(selected_models) != len(model_ids):
        return _error("One or more selected models are not registered.", {"models": "Refresh the model list and try again."})
    model_enabled = {
        item["model_id"]: item.get("enabled", True) is True
        for item in models_data
    }

    discovered = {}
    classes_by_model = {}
    for model in selected_models:
        classes = _discover_configurable_classes(model)
        if classes is None or not classes:
            return _error("A selected model could not be validated.", {"models": "Class discovery failed; no changes were saved."}, status=422)
        discovered[model.pk] = [entry["name"] for entry in classes]
        classes_by_model[model.pk] = {entry["id"]: entry["name"] for entry in classes}

    normalized_rules = []
    priorities = set()
    for item in models_data:
        model_id = item["model_id"]
        class_rows = item.get("classes")
        if not isinstance(class_rows, list):
            return _error("Class settings are invalid.", {"classes": "Expected a list of class settings."})
        seen_class_ids = set()
        for row in class_rows:
            if not isinstance(row, dict):
                return _error("Class settings are invalid.")
            class_id = row.get("class_id")
            if not isinstance(class_id, int) or class_id not in classes_by_model[model_id] or class_id in seen_class_ids:
                return _error("A class does not match the selected model.", {"classes": "Refresh discovered classes and try again."})
            seen_class_ids.add(class_id)
            class_name = row.get("class_name")
            if class_name != classes_by_model[model_id][class_id]:
                return _error("A class name does not match the selected model.", {"classes": "Class metadata must match the model."})
            side = row.get("side")
            if side not in {choice[0] for choice in VisionPartConfiguration.SIDE_CHOICES}:
                return _error("Inspection side is invalid.", {"classes": "Select Front, Back, or Both."})
            if (mode == VisionPartConfiguration.MODE_FRONT_ONLY and side in ("BACK", "BOTH")) or (
                mode == VisionPartConfiguration.MODE_BACK_ONLY and side in ("FRONT", "BOTH")
            ):
                return _error("Class assignment conflicts with the part inspection mode.", {"classes": "Class sides must match the part mode."})
            try:
                expected_front = int(row.get("expected_front", 0))
                expected_back = int(row.get("expected_back", 0))
                priority = int(row.get("priority", 0))
                front_threshold = float(row.get("front_threshold", 0.5))
                back_threshold = float(row.get("back_threshold", 0.5))
            except (TypeError, ValueError):
                return _error("Class counts, priorities, and thresholds must be numeric.", {"classes": "Enter valid numeric settings."})
            if min(expected_front, expected_back) < 0 or priority < 1:
                return _error("Counts cannot be negative and priority must be positive.", {"classes": "Check counts and priority."})
            if not (0 <= front_threshold <= 1 and 0 <= back_threshold <= 1):
                return _error("Confidence thresholds must be between 0 and 1.", {"classes": "Check confidence thresholds."})
            if priority in priorities:
                return _error("Class priorities must be unique.", {"classes": "Use a different priority for each class."})
            priorities.add(priority)
            acceptance_rule = row.get("acceptance_rule") or ""
            if acceptance_rule and acceptance_rule not in rules:
                return _error("Acceptance rule is invalid.", {"classes": "Select a supported rule."})
            normalized_rules.append({
                "model_id": model_id, "class_id": class_id, "class_name": class_name,
                "enabled": row.get("enabled") is True, "side": side,
                "expected_front": expected_front, "expected_back": expected_back,
                "front_threshold": front_threshold, "back_threshold": back_threshold,
                "priority": priority, "required": row.get("required") is True,
                "acceptance_rule": acceptance_rule,
            })

    for key in ("front_camera_source", "back_camera_source"):
        if data.get(key) not in sources:
            return _error("Camera source is invalid.", {key: "Select a supported camera source."})
    if data.get("acceptance_rule") not in rules:
        return _error("Acceptance rule is invalid.", {"acceptance_rule": "Select a supported rule."})
    try:
        front_index = int(data.get("front_camera_index", 0))
        back_index = int(data.get("back_camera_index", 0))
    except (TypeError, ValueError):
        return _error("Camera indices must be whole numbers.", {"camera_index": "Enter a valid camera index."})
    if min(front_index, back_index) < 0 or max(front_index, back_index) > 32:
        return _error("Camera index is outside the supported range.", {"camera_index": "Use an index from 0 to 32."})

    configuration_id = data.get("configuration_id")
    if configuration_id:
        configuration = get_object_or_404(
            VisionPartConfiguration, pk=configuration_id, part=part, is_active=False
        )
    else:
        configuration = None

    try:
        with transaction.atomic():
            if configuration is None:
                last_version = VisionPartConfiguration.objects.filter(part=part).order_by("-version").values_list("version", flat=True).first() or 0
                configuration = VisionPartConfiguration(part=part, version=last_version + 1)
            configuration.name = _clean(data.get("name"))[:200] or f"{part.part_number} vision configuration"
            configuration.inspection_mode = mode
            configuration.front_camera_source = data["front_camera_source"]
            configuration.front_camera_index = front_index
            configuration.back_camera_source = data["back_camera_source"]
            configuration.back_camera_index = back_index
            configuration.acceptance_rule = data["acceptance_rule"]
            configuration.status = VisionPartConfiguration.STATUS_DRAFT
            configuration.validation_errors = []
            configuration.is_active = False
            configuration.configured_by = request.user.get_username() if request.user.is_authenticated else "anonymous operator"
            configuration.save()
            configuration.part_models.all().delete()
            associations = {}
            for model in selected_models:
                associations[model.pk] = VisionPartModel.objects.create(
                    configuration=configuration,
                    model=model,
                    display_name=model.name,
                    version=model.version or "1.0",
                    classes=[{"id": index, "name": name} for index, name in enumerate(discovered[model.pk])],
                    num_classes=len(discovered[model.pk]),
                    is_enabled=model_enabled[model.pk],
                    is_approved=False,
                    validation_status="VALID",
                )
            for row in normalized_rules:
                VisionClassConfiguration.objects.create(
                    part_model=associations[row["model_id"]],
                    class_id=row["class_id"], class_name=row["class_name"],
                    is_enabled=row["enabled"], side=row["side"],
                    expected_front=row["expected_front"], expected_back=row["expected_back"],
                    front_threshold=row["front_threshold"], back_threshold=row["back_threshold"],
                    priority=row["priority"], is_required=row["required"],
                    acceptance_rule=row["acceptance_rule"],
                )
            errors = validate_configuration(configuration, {
                association.pk: discovered[association.model_id]
                for association in associations.values()
            })
            configuration.validation_errors = errors
            configuration.status = (
                VisionPartConfiguration.STATUS_VALIDATION_FAILED
                if errors else VisionPartConfiguration.STATUS_READY
            )
            configuration.validation_errors = errors
            configuration.is_active = not errors
            configuration.save(update_fields=["validation_errors", "status", "is_active", "updated_at"])
    except IntegrityError:
        return _error("The draft could not be saved because its configuration conflicts with another version.", status=409)

    return _json_response({
        "ok": True, "configuration_id": configuration.pk,
        "version": configuration.version, "status": configuration.status,
        "is_active": configuration.is_active,
        "validation_errors": configuration.validation_errors,
        "message": (
            "Model configuration saved and enabled for this part."
            if configuration.is_active
            else "Configuration saved as a draft; validation must pass before inspection."
        ),
    }, status=201)


# --------------------------------------------------------------------------- #
#  YOLO model registry + inference
# --------------------------------------------------------------------------- #


@require_GET
def yolo_models_list(request):
    """List registered YOLO models (plain JSON array, consumed by configuration.js)."""
    models = [
        {
            "id": m.id,
            "name": m.name,
            "version": m.version or "1.0",
            "part_class": m.part_class or "",
            "num_classes": m.num_classes,
            "is_active": m.is_active,
            "model_path": m.model_path,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in VisionYOLOModel.objects.all().order_by("-created_at")
    ]
    return _json_response(models)


@require_POST
def yolo_model_upload(request):
    """Register and verify a supported YOLO model from its saved path."""
    data = _body_json(request)
    name = _clean(data.get("name"))
    model_path = _clean(data.get("model_path"))
    if not name or not model_path:
        return _error("Both a model name and a model path are required.")

    try:
        model_path = str(resolve_model_path(model_path))
    except ModelPathResolutionError as exc:
        return _error(str(exc), {"model_path": str(exc)})

    if Path(model_path).suffix.lower() not in (".pt", ".onnx"):
        return _error(
            "Unsupported model format.",
            {"model_path": "Choose a .pt or .onnx model file."},
        )
    if not os.path.isfile(model_path):
        return _error(
            f"The model file does not exist. Configured path: {data.get('model_path')}. Checked path: {model_path}.",
            {"model_path": "Upload the model to the configured model-storage directory or correct this path."},
        )

    try:
        model_info = registry.inspect(model_path)
    except ModelRuntimeUnavailableError as exc:
        return _error(str(exc), status=503)
    except ModelLoadError as exc:
        return _error(str(exc), status=422)
    if not model_info["ok"]:
        return _error(
            model_info.get("error") or "The model could not be inspected.",
            status=422,
        )

    model = VisionYOLOModel.objects.create(
        name=name,
        model_path=model_path,
        version=_clean(data.get("version")) or None,
        part_class=_clean(data.get("part_class")) or None,
        num_classes=model_info["num_classes"],
    )

    return _json_response(
        {
            "ok": True,
            "id": model.id,
            "name": model.name,
            "loaded": True,
            "message": "Model registered and loaded successfully.",
        },
        status=201,
    )


def _detect_with_active_model(frame):
    service = YOLOModelService()
    model = _active_model()
    if model is None:
        return None, "No active inspection model is configured."
    try:
        model_path = str(resolve_model_path(model.model_path))
    except ModelPathResolutionError as exc:
        return None, f"The configured model path is invalid: {exc}"
    if not service.is_loaded() or service.get_model_path() != model_path:
        if not service.load_model(model_path):
            return None, f"The configured model could not be loaded: {model_path}"
    return service, None


@require_GET
def yolo_model_test(request):
    """Run the active model against the current camera frame."""
    frame = _camera_frame_or_none()
    if frame is None:
        return _json_response(
            {"ok": False, "count": 0, "message": "The camera is not running."}
        )
    service, message = _detect_with_active_model(frame)
    if service is None:
        return _json_response({"ok": False, "count": 0, "message": message})
    detections = service.detect(frame)
    return _json_response(
        {
            "ok": True,
            "count": len(detections),
            "detections": detections,
            "classes": service.get_class_names(),
        }
    )


@require_POST
def yolo_detect(request):
    """Run detection on the live frame and return the raw detections."""
    data = _body_json(request)
    frame = _camera_frame_or_none()
    if frame is None and data.get("image"):
        payload_bytes = _decode_image_payload(data.get("image"))
        if payload_bytes:
            try:
                import cv2
                import numpy as np

                frame = cv2.imdecode(
                    np.frombuffer(payload_bytes, dtype=np.uint8), cv2.IMREAD_COLOR
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Could not decode submitted image: %s", exc)
                frame = None
    if frame is None:
        return _error("No frame available. Start the camera before detecting.", 409)

    service, message = _detect_with_active_model(frame)
    if service is None:
        return _error(message, status=409)
    model = _active_model()
    detections = service.detect(
        frame,
        conf=_as_float(getattr(model, "confidence_threshold", None), 0.5),
    )
    return _json_response(
        {
            "ok": True,
            "count": len(detections),
            "detections": detections,
            "classes": service.get_class_names(),
        }
    )


# --------------------------------------------------------------------------- #
#  YOLO training (Celery + Redis)
# --------------------------------------------------------------------------- #


@require_POST
def training_start(request):
    """Queue a YOLO training job for a dataset/project."""
    data = _body_json(request)
    project = _clean(data.get("training_project"))
    if not project:
        return _error("A training project / dataset is required.")

    epochs = _as_int(data.get("epochs"), 100)
    if epochs < 1:
        return _error("epochs must be at least 1.")

    job = VisionTrainingJob.objects.create(
        training_project=project,
        status=VisionTrainingJob.STATUS_QUEUED,
        total_epochs=epochs,
        message="Queued for training.",
    )

    if not _training_backend_available():
        job.status = VisionTrainingJob.STATUS_FAILED
        job.error_message = (
            "Training backend unavailable: the celery/redis packages are not "
            "installed on this host."
        )
        job.message = job.error_message
        job.completed_at = timezone_now()
        job.save(
            update_fields=["status", "error_message", "message", "completed_at", "updated_at"]
        )
        return _json_response(
            {
                "error": job.error_message,
                "job_id": job.pk,
                "status": job.status,
                "ok": False,
            },
            status=503,
        )

    try:
        from VisionInspection.tasks import train_yolo_model

        async_result = train_yolo_model.delay(job.pk)
        job.celery_task_id = getattr(async_result, "id", "") or ""
        job.message = "Training queued."
        job.save(update_fields=["celery_task_id", "message", "updated_at"])
    except Exception as exc:
        logger.exception("Could not queue training job %s", job.pk)
        job.status = VisionTrainingJob.STATUS_FAILED
        job.error_message = f"Could not queue the training job: {exc}"
        job.message = job.error_message
        job.completed_at = timezone_now()
        job.save(
            update_fields=["status", "error_message", "message", "completed_at", "updated_at"]
        )
        return _json_response(
            {"error": job.error_message, "job_id": job.pk, "ok": False},
            status=503,
        )

    return _json_response(_job_payload(job), status=201)


def timezone_now():
    from django.utils import timezone

    return timezone.now()


def training_status(request, job_id):
    job = get_object_or_404(VisionTrainingJob, pk=job_id)
    return _json_response(_job_payload(job))


@require_POST
def training_cancel(request, job_id):
    job = get_object_or_404(VisionTrainingJob, pk=job_id)
    if job.status in (
        VisionTrainingJob.STATUS_COMPLETED,
        VisionTrainingJob.STATUS_FAILED,
        VisionTrainingJob.STATUS_CANCELLED,
    ):
        return _json_response(
            {"ok": False, "message": f"Job already {job.status.lower()}."},
            status=409,
        )

    if job.celery_task_id and _training_backend_available():
        try:
            from celery import current_app

            current_app.control.revoke(job.celery_task_id, terminate=True)
        except Exception as exc:  # pragma: no cover - broker unreachable
            logger.warning("Could not revoke task %s: %s", job.celery_task_id, exc)

    job.status = VisionTrainingJob.STATUS_CANCELLED
    job.message = "Training cancelled by user."
    job.completed_at = timezone_now()
    job.save(
        update_fields=["status", "message", "completed_at", "updated_at"]
    )
    return _json_response(
        {"ok": True, "job_id": job.pk, "message": job.message}
    )


@require_GET
def training_jobs_list(request):
    page, page_size = _pagination_params(request)
    paginator = Paginator(
        VisionTrainingJob.objects.all().order_by("-created_at"), page_size
    )
    page_obj = paginator.get_page(page)
    return _json_response(
        {
            "items": [_job_payload(j) for j in page_obj.object_list],
            "total": paginator.count,
            "pages": paginator.num_pages,
            "page": page_obj.number,
            "page_size": page_size,
        }
    )


@require_GET
def training_dataset_paths(request):
    """Discover local YOLO dataset directories (no datasets are invented)."""
    from django.conf import settings

    root = str(getattr(settings, "VISION_TRAINING_DATA_DIR", "") or "")
    datasets = []
    if root and os.path.isdir(root):
        for entry in sorted(os.listdir(root)):
            path = os.path.join(root, entry)
            if not os.path.isdir(path):
                continue
            try:
                file_count = sum(len(files) for _r, _d, files in os.walk(path))
            except OSError:  # pragma: no cover - unreadable dir
                file_count = 0
            datasets.append(
                {
                    "name": entry,
                    "path": path,
                    "num_files": file_count,
                    "has_yaml": any(
                        f.endswith((".yaml", ".yml"))
                        for f in os.listdir(path)
                        if os.path.isfile(os.path.join(path, f))
                    ),
                }
            )
    return _json_response({"root": root, "datasets": datasets})


def _integrity_fields(exc, kind):
    """Translate a unique-constraint violation into a fields map."""
    text = str(exc).lower()
    if "part_number_normalized" in text or "vision_inspection_part.part_number" in text:
        return {"part_number": "A part with this number already exists."}
    if "name_normalized" in text or "vision_inspection_employee.employee_code" in text:
        return {"employee_name": "An employee with this name already exists."}
    return {}


# --------------------------------------------------------------------------- #
#  Part ROI Setup — the ONE common ROI of a part configuration
#
#  A part configuration has a single ROI.  There are no per-model, per-class
#  or per-side ROI editors: the same rectangle is reused by every enabled
#  model and for both the Front and Back images.  Coordinates are stored in
#  the source-frame pixel system with the frame size they were drawn against.
# --------------------------------------------------------------------------- #


def _roi_payload(roi):
    """JSON view of a saved part ROI (pixel + normalized values)."""
    if roi is None:
        return None
    normalized = roi.normalized
    return {
        "id": roi.pk,
        "configuration_id": roi.configuration_id,
        "version": roi.version,
        "x": round(float(roi.x)),
        "y": round(float(roi.y)),
        "width": round(float(roi.width)),
        "height": round(float(roi.height)),
        "ref_width": roi.ref_width,
        "ref_height": roi.ref_height,
        "is_active": roi.is_active,
        "configured_by": roi.configured_by,
        "normalized": {
            "x": normalized["x"],
            "y": normalized["y"],
            "width": normalized["width"],
            "height": normalized["height"],
        },
        "created_at": roi.created_at.isoformat() if roi.created_at else None,
        "updated_at": roi.updated_at.isoformat() if roi.updated_at else None,
    }


def _configuration_for_part(part, configuration_id=None):
    """The configuration an ROI belongs to (latest version by default)."""
    configurations = VisionPartConfiguration.objects.filter(part=part).order_by("-version")
    if configuration_id:
        return configurations.filter(pk=_as_int(configuration_id, 0)).first()
    return configurations.first()


@require_GET
def api_part_vision_roi(request, part_id):
    """Read the single common ROI of a part configuration (or ``saved: false``)."""
    part = get_object_or_404(VisionInspectionPart, pk=part_id)
    configuration = _configuration_for_part(part, request.GET.get("configuration_id"))
    if configuration is None:
        return _json_response(
            {
                "saved": False,
                "part": {"id": part.pk, "number": part.part_number, "name": part.part_name},
                "configuration": None,
                "message": "This part has no vision configuration yet.",
            }
        )
    roi = active_roi(configuration)
    return _json_response(
        {
            "saved": roi is not None,
            "part": {"id": part.pk, "number": part.part_number, "name": part.part_name},
            "configuration": {
                "id": configuration.pk,
                "version": configuration.version,
                "status": configuration.status,
                "inspection_mode": configuration.inspection_mode,
            },
            "roi": _roi_payload(roi),
        }
    )


@require_POST
def api_part_vision_roi_save(request, part_id):
    """Save (version) the single common ROI of a part configuration."""
    part = get_object_or_404(VisionInspectionPart, pk=part_id)
    data = _body_json(request)
    configuration = _configuration_for_part(part, data.get("configuration_id"))
    if configuration is None:
        return _error(
            "This part has no vision configuration yet. Save the model "
            "configuration before drawing the ROI.",
            status=409,
        )

    ref_width = _as_int(data.get("ref_width"), 0)
    ref_height = _as_int(data.get("ref_height"), 0)
    x = _as_float(data.get("x"), 0.0)
    y = _as_float(data.get("y"), 0.0)
    width = _as_float(data.get("width"), 0.0)
    height = _as_float(data.get("height"), 0.0)

    if not all(math.isfinite(value) for value in (x, y, width, height)):
        return _error("ROI coordinates and dimensions must be finite numbers.")
    if ref_width <= 0 or ref_height <= 0:
        return _error(
            "The camera frame dimensions are required.",
            {"ref_width": "Start the camera so the frame size is known."},
        )
    if width <= 2 or height <= 2:
        return _error("The ROI is too small — draw a larger region.")
    if x < 0 or y < 0 or x + width > ref_width or y + height > ref_height:
        return _error("The ROI extends beyond the camera frame.")

    with transaction.atomic():
        locked_configuration = (
            VisionPartConfiguration.objects.select_for_update()
            .filter(pk=configuration.pk, part=part)
            .first()
        )
        if locked_configuration is None:
            return _error(
                "This part has no vision configuration yet. Save the model "
                "configuration before drawing the ROI.",
                status=409,
            )
        configuration = locked_configuration
        camera_error = common_roi_camera_error(configuration)
        if camera_error:
            return _error(camera_error, status=409)

        previous = (
            VisionPartROI.objects.select_for_update()
            .filter(configuration=configuration, is_active=True)
            .order_by("-version")
            .first()
        )
        last_version = (
            VisionPartROI.objects.filter(configuration=configuration)
            .order_by("-version")
            .values_list("version", flat=True)
            .first()
            or 0
        )
        version = last_version + 1
        if previous is not None:
            previous.is_active = False
            previous.save(update_fields=["is_active", "updated_at"])
        roi = VisionPartROI.objects.create(
            configuration=configuration,
            version=version,
            x=x,
            y=y,
            width=width,
            height=height,
            ref_width=ref_width,
            ref_height=ref_height,
            is_active=True,
            configured_by=(
                request.user.get_username()
                if getattr(request.user, "is_authenticated", False)
                else _station_id()
            ),
        )
        # The saved ROI keeps the configuration draft until every required
        # setting validates; the status is refreshed from real validation.
        errors = validate_configuration(configuration)
        configuration.validation_errors = errors
        configuration.status = (
            VisionPartConfiguration.STATUS_READY
            if not errors
            else VisionPartConfiguration.STATUS_VALIDATION_FAILED
        )
        configuration.is_active = configuration.is_active or not errors
        configuration.save(
            update_fields=["validation_errors", "status", "is_active", "updated_at"]
        )

    return _json_response(
        {
            "ok": True,
            "saved": True,
            "message": f"Common ROI v{version} saved for {part.part_number}.",
            "roi": _roi_payload(roi),
            "configuration_status": configuration.status,
        },
        status=201,
    )


# --------------------------------------------------------------------------- #
#  data.yaml validation — optional; never replaces the model's own classes
# --------------------------------------------------------------------------- #


@require_POST
def api_part_data_yaml_validate(request, part_id):
    """Compare an uploaded ``data.yaml`` with the model's real classes."""
    part = get_object_or_404(VisionInspectionPart, pk=part_id)
    upload = request.FILES.get("data_yaml") or request.FILES.get("data_file")
    if upload is None:
        return _error(
            "Choose a data.yaml file to validate.",
            {"data_yaml": "A data.yaml file is required."},
        )
    if not str(upload.name or "").lower().endswith((".yaml", ".yml")):
        return _error(
            "Unsupported file type.",
            {"data_yaml": "Upload a .yaml or .yml dataset file."},
        )
    if upload.size > 1024 * 512:
        return _error("The data.yaml file is too large.", status=413)

    model_id = _as_int(_clean(request.POST.get("model_id")), 0)
    model = (
        VisionYOLOModel.objects.filter(pk=model_id).first()
        if model_id
        else None
    )
    if model is None:
        return _error("Select the model this data.yaml belongs to.", {"model_id": "Required."})

    try:
        text = upload.read().decode("utf-8", "replace")
    except Exception:  # pragma: no cover - defensive
        return _error("The data.yaml file could not be read.")

    model_classes = _discover_configurable_classes(model)
    if not model_classes:
        return _error(
            "The selected model could not be inspected for class discovery.",
            status=422,
        )
    result = validate_data_yaml(text, model_classes)
    return _json_response(
        {
            "ok": result["ok"],
            "model": {"id": model.pk, "name": model.name},
            "model_classes": model_classes,
            "yaml_classes": result["classes"],
            "errors": result["errors"],
            "warnings": result["warnings"],
            "message": (
                "data.yaml matches the model classes."
                if result["ok"]
                else "data.yaml does not match the model classes."
            ),
        }
    )
