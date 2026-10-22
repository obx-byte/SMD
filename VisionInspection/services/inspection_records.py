"""Read-only inspection-record helpers shared by the Dashboard and the
Inspection Report pages.

Both pages show the same persisted records (``VisionInspectionLog``) from the
SQLite database and enrich every record with the real data that belongs to it:

* part name — the part master (``VisionInspectionPart``)
* employee — the employee master (``VisionInspectionEmployee``)
* front / back verdict and the scanned unique number — the Airbag Stitch
  session that produced the record (``VisionInspectionSession``)

Nothing is invented: when a related record is missing the helpers report the
absence so the templates can show it honestly.  Every value read from the query
string is cleaned and bounded here, so the pages never trust raw input.
"""

import re
import uuid as uuid_module
from urllib.parse import urlencode

from django.db.models import Count, Q
from django.utils import timezone

from VisionInspection.models import (
    VisionInspectionEmployee,
    VisionInspectionLog,
    VisionInspectionPart,
    VisionInspectionSession,
)

# Result values actually stored on ``VisionInspectionLog.result``.
RESULT_OK = "OK"
RESULT_NOT_OK = "NOT_OK"
RESULT_ERROR = "ERROR"
RESULT_INCOMPLETE = "INCOMPLETE"
RESULT_CHOICES = (RESULT_OK, RESULT_NOT_OK, RESULT_INCOMPLETE, RESULT_ERROR)
RESULT_INCOMPLETE_ERROR = "INCOMPLETE_ERROR"

# Longest values accepted from the query string (model limits + a margin).
DATE_MAX = 10
PART_NUMBER_MAX = 40
PART_NAME_MAX = 200
EMPLOYEE_MAX = 100
UNIQUE_NUMBER_MAX = 80
RESULT_MAX = 20

PAGE_SIZES = (10, 25, 50, 100)
DEFAULT_PAGE_SIZE = 10
RECENT_LIMIT = 6

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SESSION_PREFIX = "SESSION#"


def clean(value, max_length):
    """Trim *value* to plain text and cap it at *max_length* characters."""
    if value is None:
        return ""
    return str(value).strip()[:max_length]


def clean_date(value):
    """Return ``YYYY-MM-DD`` only when *value* really is such a date."""
    value = clean(value, DATE_MAX)
    return value if _DATE_RE.match(value) else ""


def read_filters(params):
    """Normalised filter set of a Dashboard / Report query string.

    ``part``        — part selected in the dropdown (exact part number)
    ``part_number`` — free-text part-number search
    ``part_name``   — free-text part-name search
    """
    return {
        "date_from": clean_date(params.get("date_from")),
        "date_to": clean_date(params.get("date_to")),
        "part": clean(params.get("part"), PART_NUMBER_MAX),
        "part_number": clean(params.get("part_number"), PART_NUMBER_MAX),
        "part_name": clean(params.get("part_name"), PART_NAME_MAX),
        "employee": clean(params.get("employee"), EMPLOYEE_MAX),
        "unique_number": clean(params.get("unique_number"), UNIQUE_NUMBER_MAX),
        "result": clean(params.get("result"), RESULT_MAX).upper(),
        "front_result": clean(params.get("front_result"), RESULT_MAX).upper(),
        "back_result": clean(params.get("back_result"), RESULT_MAX).upper(),
    }


def read_page_size(params):
    """Page size from the query string, limited to the offered choices."""
    try:
        size = int(clean(params.get("page_size"), 4))
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return size if size in PAGE_SIZES else DEFAULT_PAGE_SIZE


def active_filters(filters):
    """Only the filters that actually restrict the result set."""
    return {key: value for key, value in filters.items() if value}


def query_string(filters, **extra):
    """Encode the active filters so exports keep the same selection."""
    params = active_filters(filters)
    params.update({key: value for key, value in extra.items() if value})
    return urlencode(params)


def _session_ids_from_preset_names(names):
    """Valid session UUIDs behind ``SESSION#<uuid>`` preset names."""
    ids = []
    for name in names:
        if not name or not name.startswith(_SESSION_PREFIX):
            continue
        raw = name[len(_SESSION_PREFIX):]
        try:
            ids.append(str(uuid_module.UUID(raw)))
        except (ValueError, AttributeError, TypeError):
            continue
    return ids


def session_presets_for_unique_number(value):
    """Preset names of the sessions whose unique number matches *value*.

    The unique number is the barcode scanned at inspection time; the session id
    is accepted as well so a record can also be found by its own identifier.
    """
    sessions = VisionInspectionSession.objects.filter(
        barcode__icontains=value
    ) | VisionInspectionSession.objects.filter(session_id__icontains=value)
    names = [f"{_SESSION_PREFIX}{session.session_id}" for session in sessions]
    return [name for name in names if _session_ids_from_preset_names([name])]


def _logs_for_sessions(logs, sessions):
    """Match both current FK-linked rows and legacy SESSION# preset rows."""
    ids = sessions.values_list("session_id", flat=True)
    names = [
        f"{_SESSION_PREFIX}{session_id}"
        for session_id in sessions.values_list("session_id", flat=True)
    ]
    return logs.filter(Q(session__session_id__in=ids) | Q(preset_name__in=names))


def _side_filter(logs, side, value):
    if not value:
        return logs
    accepted = (
        (RESULT_INCOMPLETE, RESULT_ERROR, "PENDING")
        if value == RESULT_INCOMPLETE_ERROR
        else (value,)
    )
    sessions = VisionInspectionSession.objects.filter(
        **{f"{side}_verdict__in": accepted}
    )
    return _logs_for_sessions(logs, sessions)


def filter_logs(logs, filters):
    """Apply *filters* to a ``VisionInspectionLog`` queryset."""
    if logs is None:
        logs = VisionInspectionLog.objects.all()
    filters = filters or {}

    if filters.get("part"):
        logs = logs.filter(part_number__iexact=filters["part"])

    if filters.get("result") in RESULT_CHOICES:
        logs = logs.filter(result=filters["result"])
    elif filters.get("result") == RESULT_INCOMPLETE_ERROR:
        logs = logs.filter(result__in=(RESULT_INCOMPLETE, RESULT_ERROR))

    logs = _side_filter(logs, "front", filters.get("front_result", ""))
    logs = _side_filter(logs, "back", filters.get("back_result", ""))

    if filters.get("employee"):
        logs = logs.filter(employee_code__iexact=filters["employee"])

    if filters.get("part_name"):
        part_numbers = VisionInspectionPart.objects.filter(
            part_name__icontains=filters["part_name"]
        ).values_list("part_number", flat=True)
        logs = logs.filter(part_number__in=list(part_numbers))

    if filters.get("part_number"):
        logs = logs.filter(part_number__icontains=filters["part_number"])

    if filters.get("unique_number"):
        sessions = VisionInspectionSession.objects.filter(
            Q(barcode__icontains=filters["unique_number"])
            | Q(session_id__icontains=filters["unique_number"])
        )
        logs = logs.filter(
            Q(barcode__icontains=filters["unique_number"])
            | Q(session__in=sessions)
            | Q(preset_name__in=session_presets_for_unique_number(filters["unique_number"]))
        )

    if filters.get("date_from"):
        logs = logs.filter(timestamp__date__gte=filters["date_from"])

    if filters.get("date_to"):
        logs = logs.filter(timestamp__date__lte=filters["date_to"])

    return logs


def summarize(logs):
    """Summary counts of *logs*, all taken from the same filtered queryset."""
    logs = logs if logs is not None else VisionInspectionLog.objects.all()
    counts = logs.aggregate(
        total=Count("pk"),
        ok=Count("pk", filter=Q(result=RESULT_OK)),
        not_ok=Count("pk", filter=Q(result=RESULT_NOT_OK)),
        incomplete_error=Count(
            "pk", filter=Q(result__in=(RESULT_INCOMPLETE, RESULT_ERROR))
        ),
    )
    total = counts["total"]
    ok = counts["ok"]
    not_ok = counts["not_ok"]
    incomplete_error = counts["incomplete_error"]

    def percentage(value):
        return round(100.0 * value / total, 1) if total else 0.0

    return {
        "total": total,
        "ok": ok,
        "not_ok": not_ok,
        "incomplete_error": incomplete_error,
        "ok_pct": percentage(ok),
        "not_ok_pct": percentage(not_ok),
        "incomplete_error_pct": percentage(incomplete_error),
    }


def part_names():
    """``{part_number: part_name}`` of the part master."""
    return {
        part.part_number: part.part_name
        for part in VisionInspectionPart.objects.all()
    }


def employees_by_code():
    """``{employee_code: employee}`` of the employee master."""
    return {
        employee.employee_code: employee
        for employee in VisionInspectionEmployee.objects.all()
    }


def sessions_for(logs):
    """Sessions behind the ``SESSION#<uuid>`` preset name of *logs*."""
    ids = _session_ids_from_preset_names(
        [log.preset_name or "" for log in logs]
    )
    if not ids:
        return {}
    found = VisionInspectionSession.objects.filter(session_id__in=ids)
    return {
        f"{_SESSION_PREFIX}{session.session_id}": session for session in found
    }


def side_result(session, side):
    """Verdict of one inspection side, exactly as it was recorded.

    ``""`` means the record was not produced by an Airbag Stitch session, so no
    per-side verdict exists for it at all.
    """
    if session is None:
        return ""
    verdict = getattr(session, f"{side}_verdict", "") or ""
    if verdict == RESULT_OK:
        return RESULT_OK
    if verdict == RESULT_NOT_OK:
        return "NOT OK"
    if verdict == "ERROR":
        return "Error"
    if verdict == "NOT_REQUIRED":
        return "Not required"
    return "Incomplete"


def build_rows(logs, parts=None, employees=None, sessions=None):
    """Presentation rows for *logs*, enriched with the related real records."""
    logs = list(logs)
    parts = part_names() if parts is None else parts
    employees = employees_by_code() if employees is None else employees
    sessions = sessions_for(logs) if sessions is None else sessions

    rows = []
    from VisionInspection.services.storage_service import image_url
    for log in logs:
        session = getattr(log, "session", None) or sessions.get(log.preset_name or "")
        employee = employees.get(log.employee_code or "")
        part_name = parts.get(log.part_number or "", "")
        employee_code = log.employee_code or ""
        employee_name = (
            employee.employee_name if employee
            else (getattr(log, "employee_name", "") or (session.operator.get_full_name() if session and session.operator_id else ""))
        )
        # The unique number is the scanned barcode of the inspected unit —
        # stored on the record itself when the station saved it, otherwise read
        # back from the session that produced the record.
        barcode = getattr(log, "barcode", "") or ""
        unique_number = barcode or (session.barcode if session else "") or ""
        front = side_result(session, "front")
        back = side_result(session, "back")
        front_image_url = image_url(session, "front_image") if session else ""
        back_image_url = image_url(session, "back_image") if session else ""
        tone = result_tone(log.result)
        rows.append(
            {
                "log": log,
                "inspection_id": log.pk,
                "session": session,
                "front_image_url": front_image_url,
                "back_image_url": back_image_url,
                "front_image_download_url": f"{front_image_url}?download=1" if front_image_url else "",
                "back_image_download_url": f"{back_image_url}?download=1" if back_image_url else "",
                "part_name": part_name,
                "employee_code": employee_code,
                "employee_name": employee_name,
                "employee_label": (
                    f"{employee_code} — {employee_name}" if employee_name
                    else (employee_code or "—")
                ),
                "unique_number": unique_number,
                "result_label": result_text(log.result),
                "result_tone": tone,
                "result_badge": badge_class(tone),
                "front_result": front,
                "front_label": side_label(front),
                "front_tone": side_tone(front),
                "front_badge": badge_class(side_tone(front)),
                "back_result": back,
                "back_label": side_label(back),
                "back_tone": side_tone(back),
                "back_badge": badge_class(side_tone(back)),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
#  Presentation labels (one definition shared by page, Excel and PDF exports)
# --------------------------------------------------------------------------- #

BADGE_CLASSES = {
    "ok": "vi-badge-ok",
    "not_ok": "vi-badge-not-ok",
    "error": "vi-badge-error",
    "incomplete": "vi-badge-pending",
}


def badge_class(tone):
    """Badge CSS class of a result tone (shared theme classes)."""
    return BADGE_CLASSES.get(tone, "vi-badge-skip")


def result_text(result):
    """Result text of a stored result value, exactly as it is stored."""
    return {
        RESULT_OK: "OK",
        RESULT_NOT_OK: "NOT OK",
        RESULT_ERROR: "Error",
        RESULT_INCOMPLETE: "INCOMPLETE",
        RESULT_INCOMPLETE_ERROR: "Incomplete / Error",
    }.get(result, result or "—")


def result_label(log):
    """Result text of a record, exactly as it is stored."""
    return result_text(log.result)


def result_tone(result):
    """Colour tone of a stored result value."""
    return {
        RESULT_OK: "ok",
        RESULT_NOT_OK: "not_ok",
        RESULT_ERROR: "error",
        RESULT_INCOMPLETE: "incomplete",
    }.get(result, "")


def side_label(value):
    """Text of a front / back verdict — ``—`` when it was never recorded."""
    return value or "—"


def side_tone(value):
    """Colour tone of a front / back verdict."""
    return {
        "OK": "ok",
        "NOT OK": "not_ok",
        "Error": "error",
        "Incomplete": "incomplete",
    }.get(value, "")


def export_rows(rows):
    """Export rows in report column order — ``(text, tone)`` per cell."""
    export = []
    for index, row in enumerate(rows, start=1):
        log = row["log"]
        export.append(
            [
                (str(index), ""),
                (str(log.pk), ""),
                (timezone.localtime(log.timestamp).strftime("%Y-%m-%d %H:%M:%S"), ""),
                (log.part_number or "—", ""),
                (row["part_name"] or "—", ""),
                (row["employee_label"], ""),
                (row["unique_number"] or "—", ""),
                (row["result_label"], row["result_tone"]),
                (row["front_label"], row["front_tone"]),
                (row["back_label"], row["back_tone"]),
            ]
        )
    return export


def export_headers():
    """Column headers shared by the report page and its exports."""
    return [
        "#",
        "Inspection ID",
        "Date & Time",
        "Part No.",
        "Part Name",
        "Employee",
        "Unique No.",
        "Result",
        "Front Result",
        "Back Result",
    ]
