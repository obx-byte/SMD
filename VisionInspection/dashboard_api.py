"""JSON API views backing the operations dashboard.

These views are intentionally free of heavy computer-vision imports so the
dashboard can be served and polled independently of the camera / YOLO stack.
"""

import json
import logging

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from VisionInspection.services import dashboard_service

logger = logging.getLogger(__name__)

VALID_RESULTS = ("OK", "NOT_OK", "ERROR")
MAX_DAYS = 30


def _validate_days(raw):
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return 1, None
    if days < 1 or days > MAX_DAYS:
        return None, f"days must be between 1 and {MAX_DAYS}"
    return days, None


def _validate_part(raw):
    if raw is None:
        return "", None
    part = str(raw).strip()
    if len(part) > 40:
        return None, "part_number must be at most 40 characters"
    return part, None


def _validate_result(raw):
    if not raw:
        return "", None
    result = str(raw).strip().upper()
    if result not in VALID_RESULTS:
        return None, f"result must be one of {', '.join(VALID_RESULTS)}"
    return result, None


@require_GET
def dashboard_summary(request):
    """Return the aggregated dashboard payload.

    Query parameters (all optional and validated):
        days          — aggregation window in days (1..30, default 1)
        part_number   — filter by exact part number
        result        — filter flow by OK / NOT_OK / ERROR
    """
    days, err = _validate_days(request.GET.get("days"))
    if err:
        return JsonResponse({"error": err}, status=400)

    part_number, err = _validate_part(request.GET.get("part_number"))
    if err:
        return JsonResponse({"error": err}, status=400)

    result, err = _validate_result(request.GET.get("result"))
    if err:
        return JsonResponse({"error": err}, status=400)

    try:
        payload = dashboard_service.get_dashboard_payload(
            days=days, part_number=part_number
        )
        if result:
            payload["flow"] = dashboard_service.get_inspection_flow(
                days=days, part_number=part_number, result=result
            )
            payload["filters"]["result"] = result
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("dashboard_summary failed")
        return JsonResponse(
            {"error": "Failed to build dashboard data", "detail": str(exc)},
            status=500,
        )

    body = payload
    body["ok"] = True
    return JsonResponse(body, status=200, safe=False)
