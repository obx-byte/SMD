"""Dashboard aggregation service for the VisionInspection operations dashboard.

This module provides server-side, zero-safe aggregation helpers used by the
JSON dashboard endpoint.  It intentionally performs bulk aggregation queries
(``values().annotate()``) so that card/chart data is drawn from real records
without per-row queries (no N+1) and without loading the entire table.

Domain note
-----------
This application is an **AI vision-inspection** system.  Where a warehouse
dashboard would show "packages processed", this service aggregates completed
*inspections*; where it would show SKU inventory, it reports the configuration
coverage of the part numbers that were actually inspected.  No inventory or
production values are fabricated — every figure below is derived from existing
``VisionInspection`` records.
"""

import logging
from datetime import timedelta

from django.db.models import Count, Q
from django.urls import reverse
from django.utils import timezone

from VisionInspection.models import (
    VisionInspectionLog,
    VisionInspectionPreset,
    VisionROI,
    VisionTrainingJob,
)

logger = logging.getLogger(__name__)

# VisionInspectionLog.result choices (see models.py).  Defined locally because
# the model does not expose named constants for these.
RESULT_OK = "OK"
RESULT_NOT_OK = "NOT_OK"
RESULT_ERROR = "ERROR"


def _day_bounds(day):
    """Return ``(start, end)`` datetimes for the whole local ``day``."""
    start = timezone.make_aware(
        timezone.datetime(day.year, day.month, day.day, 0, 0, 0)
    )
    return start, start + timedelta(days=1)


def _in_range(logs, start, end):
    return logs.filter(timestamp__gte=start, timestamp__lt=end)


def _pct_change(current, previous):
    """Percentage change, or ``None`` when there is no previous baseline."""
    if not previous or previous <= 0:
        return None
    return round(((current - previous) / previous) * 100.0, 1)


def _apply_filters(logs, part_number=None, result=None):
    if part_number:
        logs = logs.filter(part_number__iexact=part_number)
    if result in (RESULT_OK, RESULT_NOT_OK, RESULT_ERROR):
        logs = logs.filter(result=result)
    return logs


# --------------------------------------------------------------------------- #
#  A. Inspection flow (package-flow equivalent)
# --------------------------------------------------------------------------- #


def get_inspection_flow(days=1, part_number=None, result=None):
    """Inspections in the selected period vs the immediately preceding period.

    Returns the current/previous totals, a percentage change, a per-part
    ("zone"-equivalent) breakdown and an intra-period trend series.
    """
    all_logs = _apply_filters(
        VisionInspectionLog.objects.all(), part_number=part_number, result=result
    )

    now = timezone.now().astimezone(timezone.get_current_timezone())
    today = now.date()
    days = max(1, int(days or 1))

    # Selected period: [start, end); preceding period: [prev_start, start)
    current_start, current_end = _day_bounds(today - timedelta(days=days - 1))
    current_end = now if days == 1 else current_end
    period = timedelta(days=days)
    prev_start = current_start - period
    prev_end = current_start

    current_count = _in_range(all_logs, current_start, current_end).count()
    previous_count = _in_range(all_logs, prev_start, prev_end).count()
    change_pct = _pct_change(current_count, previous_count)

    # Per-part breakdown (top 6) for the selected period.
    by_part = (
        _in_range(all_logs, current_start, current_end)
        .values("part_number")
        .annotate(count=Count("id"))
        .order_by("-count")[:6]
    )
    part_breakdown = [
        {
            "part_number": row["part_number"] or "UNKNOWN",
            "count": row["count"],
        }
        for row in by_part
    ]

    # Intra-period trend: hourly for a single day, daily for multi-day periods.
    trend = _build_trend(all_logs, current_start, current_end, days)

    return {
        "label": "Today" if days == 1 else f"Last {days} days",
        "current": current_count,
        "previous": previous_count,
        "change_pct": change_pct,
        "by_part": part_breakdown,
        "trend": trend,
        "period_start": current_start.isoformat(),
        "period_end": current_end.isoformat(),
    }


def _build_trend(logs, start, end, days):
    """Build a bucketed trend series spanning ``[start, end)``."""
    span_seconds = max((end - start).total_seconds(), 1)
    rows = _in_range(logs, start, end).values_list("timestamp", flat=True)

    if days == 1:
        # Hourly buckets for the selected day (00:00 .. current hour).
        num_buckets = 24
        labels = [f"{h:02d}:00" for h in range(num_buckets)]
        counts = [0] * num_buckets
        for ts in rows:
            local_ts = ts.astimezone(timezone.get_current_timezone())
            counts[local_ts.hour] += 1
    else:
        # Daily buckets, one per day in the period.
        num_buckets = days
        labels = []
        counts = [0] * num_buckets
        day_index = {}
        for i in range(num_buckets):
            day = (start + timedelta(days=i)).date()
            day_index[day] = i
            labels.append(day.strftime("%d %b"))
        for ts in rows:
            local_day = ts.astimezone(timezone.get_current_timezone()).date()
            idx = day_index.get(local_day)
            if idx is not None:
                counts[idx] += 1

    return {"labels": labels, "counts": counts, "span_seconds": span_seconds}


# --------------------------------------------------------------------------- #
#  B. AI inspection results
# --------------------------------------------------------------------------- #


def get_inspection_summary(days=1, part_number=None):
    """Passed / failed / error totals with zero-safe percentages."""
    logs = _apply_filters(
        VisionInspectionLog.objects.all(), part_number=part_number
    )

    now = timezone.now().astimezone(timezone.get_current_timezone())
    days = max(1, int(days or 1))
    start, end = _day_bounds(now.date() - timedelta(days=days - 1))
    if days == 1:
        end = now
    logs = _in_range(logs, start, end)

    agg = logs.values("result").annotate(count=Count("id"))
    counts = {row["result"]: row["count"] for row in agg}

    passed = counts.get(RESULT_OK, 0)
    failed = counts.get(RESULT_NOT_OK, 0)
    error = counts.get(RESULT_ERROR, 0)
    total = passed + failed + error

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "error": error,
        "pass_pct": round((passed / total) * 100.0, 1) if total else 0.0,
        "fail_pct": round(((failed + error) / total) * 100.0, 1) if total else 0.0,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
    }


# --------------------------------------------------------------------------- #
#  C. Part inventory / configuration health
# --------------------------------------------------------------------------- #


def get_part_inventory(limit=5, days=1, part_number=None):
    """Configuration coverage for the most relevant part numbers.

    Each entry exposes the quantity inspected for the period plus the stock-
    style thresholds required by the dashboard: the reorder threshold (reported
    by active/passing preset coverage) and the critical threshold (reported by
    failing/absent configuration).  Rows are ranked so the most urgent items
    appear first.
    """
    now = timezone.now().astimezone(timezone.get_current_timezone())
    days = max(1, int(days or 1))
    start, end = _day_bounds(now.date() - timedelta(days=days - 1))
    if days == 1:
        end = now

    query = VisionInspectionLog.objects.all()
    if part_number:
        query = query.filter(part_number__iexact=part_number)
    rows = _in_range(query, start, end).values("part_number").annotate(
        quantity=Count("id"),
        failures=Count("id", filter=Q(result=RESULT_NOT_OK))
        + Count("id", filter=Q(result=RESULT_ERROR)),
    )
    rows = sorted(
        rows, key=lambda r: (-r["failures"], -r["quantity"], r["part_number"] or "")
    )[:limit]

    parts = []
    for row in rows:
        part_no = row["part_number"] or "UNKNOWN"
        quantity = row["quantity"]
        failures = row["failures"]

        presets = VisionInspectionPreset.objects.filter(part_number=part_no)
        active_presets = presets.filter(is_active=True).count()
        if active_presets:
            coverage = 100.0
        else:
            configured = (
                VisionROI.objects.filter(preset__part_number=part_no)
                .filter(is_active=True)
                .count()
            )
            coverage = 60.0 if configured else 20.0

        parts.append(
            {
                "part_number": part_no,
                "quantity": quantity,
                "failures": failures,
                "active_presets": active_presets,
                "total_presets": presets.count(),
                "coverage": coverage,
                "health": _health_label(quantity, coverage, failures),
            }
        )

    return {"parts": parts, "threshold": {"reorder": 60.0, "critical": 20.0}}


def _health_label(quantity, coverage, failures):
    """A warehouse-style threshold label for the inspected quantity."""
    if quantity == 0:
        return "IDLE"
    if coverage <= 20 or failures == quantity:
        return "CRITICAL"
    if coverage < 60 or failures > 0:
        return "LOW"
    return "SAFE"


# --------------------------------------------------------------------------- #
#  D. Anomaly detection alerts
# --------------------------------------------------------------------------- #


def get_recent_alerts(limit=6, part_number=None):
    """Recent, real anomalies: inspection errors, failed/queued trainings.

    Empty until real anomaly records exist.
    """
    alerts = []

    # Inspection errors.
    inspection_qs = VisionInspectionLog.objects.filter(result=RESULT_ERROR)
    if part_number:
        inspection_qs = inspection_qs.filter(part_number__iexact=part_number)
    for log in inspection_qs.order_by("-timestamp")[:limit]:
        try:
            log_url = reverse("VisionInspection:history-detail", args=[log.pk])
        except Exception:
            log_url = None
        alerts.append(
            {
                "id": f"insp-{log.pk}",
                "type": "Inspection error",
                "severity": "CRITICAL",
                "message": log.error_details or "Inspection engine reported a NOT-OK result",
                "location": log.part_number or log.preset_name or "Inspection",
                "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                "status": "Open",
                "url": log_url,
            }
        )

    # Failed / stuck training jobs.
    training_qs = VisionTrainingJob.objects.all()
    if part_number:
        training_qs = training_qs.filter(
            Q(model_version__icontains=part_number)
            | Q(training_project__icontains=part_number)
        )
    try:
        training_url = reverse("VisionInspection:ui-training-jobs")
    except Exception:
        training_url = None
    for job in (
        training_qs.filter(status=VisionTrainingJob.STATUS_FAILED).order_by(
            "-created_at"
        )[:limit]
    ):
        alerts.append(
            {
                "id": f"train-{job.pk}",
                "type": "Training job failed",
                "severity": "CRITICAL",
                "message": job.error_message or job.message or "Training did not complete",
                "location": job.training_project or "AI Insights",
                "timestamp": (
                    job.completed_at or job.updated_at or job.created_at
                ).isoformat(),
                "status": job.status,
                "url": training_url,
            }
        )

    cancelled = (
        training_qs.filter(status=VisionTrainingJob.STATUS_CANCELLED).order_by(
            "-created_at"
        )[:limit]
    )
    for job in cancelled:
        alerts.append(
            {
                "id": f"cancel-{job.pk}",
                "type": "Training job cancelled",
                "severity": "WARNING",
                "message": job.message or "Training was cancelled before completion",
                "location": job.training_project or "AI Insights",
                "timestamp": (job.updated_at or job.created_at).isoformat(),
                "status": job.status,
                "url": training_url,
            }
        )

    # Newest first, capped.
    alerts.sort(key=lambda a: a["timestamp"] or "", reverse=True)
    alerts = alerts[:limit]
    return {"alerts": alerts}


# --------------------------------------------------------------------------- #
#  Combined payload
# --------------------------------------------------------------------------- #


def get_dashboard_payload(days=1, part_number=None):
    """Assemble the complete dashboard payload consumed by ``dashboard.js``."""
    days = max(1, int(days or 1))
    try:
        return {
            "generated_at": timezone.now().isoformat(),
            "filters": {"days": days, "part_number": part_number or ""},
            "flow": get_inspection_flow(days=days, part_number=part_number),
            "inspection": get_inspection_summary(days=days, part_number=part_number),
            "inventory": get_part_inventory(days=days, part_number=part_number),
            "alerts": get_recent_alerts(part_number=part_number),
        }
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to build dashboard payload")
        raise
