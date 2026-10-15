"""Part-wise vision configuration service.

Owns the part configuration lifecycle: validation, versioning, the immutable
snapshot frozen into every inspection session, and the per-class acceptance
rules used to turn real detections into a verdict.

Rules enforced here (never bypassed, never guessed):

* A class may only reference a class that exists in the uploaded model.
* A class assigned to BACK cannot require a FRONT check, and vice versa.
* A class assigned to BOTH needs the part-level mode to enable both sides.
* Priorities must be unique per enabled class so ordering is unambiguous.
* A configuration is READY only when every required setting validated.
"""

import logging
import threading

from django.db import transaction
from django.utils import timezone

from VisionInspection.models import (
    VisionClassConfiguration,
    VisionPartConfiguration,
    VisionPartModel,
    VisionPartROI,
    VisionInspectionPart,
    VisionYOLOModel,
)
from VisionInspection.services.model_registry import ModelRegistry, registry

logger = logging.getLogger(__name__)

SIDES = ("front", "back")


def _clean(value):
    return str(value or "").strip()


class ConfigurationError(Exception):
    """Raised when a configuration payload cannot be accepted."""


# --------------------------------------------------------------------------- #
#  Access
# --------------------------------------------------------------------------- #


def active_configuration(part_number):
    """The active configuration of *part_number* (or ``None``)."""
    if not part_number:
        return None
    return (
        VisionPartConfiguration.objects.filter(
            part__part_number__iexact=_clean(part_number), is_active=True
        )
        .select_related("part")
        .first()
    )


def active_roi(configuration):
    """The single common ROI of *configuration* (or ``None``)."""
    if configuration is None:
        return None
    return (
        VisionPartROI.objects.filter(configuration=configuration, is_active=True)
        .order_by("-version")
        .first()
    )


def enabled_part_models(configuration):
    """Enabled (and approved) models of a configuration, in stable order."""
    if configuration is None:
        return VisionPartModel.objects.none()
    return (
        configuration.part_models.filter(is_enabled=True, is_approved=True)
        .select_related("model")
        .order_by("id")
    )


def class_rows(configuration, include_unapproved=False):
    """Enabled class rules of a configuration ordered by priority.

    Returns one row per configured class with the model identity attached, so
    two models that share a class name stay distinguishable.
    """
    if configuration is None:
        return []
    rows = []
    part_models = (
        configuration.part_models.filter(is_enabled=True)
        .select_related("model")
        .order_by("id")
        if include_unapproved
        else enabled_part_models(configuration)
    )
    for part_model in part_models:
        for rule in part_model.class_configs.filter(is_enabled=True).order_by(
            "priority", "class_id"
        ):
            rows.append(
                {
                    "part_model": part_model,
                    "rule": rule,
                    "model_name": part_model.display_name or part_model.model.name,
                    "model_version": part_model.version
                    or (part_model.model.version or ""),
                }
            )
    rows.sort(key=lambda row: (row["rule"].priority, row["rule"].class_id))
    return rows


def camera_roles(configuration):
    """Required camera roles with their exclusive backend.

    Exactly one backend per role: ``{"front": {...}, "back": {...}}`` where
    ``source`` is ``RPI`` or ``OPENCV`` and ``index`` the device index.
    """
    if configuration is None:
        return {}
    roles = {}
    if configuration.requires_front:
        roles["front"] = {
            "source": configuration.front_camera_source,
            "index": int(configuration.front_camera_index or 0),
        }
    if configuration.requires_back:
        roles["back"] = {
            "source": configuration.back_camera_source,
            "index": int(configuration.back_camera_index or 0),
        }
    return roles


def common_roi_camera_error(configuration):
    """Return why a common ROI cannot be shared by the configured cameras."""
    roles = camera_roles(configuration)
    if "front" not in roles or "back" not in roles:
        return ""
    front = roles["front"]
    back = roles["back"]
    same_source = front["source"] == back["source"]
    same_opencv_index = (
        front["source"] != VisionPartConfiguration.SOURCE_OPENCV
        or front["index"] == back["index"]
    )
    if not same_source or not same_opencv_index:
        return (
            "A common ROI requires the Front and Back roles to use the same "
            "camera source and, for OpenCV cameras, the same index. Configure "
            "matching cameras before saving the ROI."
        )
    return ""


# --------------------------------------------------------------------------- #
#  Validation
# --------------------------------------------------------------------------- #


def validate_configuration(configuration, discovered=None):
    """Return the list of validation errors of *configuration*.

    ``discovered`` optionally maps ``part_model_id -> [class names]`` freshly
    read from the uploaded model file, so stale class metadata is detected
    instead of silently accepted.  An empty list means the configuration is
    ready for inspection.
    """
    if configuration is None:
        return ["No vision configuration exists for this part."]

    errors = []
    mode = configuration.inspection_mode
    if mode not in dict(VisionPartConfiguration.MODE_CHOICES):
        errors.append("The inspection mode is not a supported value.")

    part_models = list(configuration.part_models.all())
    enabled = [pm for pm in part_models if pm.is_enabled]
    if not enabled:
        errors.append("At least one enabled model is required.")

    for part_model in enabled:
        label = part_model.display_name or part_model.model.name
        stored = list(part_model.classes or [])
        if not stored:
            errors.append(
                f"Model '{label}' has no discovered classes. Re-upload it so its "
                f"classes can be read."
            )
        if discovered and part_model.pk in discovered:
            fresh = [str(name) for name in (discovered[part_model.pk] or [])]
            stored_names = [str(item.get("name")) for item in stored]
            if fresh and fresh != stored_names:
                errors.append(
                    f"The discovered classes of model '{label}' no longer matches "
                    f"the model file ({', '.join(fresh)}). Re-upload the model to "
                    f"refresh its classes."
                )

    # Class-level checks (only enabled classes of enabled models).
    priorities = {}
    count_rules = (
        VisionClassConfiguration.RULE_COUNT,
        VisionClassConfiguration.RULE_COUNT_CONFIDENCE,
    )
    configured_rules = class_rows(configuration, include_unapproved=True)
    for row in configured_rules:
        rule = row["rule"]
        label = f"{row['model_name']} / {rule.class_name}"
        owner = row["part_model"]
        stored = list(owner.classes or [])
        known_ids = {int(item.get("id")) for item in stored if isinstance(item, dict)}
        if int(rule.class_id) not in known_ids:
            errors.append(
                f"Class '{label}' does not exist in model "
                f"'{owner.display_name or owner.model.name}'. Stale class "
                f"references must be removed."
            )
            continue
        for item in stored:
            if int(item.get("id")) == int(rule.class_id) and str(
                item.get("name")
            ) != str(rule.class_name):
                errors.append(
                    f"The stored class name of '{rule.class_name}' no longer matches "
                    f"the model file ('{item.get('name')}')."
                )

        if int(rule.priority) in priorities and priorities[int(rule.priority)] != label:
            errors.append(
                f"Priority {int(rule.priority)} is used by more than one class "
                f"('{priorities[int(rule.priority)]}' and '{label}'). Priorities "
                f"must be unique."
            )
        priorities[int(rule.priority)] = label

        for side in SIDES:
            expected = rule.expected_for(side)
            threshold = rule.threshold_for(side)
            if expected < 0:
                errors.append(f"Expected count for '{label}' ({side}) cannot be negative.")
            if not 0.0 <= threshold <= 1.0:
                errors.append(
                    f"Confidence threshold for '{label}' ({side}) must be between 0 and 1."
                )
            if expected and threshold <= 0.0:
                errors.append(
                    f"'{label}' ({side}) needs a confidence threshold when a "
                    f"detection is expected."
                )

        if rule.side == VisionPartConfiguration.SIDE_BOTH:
            if not configuration.requires_front or not configuration.requires_back:
                errors.append(
                    f"Class '{rule.class_name}' is assigned to Both, but the part "
                    f"inspection mode is "
                    f"{configuration.get_inspection_mode_display()}. Change the "
                    f"class side or the part mode."
                )
        elif rule.side == VisionPartConfiguration.SIDE_BACK and not configuration.requires_back:
            errors.append(
                f"Class '{rule.class_name}' requires Back inspection, but the part "
                f"inspection mode is {configuration.get_inspection_mode_display()}."
            )
        elif rule.side == VisionPartConfiguration.SIDE_FRONT and not configuration.requires_front:
            errors.append(
                f"Class '{rule.class_name}' requires Front inspection, but the part "
                f"inspection mode is {configuration.get_inspection_mode_display()}."
            )

        acceptance_rule = rule.acceptance_rule or configuration.acceptance_rule
        for side in SIDES:
            if (
                rule.is_required
                and rule.applies_to(side)
                and acceptance_rule in count_rules
                and rule.expected_for(side) == 0
            ):
                errors.append(
                    f"Required class '{rule.class_name}' has no expected count for "
                    f"the {side} side."
                )

    enabled_rules = [row["rule"] for row in configured_rules]
    for side in SIDES:
        if not getattr(configuration, f"requires_{side}"):
            continue
        side_rules = [rule for rule in enabled_rules if rule.applies_to(side)]
        if any(rule.is_required for rule in side_rules):
            continue
        if side_rules:
            class_names = ", ".join(
                f"'{rule.class_name}'" for rule in side_rules
            )
            errors.append(
                f"No required stitch class is configured for the {side.title()} "
                f"inspection. Enabled class rules ({class_names}) are all optional. "
                "In Part Vision, "
                f"mark at least one {side.title()} or Both class as Required and "
                "set its expected count and inspection rule."
            )
        else:
            available_classes = ", ".join(
                f"{part_model.display_name or part_model.model.name} / "
                f"'{item['name']}'"
                for part_model in enabled
                for item in (part_model.classes or [])
                if isinstance(item, dict) and item.get("name")
            )
            class_detail = (
                f" Discovered classes available for assignment: {available_classes}."
                if available_classes
                else ""
            )
            errors.append(
                f"No required stitch class is configured for the {side.title()} "
                f"inspection. No enabled class rule is assigned to the "
                f"{side.title()} inspection. In Part Vision, assign a discovered "
                f"model class to {side.title()} or Both, mark it Required, and "
                f"set its expected count and inspection rule.{class_detail}"
            )

    # Camera source exclusivity: exactly one backend per role.
    roles = camera_roles(configuration)
    if not roles:
        errors.append("The part inspection mode requires no camera role.")
    for role, spec in roles.items():
        if spec["source"] not in (
            VisionPartConfiguration.SOURCE_OPENCV,
            VisionPartConfiguration.SOURCE_RPI,
        ):
            errors.append(f"Camera source for {role} must be OPENCV or RPI.")

    # The single common ROI.  A missing ROI does not invalidate the saved
    # configuration (it is configured separately in ROI Setup) but it does
    # block inspection — the submit path enforces that.
    roi = active_roi(configuration)
    if roi is not None and roi.pixel_rect(roi.ref_width, roi.ref_height) is None:
        errors.append("The saved ROI is outside the source frame or too small.")
    if roi is not None:
        camera_error = common_roi_camera_error(configuration)
        if camera_error:
            errors.append(camera_error)

    return errors


def configuration_state(configuration, discovered=None):
    """The derived status + errors of a configuration.

    ``READY`` only when nothing failed; ``VALIDATION_FAILED`` when something
    did; ``DRAFT`` when the configuration exists but is incomplete.
    """
    errors = validate_configuration(configuration, discovered)
    if configuration is None:
        return {"status": VisionPartConfiguration.STATUS_NOT_CONFIGURED, "errors": errors}
    if errors:
        return {"status": VisionPartConfiguration.STATUS_VALIDATION_FAILED, "errors": errors}
    return {"status": VisionPartConfiguration.STATUS_READY, "errors": []}


def store_validation(configuration, errors):
    """Persist the derived status + errors on the configuration row."""
    configuration.validation_errors = list(errors or [])
    configuration.status = (
        VisionPartConfiguration.STATUS_READY
        if not configuration.validation_errors
        else VisionPartConfiguration.STATUS_VALIDATION_FAILED
    )
    if configuration.validation_errors:
        configuration.is_active = False
    configuration.save(
        update_fields=["status", "validation_errors", "is_active", "updated_at"]
    )
    return configuration


# --------------------------------------------------------------------------- #
#  Snapshot (immutable reference stored on every session)
# --------------------------------------------------------------------------- #


def config_snapshot(configuration):
    """Freeze the exact configuration used by an inspection session."""
    if configuration is None:
        return {}
    roi = active_roi(configuration)
    models_payload = []
    for part_model in configuration.part_models.all():
        models_payload.append(
            {
                "part_model_id": part_model.pk,
                "model_id": part_model.model_id,
                "name": part_model.display_name or part_model.model.name,
                "version": part_model.version or (part_model.model.version or ""),
                "task_type": part_model.task_type,
                "enabled": part_model.is_enabled,
                "approved": part_model.is_approved,
                "num_classes": part_model.num_classes,
                "classes": list(part_model.classes or []),
                "model_path": part_model.model.model_path,
            }
        )
    classes_payload = []
    for row in class_rows(configuration):
        rule = row["rule"]
        classes_payload.append(
            {
                "priority": int(rule.priority),
                "part_model_id": row["part_model"].pk,
                "model_id": row["part_model"].model_id,
                "model_name": row["model_name"],
                "model_version": row["model_version"],
                "class_id": int(rule.class_id),
                "class_name": rule.class_name,
                "side": rule.side,
                "required": rule.is_required,
                "acceptance_rule": rule.acceptance_rule or configuration.acceptance_rule,
                "expected_front": int(rule.expected_front),
                "expected_back": int(rule.expected_back),
                "front_threshold": float(rule.front_threshold),
                "back_threshold": float(rule.back_threshold),
            }
        )
    return {
        "configuration_id": configuration.pk,
        "configuration_version": int(configuration.version),
        "part_number": configuration.part.part_number,
        "part_name": configuration.part.part_name,
        "inspection_mode": configuration.inspection_mode,
        "front_camera": {
            "source": configuration.front_camera_source,
            "index": int(configuration.front_camera_index or 0),
        },
        "back_camera": {
            "source": configuration.back_camera_source,
            "index": int(configuration.back_camera_index or 0),
        },
        "single_camera_reposition_confirmation": bool(
            configuration.single_camera_reposition_confirmation
        ),
        "acceptance_rule": configuration.acceptance_rule,
        "models": models_payload,
        "classes": classes_payload,
        "roi": (
            {
                "roi_id": roi.pk,
                "version": int(roi.version),
                "x": float(roi.x),
                "y": float(roi.y),
                "width": float(roi.width),
                "height": float(roi.height),
                "ref_width": int(roi.ref_width),
                "ref_height": int(roi.ref_height),
            }
            if roi is not None
            else None
        ),
    }


def required_sides(snapshot):
    """Sides a snapshot actually requires, from the part mode."""
    mode = (snapshot or {}).get("inspection_mode")
    if mode == VisionPartConfiguration.MODE_FRONT_ONLY:
        return ["front"]
    if mode == VisionPartConfiguration.MODE_BACK_ONLY:
        return ["back"]
    if mode == VisionPartConfiguration.MODE_FRONT_BACK:
        return ["front", "back"]
    return []


# --------------------------------------------------------------------------- #
#  Save / update (drafts allowed, READY only after full validation)
# --------------------------------------------------------------------------- #


def save_configuration(part, payload, configured_by=""):
    """Create or update the configuration of *part* from a JSON payload.

    A payload with ``version`` updates that version in place; otherwise the
    next version is created.  Incomplete payloads are stored as DRAFT — only a
    fully valid configuration becomes READY.
    """
    part = _as_part(part)
    payload = payload or {}
    mode = _clean(payload.get("inspection_mode")).upper() or VisionPartConfiguration.MODE_FRONT_BACK
    if mode not in dict(VisionPartConfiguration.MODE_CHOICES):
        raise ConfigurationError("inspection_mode must be FRONT_ONLY, BACK_ONLY or FRONT_BACK.")

    with transaction.atomic():
        configuration = _resolve_configuration(part, payload)
        configuration.inspection_mode = mode
        configuration.front_camera_source = _source_or_default(
            payload.get("front_camera_source")
        )
        configuration.front_camera_index = _as_int(payload.get("front_camera_index"), 0)
        configuration.back_camera_source = _source_or_default(
            payload.get("back_camera_source")
        )
        configuration.back_camera_index = _as_int(payload.get("back_camera_index"), 1)
        configuration.single_camera_reposition_confirmation = _as_bool(
            payload.get("single_camera_reposition_confirmation"), True
        )
        configuration.acceptance_rule = _rule_or_default(payload.get("acceptance_rule"))
        configuration.configured_by = _clean(configured_by)
        if not configuration.name:
            configuration.name = f"{part.part_number} configuration"
        configuration.save()

        _apply_class_payloads(configuration, payload.get("classes") or [])
        _apply_model_payloads(configuration, payload.get("models") or [])

    result = configuration_state(configuration)
    store_validation(configuration, result["errors"])
    return configuration, result


def _resolve_configuration(part, payload):
    version = payload.get("version")
    if version:
        existing = VisionPartConfiguration.objects.filter(
            part=part, version=_as_int(version, 0)
        ).first()
        if existing is not None:
            return existing
    highest = (
        VisionPartConfiguration.objects.filter(part=part)
        .order_by("-version")
        .values_list("version", flat=True)
        .first()
    )
    return VisionPartConfiguration(part=part, version=(highest or 0) + 1)


def _apply_model_payloads(configuration, models):
    """Enable/disable/approve existing part models (models are added via upload)."""
    for item in models:
        if not isinstance(item, dict):
            continue
        part_model_id = _as_int(item.get("part_model_id"), 0)
        if not part_model_id:
            continue
        part_model = configuration.part_models.filter(pk=part_model_id).first()
        if part_model is None:
            continue
        updates = []
        if "is_enabled" in item:
            part_model.is_enabled = _as_bool(item.get("is_enabled"), part_model.is_enabled)
            updates.append("is_enabled")
        if "is_approved" in item:
            part_model.is_approved = _as_bool(
                item.get("is_approved"), part_model.is_approved
            )
            updates.append("is_approved")
        if "display_name" in item:
            part_model.display_name = _clean(item.get("display_name"))
            updates.append("display_name")
        if "version" in item:
            part_model.version = _clean(item.get("version"))
            updates.append("version")
        if updates:
            updates.append("updated_at")
            part_model.save(update_fields=updates)


def _apply_class_payloads(configuration, classes):
    """Upsert per-class rules; reject references to unknown model classes."""
    allowed_models = {
        pm.pk: {int(c.get("id")) for c in pm.classes or []}
        for pm in configuration.part_models.all()
    }
    for item in classes:
        if not isinstance(item, dict):
            continue
        part_model_id = _as_int(item.get("part_model_id"), 0)
        class_id = _as_int(item.get("class_id"), -1)
        if part_model_id not in allowed_models:
            raise ConfigurationError(
                "A class rule references a model that is not attached to this part."
            )
        if class_id not in allowed_models[part_model_id]:
            raise ConfigurationError(
                f"Class id {class_id} does not exist in the selected model."
            )
        rule, _created = VisionClassConfiguration.objects.update_or_create(
            part_model_id=part_model_id,
            class_id=class_id,
            defaults={
                "class_name": _clean(item.get("class_name")) or str(class_id),
                "is_enabled": _as_bool(item.get("is_enabled"), True),
                "side": _side_or_default(item.get("side")),
                "expected_front": max(0, _as_int(item.get("expected_front"), 0)),
                "expected_back": max(0, _as_int(item.get("expected_back"), 0)),
                "front_threshold": _clamp01(item.get("front_threshold"), 0.5),
                "back_threshold": _clamp01(item.get("back_threshold"), 0.5),
                "priority": max(1, _as_int(item.get("priority"), 1)),
                "is_required": _as_bool(item.get("is_required"), True),
                "acceptance_rule": _clean(item.get("acceptance_rule")) or "",
            },
        )
        if "is_enabled" in item and not rule.is_enabled:
            continue


# --------------------------------------------------------------------------- #
#  Acceptance rules — real detections only
# --------------------------------------------------------------------------- #


def evaluate_class(rule_payload, side, detections):
    """Evaluate one configured class on one side from real detections.

    *detections* are the detections of the owning model restricted to this
    class.  The result never invents a confidence: absent detections give
    ``None`` and the status follows the configured rule.
    """
    expected = int(rule_payload.get("expected_front" if side == "front" else "expected_back", 0))
    threshold = float(
        rule_payload.get("front_threshold" if side == "front" else "back_threshold", 0.5)
    )
    acceptance = _clean(rule_payload.get("acceptance_rule")) or "count_confidence"
    required = bool(rule_payload.get("required", True))

    kept = [d for d in detections or [] if float(d.get("confidence", 0.0)) >= threshold]
    actual = len(kept)
    best = max((float(d.get("confidence", 0.0)) for d in kept), default=None)
    result = {
        "count": actual,
        "expected": expected,
        "best_confidence": (round(best, 4) if best is not None else None),
        "detections": [
            {
                "confidence": round(float(d.get("confidence", 0.0)), 4),
                "box": [d.get("x1"), d.get("y1"), d.get("x2"), d.get("y2")],
            }
            for d in kept
        ],
        "status": "PENDING",
        "reason": "",
    }

    if not required and expected == 0:
        result["status"] = "NOT_REQUIRED"
        result["reason"] = "Class is optional and has no expected count."
        return result

    if acceptance in ("count", "count_confidence"):
        if actual < expected:
            result["status"] = "NOT_OK"
            result["reason"] = (
                f"Expected {expected} detection(s) at confidence >= {threshold:.2f}, "
                f"found {actual}."
            )
            return result
    if acceptance in ("presence", "count_confidence"):
        if actual == 0:
            result["status"] = "NOT_OK"
            result["reason"] = "Required class was not detected."
            return result
    if acceptance == "confidence":
        if best is None:
            result["status"] = "NOT_OK"
            result["reason"] = "No detection reached the configured confidence."
            return result

    result["status"] = "OK"
    result["reason"] = (
        f"{actual} detection(s) at confidence >= {threshold:.2f}; "
        f"best {result['best_confidence']:.2f}."
    )
    return result


def side_verdict(class_results):
    """Combine per-class results into a side verdict.

    An incomplete or error side is never reported OK; a missing required
    detection is NOT OK; a NOT REQUIRED class never fails the opposite side.
    """
    statuses = [row.get("status") for row in class_results or []]
    if "ERROR" in statuses:
        return "ERROR"
    required_rows = [row for row in class_results or [] if row.get("required")]
    if not required_rows:
        return "INCOMPLETE"
    if any(row.get("status") == "ERROR" for row in required_rows):
        return "ERROR"
    if any(row.get("status") == "NOT_OK" for row in required_rows):
        return "NOT_OK"
    if any(row.get("status") in ("PENDING", "INCOMPLETE") for row in required_rows):
        return "INCOMPLETE"
    return "OK"


# --------------------------------------------------------------------------- #
#  data.yaml validation (optional; never replaces model classes)
# --------------------------------------------------------------------------- #


def validate_data_yaml(text, model_classes):
    """Compare an uploaded ``data.yaml`` with the model's real classes.

    Returns ``{ok, errors, warnings, classes}``.  The model's own class names
    always win — the YAML is only checked for missing / reordered / mismatched
    entries, which are reported instead of being silently applied.
    """
    errors, warnings = [], []
    yaml_classes = _parse_yaml_names(text)
    model_classes = list(model_classes or [])
    if not yaml_classes:
        return {
            "ok": True,
            "errors": [],
            "warnings": ["The data.yaml has no readable names: entry."],
            "classes": [],
        }
    if len(yaml_classes) != len(model_classes):
        errors.append(
            f"data.yaml declares {len(yaml_classes)} classes but the model "
            f"exposes {len(model_classes)}."
        )
    for position, (model_class, yaml_name) in enumerate(
        zip(model_classes, yaml_classes)
    ):
        if _clean(model_class.get("name")) != _clean(yaml_name):
            errors.append(
                f"Class {position}: model has '{model_class.get('name')}' but "
                f"data.yaml has '{yaml_name}'."
            )
    if errors:
        return {"ok": False, "errors": errors, "warnings": warnings, "classes": yaml_classes}
    return {"ok": True, "errors": [], "warnings": warnings, "classes": yaml_classes}


def _parse_yaml_names(text):
    """Read the ``names:`` entry of a data.yaml (scalars only, no tag execution)."""
    from VisionInspection.services.model_registry import _parse_names_block

    raw = str(text or "")
    # data.yaml uses ``names: {0: a, 1: b}`` or a block list; normalise both.
    names = _parse_names_block(raw)
    if names:
        return [names[key] for key in sorted(names)]
    inline = _inline_names(raw)
    if inline:
        return inline
    return []


def _inline_names(text):
    names = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("names:"):
            continue
        value = stripped.split(":", 1)[1].strip()
        if not value:
            continue
        body = value.strip("{}[]")
        for chunk in body.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" in chunk:
                chunk = chunk.split(":", 1)[1]
            chunk = chunk.strip().strip("'\"")
            if chunk:
                names.append(chunk)
        break
    return names


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #


def _as_part(part):
    if isinstance(part, VisionInspectionPart):
        return part
    part = VisionInspectionPart.objects.filter(part_number__iexact=_clean(part)).first()
    if part is None:
        raise ConfigurationError("The part number is not registered.")
    return part


def _as_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _clamp01(value, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def _source_or_default(value):
    source = _clean(value).upper()
    if source in (
        VisionPartConfiguration.SOURCE_OPENCV,
        VisionPartConfiguration.SOURCE_RPI,
    ):
        return source
    return VisionPartConfiguration.SOURCE_OPENCV


def _side_or_default(value):
    side = _clean(value).upper()
    if side in (
        VisionPartConfiguration.SIDE_FRONT,
        VisionPartConfiguration.SIDE_BACK,
        VisionPartConfiguration.SIDE_BOTH,
    ):
        return side
    return VisionPartConfiguration.SIDE_FRONT


def _rule_or_default(value):
    rule = _clean(value).lower()
    valid = {choice[0] for choice in VisionClassConfiguration.RULE_CHOICES}
    valid |= {choice[0] for choice in VisionPartConfiguration._meta.get_field("acceptance_rule").choices}
    return rule if rule in valid else "count_confidence"
