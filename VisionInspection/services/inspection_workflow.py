"""Airbag Stitch Inspection — sequential front→back workflow service.

Owns the inspection-session state machine and the real sensor/model hooks.
Verdicts are computed only from actual results and explicitly configured rules;
missing rules/mode/camera produce PENDING / INCOMPLETE / ERROR, never a
fabricated OK or NOT OK.
"""

import base64
import io
import logging
import re
import threading
import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from VisionInspection.models import (
    VisionInspectionCameraConfig,
    VisionInspectionLog,
    VisionInspectionMasterROI,
    VisionInspectionPart,
    VisionInspectionSession,
    VisionYOLOModel,
    VisionPartConfiguration,
    VisionPartModel,
    VisionPartROI,
)
from VisionInspection.services.camera_service import (
    CAMERA_STATES,
    CameraService,
    get_camera_capabilities,
)
from VisionInspection.services.yolo_service import YOLOModelService
from VisionInspection.services.model_registry import (
    ModelLoadError,
    ModelRegistry,
    ModelRuntimeUnavailableError,
)
from VisionInspection.services import storage_service
from VisionInspection.services.part_configuration import (
    store_validation,
    validate_configuration,
)

logger = logging.getLogger(__name__)

_S = VisionInspectionSession  # alias

# Stitch rules are optional engineering configuration.  If absent, per-stitch
# status is reported as unrulled and the session cannot be classified OK.
_DEFAULT_RULES = getattr(settings, "VISION_STITCH_RULES", {}) or {}


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def station_id() -> str:
    """Return the configured station label, or an empty value."""
    return _clean(getattr(settings, "VISION_INSPECTION_STATION_ID", ""))


def _clean(value) -> str:
    return str(value or "").strip()


def _encode_jpeg(image, quality=85) -> bytes | None:
    import cv2

    ok, buf = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else None


def _annotate(image, detections):
    """Return a copy of *image* with real segmentation masks and boxes drawn."""
    import cv2
    import numpy as np

    out = image.copy()
    mask_layer = out.copy()
    mask_count = 0
    for detection in detections:
        polygon = detection.get("polygon")
        if not polygon or len(polygon) < 3:
            continue
        points = np.asarray(polygon, dtype=np.int32).reshape((-1, 1, 2))
        cv2.fillPoly(mask_layer, [points], (0, 190, 255))
        mask_count += 1
    if mask_count:
        out = cv2.addWeighted(mask_layer, 0.32, out, 0.68, 0)

    for d in detections:
        x1, y1, x2, y2 = d.get("x1", 0), d.get("y1", 0), d.get("x2", 0), d.get("y2", 0)
        label = f"{d.get('class', '?')} {d.get('confidence', 0):.2f}"
        polygon = d.get("polygon")
        if polygon and len(polygon) >= 3:
            points = np.asarray(polygon, dtype=np.int32).reshape((-1, 1, 2))
            cv2.polylines(out, [points], True, (0, 255, 255), 2, cv2.LINE_AA)
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 255, 0), 2)
        cv2.putText(
            out, label, (x1, max(0, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1, cv2.LINE_AA,
        )
    return out


def _map_detections_to_frame(detections, offset_x, offset_y, width, height):
    """Translate crop-local boxes and polygons into source-frame coordinates."""
    mapped = []
    for detection in detections:
        item = dict(detection)
        for key, offset, limit in (
            ("x1", offset_x, width - 1),
            ("x2", offset_x, width - 1),
            ("y1", offset_y, height - 1),
            ("y2", offset_y, height - 1),
        ):
            if key in item:
                item[key] = max(0, min(limit, int(item[key]) + offset))
        if item.get("polygon"):
            item["polygon"] = [
                [
                    max(0, min(width - 1, float(point[0]) + offset_x)),
                    max(0, min(height - 1, float(point[1]) + offset_y)),
                ]
                for point in item["polygon"]
            ]
        mapped.append(item)
    return mapped


class InspectionWorkflow:
    """Singleton service orchestrating the inspection workflow."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_ready", False):
            return
        self._ready = True
        self._camera = CameraService()
        self._yolo = YOLOModelService()
        self._model_registry = ModelRegistry()
        self._submit_lock = threading.Lock()
        self._resource_lock = threading.RLock()
        self._camera_session_id = None

    # ------------------------------------------------------------------ #
    #  Step 1 — Part barcode
    # ------------------------------------------------------------------ #
    def scan_part(self, barcode: str):
        barcode = str(barcode or "").strip()
        if not barcode:
            return {"ok": False, "state": "waiting", "message": "Scan a part barcode."}
        nb = _norm(barcode)
        part = (
            VisionInspectionPart.objects.filter(part_number__iexact=barcode).first()
            or VisionInspectionPart.objects.filter(part_number_normalized=nb).first()
        )
        if part is None:
            return {"ok": False, "state": "part_not_found", "message": "Part not found for this barcode."}
        latest_config = VisionPartConfiguration.objects.filter(part=part).order_by("-version").first()
        active_config = VisionPartConfiguration.objects.filter(
            part=part, is_active=True, status=VisionPartConfiguration.STATUS_READY
        ).order_by("-version").first()
        has_model = bool(active_config and active_config.part_models.filter(is_enabled=True).exists())
        if latest_config is None:
            has_model = self._active_model() is not None
        active_session = (
            VisionInspectionSession.objects.filter(
                is_active=True,
                is_saved=False,
            )
            .exclude(stage__in=["saved", "cancelled"])
            .exclude(front_status=_S.ERROR)
            .exclude(back_status=_S.ERROR)
            .order_by("-started_at")
            .first()
        )
        return {
            "ok": True,
            "state": "part_identified",
            "part": {
                "id": part.pk,
                "part_number": part.part_number,
                "part_name": part.part_name,
                "is_active": part.is_active,
            },
            "has_model": has_model,
            "configuration_status": (
                active_config.status if active_config
                else latest_config.status if latest_config
                else "NOT_CONFIGURED"
            ),
            "inspection_mode": active_config.inspection_mode if active_config else "",
            "front_roi": self._roi_pixels(part.part_number, "front"),
            "back_roi": self._roi_pixels(part.part_number, "back"),
            "active_session": ({
                "session_id": str(active_session.session_id),
                "part_number": active_session.part_number,
                "stage": active_session.stage,
            } if active_session else None),
            "message": f"Part identified: {part.part_number}.",
        }

    def _active_model(self):
        return (
            VisionYOLOModel.objects.filter(is_active=True)
            .order_by("-updated_at")
            .first()
        )

    def _roi_pixels(self, part_number, view):
        conf = self.get_camera_config(view)
        if conf:
            return {
                "x": round(conf.roi_x * conf.frame_width),
                "y": round(conf.roi_y * conf.frame_height),
                "width": round(conf.roi_w * conf.frame_width),
                "height": round(conf.roi_h * conf.frame_height),
                "ref_width": conf.frame_width,
                "ref_height": conf.frame_height,
                "source": "config",
            }
        roi = VisionInspectionMasterROI.objects.filter(
            part_number=part_number, view=view, is_active=True
        ).first()
        if not roi:
            return None
        return {
            "x": round(roi.x * roi.ref_width),
            "y": round(roi.y * roi.ref_height),
            "width": round(roi.width * roi.ref_width),
            "height": round(roi.height * roi.ref_height),
            "ref_width": roi.ref_width,
            "ref_height": roi.ref_height,
            "source": "part",
        }

    # ------------------------------------------------------------------ #
    #  One-time camera ROI configuration (reused across parts)
    # ------------------------------------------------------------------ #
    def get_camera_config(self, view):
        """Return the active camera ROI configuration for *view* (or None)."""
        return (
            VisionInspectionCameraConfig.objects.filter(view=view, is_active=True)
            .order_by("-version")
            .first()
        )

    def save_camera_config(self, view, x, y, width, height, ref_w, ref_h,
                           source="opencv", index=0, updated_by=""):
        """Persist a one-time camera ROI config (versioned; one active per view)."""
        ref_w = float(ref_w or 0) or 1.0
        ref_h = float(ref_h or 0) or 1.0
        with transaction.atomic():
            prev = self.get_camera_config(view)
            version = (prev.version + 1) if prev else 1
            VisionInspectionCameraConfig.objects.filter(view=view, is_active=True).update(
                is_active=False
            )
            return VisionInspectionCameraConfig.objects.create(
                view=view,
                camera_source=source or "opencv",
                camera_index=int(index or 0),
                frame_width=int(ref_w),
                frame_height=int(ref_h),
                roi_x=max(0.0, min(1.0, float(x) / ref_w)),
                roi_y=max(0.0, min(1.0, float(y) / ref_h)),
                roi_w=max(0.0, min(1.0, float(width) / ref_w)),
                roi_h=max(0.0, min(1.0, float(height) / ref_h)),
                version=version,
                is_active=True,
                updated_by=updated_by or "",
            )

    def _resolve_side_roi(self, part_number, view):
        """Resolve a session ROI: prefer the active camera config, else per-part."""
        conf = self.get_camera_config(view)
        if conf:
            return {
                "x": conf.roi_x, "y": conf.roi_y, "w": conf.roi_w, "h": conf.roi_h,
                "ref_width": conf.frame_width, "ref_height": conf.frame_height,
                "source": "config", "version": conf.version,
            }, "config"
        roi = VisionInspectionMasterROI.objects.filter(
            part_number=part_number, view=view, is_active=True
        ).first()
        if roi:
            return {
                "x": roi.x, "y": roi.y, "w": roi.width, "h": roi.height,
                "ref_width": roi.ref_width, "ref_height": roi.ref_height,
                "source": "part", "version": 0,
            }, "part"
        return None, None

    # ------------------------------------------------------------------ #
    #  Step 2 — Model validation / load (once, reused)
    # ------------------------------------------------------------------ #
    def validate_model(self, model_id: int | None = None, part_number: str | None = None):
        if part_number:
            part = VisionInspectionPart.objects.filter(part_number__iexact=part_number).first()
            if part is None:
                return {"ok": False, "state": "part_not_found", "message": "Part not found."}
            configuration = VisionPartConfiguration.objects.filter(
                part=part, is_active=True
            ).order_by("-version").first()
            if configuration:
                return self._validate_part_configuration(configuration)
            latest_configuration = VisionPartConfiguration.objects.filter(
                part=part
            ).order_by("-version").first()
            if latest_configuration:
                validation_errors = list(
                    latest_configuration.validation_errors or []
                )
                return {
                    "ok": False,
                    "state": "configuration_invalid",
                    "configuration_id": latest_configuration.pk,
                    "configuration_version": latest_configuration.version,
                    "validation_errors": validation_errors,
                    "message": " ".join(validation_errors)
                    or "This part has no active, validated vision configuration.",
                }

        model = (
            VisionYOLOModel.objects.filter(pk=model_id).first()
            if model_id
            else self._active_model()
        )
        if model is None:
            return {
                "ok": False,
                "state": "model_missing",
                "message": "No approved inspection model configured for this part.",
            }
        if not self._yolo.is_loaded() or self._yolo.get_model_path() != model.model_path:
            loaded = self._yolo.load_model(model.model_path)
            if not loaded:
                return {
                    "ok": False,
                    "state": "model_invalid",
                    "message": f"Model could not be loaded: {model.model_path}",
                }
        classes = self._yolo.get_class_names() or []
        required = self._required_classes(model)
        missing = [c for c in required if c not in classes]
        if required and missing:
            return {
                "ok": False,
                "state": "model_classes_missing",
                "message": "Model is missing required classes: " + ", ".join(missing),
                "classes": classes,
            }
        return {
            "ok": True,
            "state": "model_loaded",
            "model_id": model.id,
            "model_version": model.version or "",
            "classes": classes,
            "num_classes": len(classes),
            "message": f"Model loaded: {model.name} ({len(classes)} classes).",
        }

    def _required_classes(self, model):
        rule_classes = set(_DEFAULT_RULES.keys())
        if model.part_class:
            rule_classes.add(model.part_class)
        return sorted(rule_classes)

    def _configuration_roi(self, configuration):
        roi = configuration.rois.filter(is_active=True).order_by("-version").first()
        if roi is None:
            return None
        normalized = roi.normalized
        return {
            "x": normalized["x"],
            "y": normalized["y"],
            "w": normalized["width"],
            "h": normalized["height"],
            "ref_width": roi.ref_width,
            "ref_height": roi.ref_height,
            "source": "part_configuration",
            "version": roi.version,
        }

    def _validate_part_configuration(self, configuration):
        def configuration_error(state, message):
            validation_errors = [message]
            store_validation(configuration, validation_errors)
            return {
                "ok": False,
                "state": state,
                "configuration_id": configuration.pk,
                "configuration_version": configuration.version,
                "message": message,
                "validation_errors": validation_errors,
            }

        associations = list(
            configuration.part_models.filter(is_enabled=True)
            .select_related("model")
            .prefetch_related("class_configs")
        )
        if not associations:
            return configuration_error(
                "configuration_invalid",
                "No enabled models are configured for this part.",
            )

        configured_models = []
        class_count = 0
        discovered_classes = {}
        for association in associations:
            try:
                model_info = self._model_registry.inspect(
                    association.model.model_path,
                    task=association.task_type,
                )
            except ModelLoadError as exc:
                logger.warning(
                    "Configured model %s failed inspection: %s",
                    association.model_id,
                    exc,
                )
                return configuration_error(
                    (
                        "model_runtime_unavailable"
                        if isinstance(exc, ModelRuntimeUnavailableError)
                        else "model_invalid"
                    ),
                    f"Configured model {association.model.name} could not be loaded: {exc}",
                )
            if not model_info["ok"]:
                return configuration_error(
                    "model_invalid",
                    f"Configured model {association.model.name} could not be loaded: {model_info['error']}",
                )
            actual_task = model_info.get("task", "")
            task_matches = actual_task == association.task_type
            if not task_matches:
                return configuration_error(
                    "model_task_mismatch",
                    (
                        f"Configured model {association.model.name} is marked as "
                        f"{association.task_type}, but the model file is "
                        f"{actual_task or 'not an inference-ready model'}. "
                        "Update the model task or select a compatible model."
                    ),
                )
            actual_names = [item["name"] for item in model_info["classes"]]
            discovered_classes[association.pk] = actual_names
            saved_classes = association.classes or []
            if len(actual_names) != len(saved_classes) or any(
                item.get("id") != index or item.get("name") != actual_names[index]
                for index, item in enumerate(saved_classes)
            ):
                return configuration_error(
                    "model_classes_missing",
                    f"Configured classes for {association.model.name} no longer match the model file.",
                )
            rules = list(association.class_configs.filter(is_enabled=True).order_by("priority", "class_id"))
            if not rules:
                return configuration_error(
                    "configuration_invalid",
                    f"Model {association.model.name} has no enabled class rules.",
                )
            class_count += len(rules)
            configured_models.append({
                "model_id": association.model_id,
                "name": association.display_name or association.model.name,
                "version": association.version or association.model.version or "1.0",
                "path": association.model.model_path,
                "task_type": association.task_type,
                "classes": [{"id": index, "name": name} for index, name in enumerate(actual_names)],
                "rules": [
                    {
                        "class_id": rule.class_id,
                        "class_name": rule.class_name,
                        "side": rule.side,
                        "expected_front": rule.expected_front,
                        "expected_back": rule.expected_back,
                        "front_threshold": rule.front_threshold,
                        "back_threshold": rule.back_threshold,
                        "priority": rule.priority,
                        "required": rule.is_required,
                        "acceptance_rule": rule.acceptance_rule or configuration.acceptance_rule,
                    }
                    for rule in rules
                ],
            })
        configuration_errors = validate_configuration(
            configuration, discovered_classes
        )
        store_validation(configuration, configuration_errors)
        if configuration_errors:
            rule_diagnostics = [
                {
                    "model_id": association.model_id,
                    "enabled": association.is_enabled,
                    "class_count": association.class_configs.filter(
                        is_enabled=True
                    ).count(),
                    "rules": list(
                        association.class_configs.filter(is_enabled=True)
                        .order_by("priority", "class_id")
                        .values_list(
                            "class_name", "is_required", "side"
                        )
                    ),
                }
                for association in associations
            ]
            logger.warning(
                "Part vision configuration validation failed: part_id=%s "
                "part_number=%s configuration_id=%s version=%s mode=%s "
                "models=%s errors=%s",
                configuration.part_id,
                configuration.part.part_number,
                configuration.pk,
                configuration.version,
                configuration.inspection_mode,
                rule_diagnostics,
                configuration_errors,
            )
            return {
                "ok": False,
                "state": "configuration_invalid",
                "configuration_id": configuration.pk,
                "configuration_version": configuration.version,
                "message": " ".join(configuration_errors),
                "validation_errors": configuration_errors,
            }
        return {
            "ok": True,
            "state": "model_loaded",
            "configuration_id": configuration.pk,
            "configuration_version": configuration.version,
            "inspection_mode": configuration.inspection_mode,
            "configuration": configuration,
            "models": configured_models,
            "model_id": configured_models[0]["model_id"],
            "model_version": ", ".join(model["version"] for model in configured_models),
            "classes": [
                {"model_id": model["model_id"], **item}
                for model in configured_models for item in model["classes"]
            ],
            "num_classes": class_count,
            "message": f"{len(configured_models)} model(s) validated for {configuration.part.part_number}.",
        }

    # ------------------------------------------------------------------ #
    #  Step 3 — Submit (unique session, front starts; single active session)
    # ------------------------------------------------------------------ #
    def submit(self, part_number: str, model_id: int | None = None):
        with self._submit_lock:
            return self._submit_locked(part_number, model_id)

    @transaction.atomic
    def _submit_locked(self, part_number: str, model_id: int | None = None):
        part_number = str(part_number or "").strip()
        if not part_number:
            return {"ok": False, "state": "error", "message": "Part number is required."}

        normalized = _norm(part_number)
        part = (
            VisionInspectionPart.objects.filter(part_number__iexact=part_number).first()
            or VisionInspectionPart.objects.filter(part_number_normalized=normalized).first()
        )
        if part is not None:
            part = VisionInspectionPart.objects.select_for_update().get(pk=part.pk)
            part_number = part.part_number

        try:
            storage_service.ensure_evidence_capacity()
        except storage_service.StorageOperationError as exc:
            return {"ok": False, "state": "storage_unavailable", "message": str(exc)}

        existing = (
            VisionInspectionSession.objects.filter(
                part_number=part_number, is_active=True, is_saved=False
            )
            .exclude(stage__in=["saved", "cancelled"])
            .first()
        )
        if existing:
            return {
                "ok": False,
                "state": "duplicate_session",
                "session_id": str(existing.session_id),
                "message": "An active inspection session already exists for this part.",
            }

        validation = self.validate_model(model_id, part_number)
        if not validation["ok"]:
            return {"ok": False, "state": validation["state"], "message": validation["message"]}

        configuration = validation.get("configuration")
        if configuration:
            inspection_mode = validation["inspection_mode"]
            requires_front = configuration.requires_front
            requires_back = configuration.requires_back
            common_roi = self._configuration_roi(configuration)
            camera_settings = {
                "front": {"source": configuration.front_camera_source, "index": configuration.front_camera_index},
                "back": {"source": configuration.back_camera_source, "index": configuration.back_camera_index},
            }
            if common_roi and requires_front and requires_back and camera_settings["front"] != camera_settings["back"]:
                return {
                    "ok": False,
                    "state": "roi_incompatible",
                    "message": "The common ROI cannot be mapped between different camera roles without a validated calibration.",
                }
            front_norm = common_roi if requires_front and common_roi else None
            back_norm = common_roi if requires_back and common_roi else None
            if requires_front and front_norm is None:
                front_norm, _front_source = self._resolve_side_roi(part_number, "front")
            if requires_back and back_norm is None:
                back_norm, _back_source = self._resolve_side_roi(part_number, "back")
            required_sides = [side for side, required in (("front", requires_front), ("back", requires_back)) if required]
            if not required_sides:
                return {"ok": False, "state": "configuration_invalid", "message": "The configuration requires no inspection sides."}
            if any((front_norm if side == "front" else back_norm) is None for side in required_sides):
                return {
                    "ok": False,
                    "state": "roi_missing",
                    "message": "A required side has no compatible ROI. Configure the common part ROI or side ROI before inspection.",
                }
            selected_models = validation["models"]
            model = VisionYOLOModel.objects.get(pk=selected_models[0]["model_id"])
            first_side = required_sides[0]
            model_classes = validation["classes"]
            snapshot = {
                "configuration_id": configuration.pk,
                "configuration_version": configuration.version,
                "inspection_mode": inspection_mode,
                "acceptance_rule": configuration.acceptance_rule,
                "camera": camera_settings,
                "models": selected_models,
                "common_roi": common_roi,
            }
            initial_front_status = _S.IN_PROGRESS if first_side == "front" else _S.SKIPPED
            initial_back_status = _S.IN_PROGRESS if first_side == "back" else (
                _S.PENDING if requires_back else _S.SKIPPED
            )
            initial_front_verdict = "PENDING" if requires_front else "NOT_REQUIRED"
            initial_back_verdict = "PENDING" if requires_back else "NOT_REQUIRED"
        else:
            inspection_mode = VisionPartConfiguration.MODE_FRONT_BACK
            requires_front = requires_back = True
            camera_settings = {
                "front": {"source": VisionPartConfiguration.SOURCE_OPENCV, "index": 0},
                "back": {"source": VisionPartConfiguration.SOURCE_OPENCV, "index": 0},
            }
            snapshot = {}
            selected_models = []
            common_roi = None
            model = VisionYOLOModel.objects.get(pk=validation["model_id"])
            front_norm, _front_source = self._resolve_side_roi(part_number, "front")
            back_norm, _back_source = self._resolve_side_roi(part_number, "back")
            if front_norm is None:
                return {
                    "ok": False,
                    "state": "roi_missing",
                    "message": "No ROI is configured. Set up the camera ROI once (Inspection Master / Camera Setup) before inspecting.",
                }
            model_classes = validation["classes"]
            first_side = "front"
            initial_front_status = _S.IN_PROGRESS
            initial_back_status = _S.PENDING
            initial_front_verdict = initial_back_verdict = "PENDING"

        camera_setting = camera_settings[first_side]
        source = "picamera" if camera_setting["source"] == VisionPartConfiguration.SOURCE_RPI else "opencv"
        capabilities = get_camera_capabilities()
        if source == "picamera" and not capabilities["sources"][1]["available"]:
            return {
                "ok": False,
                "state": "camera_unavailable",
                "message": "The configured Raspberry Pi camera is unavailable on this station.",
            }

        active_camera = CameraService()
        if (
            self._camera.get_status() != CAMERA_STATES["ON"]
            or CameraService.get_source() != source
            or active_camera._camera_index != camera_setting["index"]
        ):
            CameraService.stop_camera()
            if not CameraService.start_camera(camera_index=camera_setting["index"], source=source):
                return {"ok": False, "state": "camera_error", "message": "The configured camera could not be started."}

        try:
            with transaction.atomic():
                session = VisionInspectionSession.objects.create(
                    part_number=part_number,
                    model=model,
                    model_version=validation["model_version"],
                    model_classes=model_classes,
                    configuration=configuration,
                    config_version=configuration.version if configuration else 0,
                    config_snapshot=snapshot,
                    roi_version=common_roi["version"] if common_roi else 0,
                    stage=first_side,
                    front_status=initial_front_status,
                    back_status=initial_back_status,
                    front_verdict=initial_front_verdict,
                    back_verdict=initial_back_verdict,
                    overall="PENDING",
                    front_roi=None,
                    back_roi=None,
                    front_roi_norm=(front_norm or {}),
                    back_roi_norm=(back_norm or {}),
                    is_active=True,
                )
        except IntegrityError:
            existing = VisionInspectionSession.objects.filter(
                part_number=part_number, is_active=True, is_saved=False
            ).first()
            if existing is None:
                raise
            return {
                "ok": False,
                "state": "duplicate_session",
                "session_id": str(existing.session_id),
                "message": "An active inspection session already exists for this part.",
            }
        self._camera_session_id = str(session.session_id)
        logger.info("Inspection session %s created for %s", session.session_id, part_number)
        return {
            "ok": True,
            "session_id": str(session.session_id),
            "state": f"{first_side}_in_progress",
            "next_side": first_side,
            "message": f"{first_side.title()} inspection started.",
            "progress": self.get_progress(str(session.session_id)),
        }

    def _get_session(self, session_id):
        try:
            return VisionInspectionSession.objects.get(session_id=session_id)
        except Exception:
            return None

    def activate_side(self, session_id: str, side: str):
        with self._resource_lock:
            return self._activate_side_locked(session_id, side)

    def _activate_side_locked(self, session_id: str, side: str):
        """Activate the camera frozen in a session without running inference."""
        if side not in ("front", "back"):
            return {"ok": False, "state": "error", "message": "Inspection side is invalid."}
        session = self._get_session(session_id)
        if session is None:
            return {"ok": False, "state": "error", "message": "Session not found."}
        if not session.is_active or session.is_saved or session.stage == "cancelled":
            return {"ok": False, "state": "session_inactive", "message": "This inspection session is no longer active."}

        if side == "back" and session.front_status not in (_S.COMPLETED, _S.SKIPPED):
            return {"ok": False, "state": "incomplete", "message": "Complete the front inspection before activating the back camera."}

        if session.configuration_id:
            mode = session.config_snapshot.get("inspection_mode")
            if (side == "front" and mode == VisionPartConfiguration.MODE_BACK_ONLY) or (
                side == "back" and mode == VisionPartConfiguration.MODE_FRONT_ONLY
            ):
                return {"ok": False, "state": "side_not_required", "message": f"{side.title()} inspection is not required."}
            camera = session.config_snapshot.get("camera", {}).get(side, {})
            source = "picamera" if camera.get("source") == VisionPartConfiguration.SOURCE_RPI else "opencv"
            index = int(camera.get("index", 0))
            capabilities = get_camera_capabilities()
            if source == "picamera" and not capabilities["sources"][1]["available"]:
                return {"ok": False, "state": "camera_unavailable", "message": "The configured Raspberry Pi camera is unavailable on this station."}
            active_camera = CameraService()
            camera_matches = (
                CameraService.get_source() == source
                and active_camera._camera_index == index
            )
            camera_status = self._camera.get_status()
            if not camera_matches or camera_status not in (
                CAMERA_STATES["ON"], CAMERA_STATES["CONNECTING"]
            ):
                CameraService.stop_camera()
                if not CameraService.start_camera(camera_index=index, source=source):
                    return {"ok": False, "state": "camera_error", "message": f"The configured {side} camera could not be started."}

        camera_status = self._camera.get_status()
        camera_ready = camera_status in (
            CAMERA_STATES["ON"], CAMERA_STATES["CONNECTING"]
        )
        if not camera_ready:
            return {
                "ok": False,
                "state": "camera_error",
                "message": CameraService.get_error() or "Camera is not connected.",
            }
        self._camera_session_id = str(session.session_id)
        return {
            "ok": True,
            "state": f"{side}_camera_ready" if camera_status == CAMERA_STATES["ON"] else "camera_connecting",
            "message": f"{side.title()} camera ready." if camera_status == CAMERA_STATES["ON"] else f"{side.title()} camera is connecting.",
            "progress": self.get_progress(str(session.session_id)),
        }

    def _configured_side_results(self, session, side, crop):
        detections = []
        results = []
        for model_config in session.config_snapshot.get("models", []):
            rules = [
                rule for rule in model_config["rules"]
                if rule["side"] in (side.upper(), "BOTH")
            ]
            if not rules:
                continue
            required_task = model_config.get("task_type", "segment")
            try:
                model_info = self._model_registry.inspect(
                    model_config["path"],
                    task=required_task,
                )
            except ModelLoadError as exc:
                logger.exception(
                    "Model inspection failed during %s inference for session %s",
                    side,
                    session.session_id,
                )
                return None, None, f"Configured model {model_config['name']} could not be loaded: {exc}"
            if not model_info["ok"]:
                return None, None, (
                    f"Configured model {model_config['name']} could not be loaded: "
                    f"{model_info['error']}"
                )
            actual_task = model_info.get("task", "")
            task_matches = actual_task == required_task
            if not task_matches:
                return None, None, (
                    f"Configured model {model_config['name']} is marked as "
                    f"{required_task}, but the model file is "
                    f"{actual_task or 'not inference-ready'}."
                )
            actual_classes = [item["name"] for item in model_info["classes"]]
            if actual_classes != [item["name"] for item in model_config["classes"]]:
                return None, None, f"Configured classes for {model_config['name']} no longer match the model file."
            threshold = min(
                float(rule["front_threshold"] if side == "front" else rule["back_threshold"])
                for rule in rules
            )
            class_ids = {int(rule["class_id"]) for rule in rules}
            try:
                model_detections, inference_time = self._model_registry.predict(
                    model_config["path"],
                    crop,
                    conf=threshold,
                    classes=class_ids,
                    task=required_task,
                )
            except ModelLoadError as exc:
                logger.exception(
                    "Model loading failed during %s inference for session %s",
                    side,
                    session.session_id,
                )
                return None, None, f"Configured model {model_config['name']} could not be loaded: {exc}"
            if isinstance(inference_time, str):
                return None, None, (
                    f"Inference failed for model {model_config['name']}: "
                    f"{inference_time}"
                )
            if required_task == "segment" and any(
                "polygon" not in detection for detection in model_detections
            ):
                return None, None, (
                    f"Segmentation model {model_config['name']} returned detections "
                    "without masks. The model output cannot be treated as a valid "
                    "segmentation result."
                )
            detections.extend({**detection, "model_id": model_config["model_id"]} for detection in model_detections)
            for rule in rules:
                threshold = float(rule[f"{side}_threshold"])
                matching = [
                    detection for detection in model_detections
                    if detection.get("class_id") == rule["class_id"]
                    and float(detection.get("confidence", 0.0)) >= threshold
                ]
                count = len(matching)
                confidence = max(
                    (float(detection.get("confidence", 0.0)) for detection in matching),
                    default=None,
                )
                expected = int(rule[f"expected_{side}"])
                acceptance = rule["acceptance_rule"]
                if acceptance == "presence":
                    passed = count > 0
                elif acceptance == "confidence":
                    passed = confidence is not None
                elif acceptance == "count_confidence":
                    passed = count == expected and confidence is not None
                else:
                    passed = count == expected
                results.append({
                    "priority": rule["priority"],
                    "model_id": model_config["model_id"],
                    "model": model_config["name"],
                    "class_id": rule["class_id"],
                    "stitch": rule["class_name"],
                    "side": side.upper(),
                    "expected_count": expected,
                    "actual_count": count,
                    "confidence": round(confidence, 4) if confidence is not None else None,
                    "threshold": threshold,
                    "required": rule["required"],
                    "status": "OK" if passed else ("NOT_OK" if rule["required"] else "OPTIONAL_NOT_OK"),
                })
        results.sort(key=lambda row: (row["priority"], row["model_id"], row["class_id"]))
        return detections, results, None

    # ------------------------------------------------------------------ #
    #  Step 4/5 — Run one side (real camera + YOLO + ROI crop)
    # ------------------------------------------------------------------ #
    def run_side(self, session_id: str, side: str, frame=None):
        if side not in ("front", "back"):
            return {"ok": False, "state": "invalid_side", "message": "Inspection side is invalid."}
        session = self._get_session(session_id)
        if session is None:
            return {"ok": False, "state": "error", "message": "Session not found."}
        if not session.is_active or session.is_saved or session.stage == "cancelled":
            return {"ok": False, "state": "session_inactive", "message": "This inspection session is no longer active."}
        side_status = getattr(session, f"{side}_status")
        if side_status == _S.COMPLETED:
            return {
                "ok": True,
                "state": f"{side}_completed",
                "already_completed": True,
                "message": f"{side.title()} inspection has already completed; stored results were returned.",
                "detections": [],
                "results": getattr(session, f"{side}_results"),
                "progress": self.get_progress(str(session.session_id)),
            }
        if side_status == _S.SKIPPED:
            return {
                "ok": False,
                "state": "side_not_required",
                "message": f"{side.title()} inspection is not required for this part.",
            }

        if session.configuration_id:
            configured_mode = session.config_snapshot.get("inspection_mode")
            if (side == "front" and configured_mode == VisionPartConfiguration.MODE_BACK_ONLY) or (
                side == "back" and configured_mode == VisionPartConfiguration.MODE_FRONT_ONLY
            ):
                return {"ok": False, "state": "side_not_required", "message": f"{side.title()} inspection is not required for this part."}
            camera = session.config_snapshot.get("camera", {}).get(side, {})
            source = "picamera" if camera.get("source") == VisionPartConfiguration.SOURCE_RPI else "opencv"
            index = int(camera.get("index", 0))
            active_camera = CameraService()
            if (
                self._camera.get_status() != CAMERA_STATES["ON"]
                or CameraService.get_source() != source
                or active_camera._camera_index != index
            ):
                CameraService.stop_camera()
                if not CameraService.start_camera(camera_index=index, source=source):
                    return self._fail_side(session, side, "The configured camera could not be started.")

        # ROI frozen at Submit (config-first), with per-part FK fallback.
        roi = (session.front_roi_norm if side == "front" else session.back_roi_norm) or None
        if not roi:
            fk = session.front_roi if side == "front" else session.back_roi
            if fk is not None:
                roi = {
                    "x": fk.x, "y": fk.y, "w": fk.width, "h": fk.height,
                    "ref_width": fk.ref_width, "ref_height": fk.ref_height,
                }
        if not roi or not roi.get("w"):
            return self._fail_side(session, side, "No ROI configured for this side.")

        if frame is None:
            if self._camera.get_status() != CAMERA_STATES["ON"]:
                return self._fail_side(session, side, "Camera is not running.")
            ok, frame = self._camera.get_frame()
            if not ok or frame is None:
                return self._fail_side(session, side, "No camera frame available.")
        if (
            getattr(frame, "ndim", 0) != 3
            or frame.shape[0] <= 0
            or frame.shape[1] <= 0
            or frame.shape[2] not in (3, 4)
        ):
            return self._fail_side(session, side, "The camera returned an invalid image frame.")
        if not session.configuration_id and not self._yolo.is_loaded():
            return self._fail_side(session, side, "Inspection model is not loaded.")

        h, w = frame.shape[:2]
        px, py = int(roi["x"] * w), int(roi["y"] * h)
        pw, ph = int(roi["w"] * w), int(roi["h"] * h)
        if px + pw > w or py + ph > h or pw <= 1 or ph <= 1:
            return self._fail_side(session, side, "ROI outside frame or invalid.")
        crop = frame[py:py + ph, px:px + pw]  # crop before inference

        t0 = timezone.now()
        if session.configuration_id:
            detections, results, inference_error = self._configured_side_results(session, side, crop)
            if inference_error:
                return self._fail_side(session, side, inference_error)
            verdict = self._side_verdict(results)
        else:
            detections = self._yolo.detect(crop, conf=float(getattr(settings, "VISION_INFERENCE_CONF", 0.5)))
            results = self._per_stitch(detections)
            verdict = self._side_verdict(results)
        frame_detections = _map_detections_to_frame(
            detections, px, py, w, h
        )
        annotated_frame = _annotate(frame, frame_detections)
        annotated_jpeg = _encode_jpeg(annotated_frame)
        logger.info(
            "Inspection inference completed: session=%s side=%s frame=%sx%s "
            "roi=%sx%s detections=%s masks=%s verdict=%s",
            session.session_id,
            side,
            w,
            h,
            pw,
            ph,
            len(frame_detections),
            sum("polygon" in detection for detection in frame_detections),
            verdict,
        )

        with transaction.atomic():
            session = VisionInspectionSession.objects.select_for_update().get(pk=session.pk)
            if not session.is_active or session.is_saved or session.stage == "cancelled":
                return {"ok": False, "state": "session_inactive", "message": "This inspection session was reset before inference completed."}
            if getattr(session, f"{side}_status") in (_S.COMPLETED, _S.SKIPPED):
                return {"ok": False, "state": "side_already_completed", "message": f"{side.title()} inspection has already completed."}
            try:
                self._save_image(session, side, "image", _encode_jpeg(frame))
                self._save_image(session, side, "annotated", annotated_jpeg)
            except storage_service.StorageOperationError as exc:
                return self._fail_side(session, side, f"Inspection evidence could not be persisted: {exc}")

            setattr(session, f"{side}_results", results)
            setattr(session, f"{side}_verdict", verdict)
            if session.configuration_id:
                session.class_results = [
                    row for row in session.class_results if row.get("side") != side.upper()
                ] + results
            setattr(session, f"{side}_status", _S.COMPLETED)
            if side == "front":
                session.front_completed_at = timezone.now()
                session.stage = "back" if session.back_status != _S.SKIPPED else "result"
            else:
                session.back_completed_at = timezone.now()
                session.stage = "result"
            session.save()

        return {
            "ok": True,
            "state": f"{side}_completed",
            "detections": frame_detections,
            "results": results,
            "mask_count": sum("polygon" in detection for detection in frame_detections),
            "annotated_frame": (
                "data:image/jpeg;base64,"
                + base64.b64encode(annotated_jpeg).decode("ascii")
                if annotated_jpeg
                else None
            ),
            "latency_ms": round((timezone.now() - t0).total_seconds() * 1000, 1),
            "progress": self.get_progress(str(session.session_id)),
        }

    def _fail_side(self, session, side, message):
        with transaction.atomic():
            session = VisionInspectionSession.objects.select_for_update().get(pk=session.pk)
            if not session.is_active or session.is_saved or session.stage == "cancelled":
                return {"ok": False, "state": "session_inactive", "message": "This inspection session is no longer active."}
            setattr(session, f"{side}_status", _S.ERROR)
            setattr(session, f"{side}_verdict", "ERROR")
            session.overall = _S.OVERALL_INCOMPLETE
            session.error_message = message
            session.is_active = False
            session.save()
        self._stop_camera_for_session(str(session.session_id))
        return {"ok": False, "state": f"{side}_error", "message": message,
                "progress": self.get_progress(str(session.session_id))}

    def _per_stitch(self, detections):
        rules = _DEFAULT_RULES or {}
        agg = {}
        for d in detections:
            cls = d.get("class", "")
            agg[cls] = max(agg.get(cls, 0.0), float(d.get("confidence", 0.0)))
        rows = []
        for cls, rule in (rules.items() if rules else {}):
            conf = agg.get(cls)
            min_count = int(rule.get("min_count", 1)) if isinstance(rule, dict) else 1
            if conf is None:
                status, measurement = "NO_RULE", "—"
            elif conf >= float(rule.get("min_confidence", 0.0)):
                status, measurement = "—", f"conf {conf:.2f}"
            else:
                status, measurement = "—", f"conf {conf:.2f}"
            rows.append({
                "stitch": cls, "confidence": (round(conf, 3) if conf is not None else None),
                "measurement": measurement, "status": "NO_RULE", "min_count": min_count,
            })
        if not rows:
            for cls, conf in agg.items():
                rows.append({
                    "stitch": cls, "confidence": round(conf, 3),
                    "measurement": f"conf {conf:.2f}", "status": "NO_RULE", "min_count": None,
                })
        return rows

    def _side_verdict(self, results):
        required = [row for row in results if row.get("required")]
        if not required:
            return "INDETERMINATE"
        return "NOT_OK" if any(row.get("status") != "OK" for row in required) else "OK"

    def _save_image(self, session, side, kind, jpeg_bytes):
        if not jpeg_bytes:
            raise storage_service.StorageOperationError("The inspection image could not be encoded.")
        field_name = f"{side}_{'annotated' if kind == 'annotated' else 'image'}"
        name = f"{session.session_id}_{side}_{kind}.jpg"
        storage_service.register_image(session, field_name, jpeg_bytes, name)

    # ------------------------------------------------------------------ #
    #  Step 6/8 — Overall result (never OK unless both sides truly OK)
    # ------------------------------------------------------------------ #
    @transaction.atomic
    def compute_result(self, session_id: str):
        session = self._get_session(session_id)
        if session is None:
            return {"ok": False, "state": "error", "message": "Session not found."}
        if not session.is_active or session.is_saved or session.stage == "cancelled":
            return {"ok": False, "state": "session_inactive", "message": "This inspection session is no longer active."}
        session = VisionInspectionSession.objects.select_for_update().get(pk=session.pk)
        if not session.is_active or session.is_saved or session.stage == "cancelled":
            return {"ok": False, "state": "session_inactive", "message": "This inspection session is no longer active."}
        fv, bv = session.front_verdict, session.back_verdict
        required_statuses = [
            (session.front_status, fv),
            (session.back_status, bv),
        ]
        if session.configuration_id:
            required_statuses = [pair for pair, side in zip(
                required_statuses, ("front", "back")
            ) if session.config_snapshot.get("inspection_mode") != (
                VisionPartConfiguration.MODE_BACK_ONLY if side == "front"
                else VisionPartConfiguration.MODE_FRONT_ONLY
            )]
        if any(status == _S.ERROR or verdict == "ERROR" for status, verdict in required_statuses):
            overall = _S.OVERALL_INCOMPLETE
        elif required_statuses and all(status == _S.COMPLETED and verdict == "OK" for status, verdict in required_statuses):
            overall = _S.OVERALL_OK
        elif any(status == _S.COMPLETED and verdict == "NOT_OK" for status, verdict in required_statuses):
            overall = _S.OVERALL_NOT_OK
        else:
            overall = _S.OVERALL_INCOMPLETE  # incomplete/indeterminate never OK
        session.overall = overall
        if overall != _S.OVERALL_INCOMPLETE or all(status == _S.COMPLETED for status, _verdict in required_statuses):
            session.stage = "result"
        session.computed_at = timezone.now()
        session.save()
        return {"ok": True, "state": "result_ready", "overall": overall,
                "front_verdict": fv, "back_verdict": bv,
                "progress": self.get_progress(str(session.session_id))}

    # ------------------------------------------------------------------ #
    #  Step 9 — Save (idempotent, transaction, reuses history/traceability)
    # ------------------------------------------------------------------ #
    @transaction.atomic
    def save(self, session_id: str):
        session = self._get_session(session_id)
        if session is None:
            return {"ok": False, "state": "error", "message": "Session not found."}
        session = VisionInspectionSession.objects.select_for_update().get(pk=session.pk)
        if session.is_saved:
            log = VisionInspectionLog.objects.filter(
                preset_name=f"SESSION#{session.session_id}"
            ).first()
            return {"ok": True, "state": "already_saved", "session_id": str(session.session_id),
                    "inspection_id": log.pk if log else None}
        if not session.is_active or session.stage == "cancelled":
            return {"ok": False, "state": "session_inactive", "message": "This inspection session is no longer active."}
        required_statuses = [session.front_status, session.back_status]
        if session.configuration_id:
            mode = session.config_snapshot.get("inspection_mode")
            required_statuses = [
                session.front_status if mode != VisionPartConfiguration.MODE_BACK_ONLY else _S.SKIPPED,
                session.back_status if mode != VisionPartConfiguration.MODE_FRONT_ONLY else _S.SKIPPED,
            ]
        if any(status not in (_S.COMPLETED, _S.SKIPPED) for status in required_statuses):
            return {"ok": False, "state": "incomplete",
                    "message": "Every required inspection side must complete before saving."}
        if session.overall in ("PENDING", _S.OVERALL_INCOMPLETE) and session.overall == _S.OVERALL_INCOMPLETE:
            # INCOMPLETE is a valid terminal state when a rule is missing; still
            # saveable as a completed-but-incomplete record (never OK).
            pass
        if session.overall == "PENDING":
            return {"ok": False, "state": "incomplete",
                    "message": "Compute the final result before saving."}

        # Reuse the inspection log for history / reports / traceability.
        log = VisionInspectionLog.objects.create(
            part_number=session.part_number,
            employee_code=station_id(),
            preset_name=f"SESSION#{session.session_id}",
            preset_version=1,
            yolo_model_name=(session.model.name if session.model else ""),
            yolo_model_version=session.model_version,
            result=session.overall if session.overall in ("OK", "NOT_OK") else "NOT_OK",
            roi_results=[
                {"side": "front", "results": session.front_results},
                {"side": "back", "results": session.back_results},
            ],
            detected_objects=session.front_results + session.back_results,
            inspection_image=None,
            part_bbox={},
            trigger_zone_polygon=[],
            stable_frames_used=0,
        )
        # Give the history log a stable reference to the same verified evidence.
        if session.front_image:
            try:
                storage_service.link_image_reference(
                    session, "front_image", log, "inspection_image"
                )
            except storage_service.StorageOperationError:
                logger.exception("Failed to link front evidence image for session %s", session.session_id)
                raise
        session.is_saved = True
        session.saved_at = timezone.now()
        session.stage = "saved"
        session.is_active = False
        session.save()
        transaction.on_commit(
            lambda: self._stop_camera_for_session(str(session.session_id))
        )
        logger.info("Inspection session %s saved as log %s", session.session_id, log.pk)
        return {"ok": True, "state": "saved", "session_id": str(session.session_id),
                "inspection_id": log.pk, "overall": session.overall}

    # ------------------------------------------------------------------ #
    #  Progress + cancel
    # ------------------------------------------------------------------ #
    def get_progress(self, session_id: str):
        session = self._get_session(session_id)
        if session is None:
            return {"ok": False, "message": "Session not found."}
        stages = [
            ("scan", "Part Scanned", bool(session.part_number)),
            ("model", "Model Loaded", bool(session.model)),
            ("front", "Front Inspection",
                session.front_status == _S.COMPLETED, session.front_status),
            ("back", "Back Inspection",
                session.back_status == _S.COMPLETED, session.back_status),
            ("result", "Final Result", session.overall in ("OK", "NOT_OK")),
            ("saved", "Save to Database", session.is_saved),
        ]
        out = []
        for item in stages:
            key, label, done = item[0], item[1], item[2]
            status = item[3] if len(item) > 3 else "PENDING"
            if done:
                st = "Completed"
            elif status in (_S.IN_PROGRESS, "IN_PROGRESS"):
                st = "In Progress"
            elif status in (_S.ERROR, "ERROR"):
                st = "Failed"
            else:
                st = "Pending"
            out.append({"step": key, "label": label, "status": st})
        return {
            "ok": True,
            "session_id": str(session.session_id),
            "part_number": session.part_number,
            "stage": session.stage,
            "front_status": session.front_status,
            "back_status": session.back_status,
            "front_verdict": session.front_verdict,
            "back_verdict": session.back_verdict,
            "overall": session.overall,
            "is_saved": session.is_saved,
            "is_active": session.is_active,
            "front_results": session.front_results,
            "back_results": session.back_results,
            "stages": out,
        }

    @transaction.atomic
    def cancel(self, session_id: str):
        session = (
            VisionInspectionSession.objects.select_for_update()
            .filter(session_id=session_id)
            .first()
        )
        if session is None:
            return {"ok": False, "state": "not_found", "message": "Inspection session not found."}
        if session.is_saved or session.stage == "saved":
            transaction.on_commit(
                lambda: self._stop_camera_for_session(str(session.session_id))
            )
            return {"ok": True, "state": "already_saved", "session_state": session.stage}
        if all(status in (_S.COMPLETED, _S.SKIPPED) for status in (
            session.front_status, session.back_status
        )):
            transaction.on_commit(
                lambda: self._stop_camera_for_session(str(session.session_id))
            )
            if session.stage == "cancelled":
                return {"ok": True, "state": "already_cancelled", "session_state": session.stage}
            return {
                "ok": False,
                "state": "completed",
                "session_state": session.stage,
                "message": "This inspection is complete and its results must be saved, not cancelled.",
            }
        if session.stage == "cancelled":
            transaction.on_commit(
                lambda: self._stop_camera_for_session(str(session.session_id))
            )
            return {"ok": True, "state": "already_cancelled", "session_state": session.stage}

        session.is_active = False
        session.stage = "cancelled"
        session.save(update_fields=["is_active", "stage"])
        transaction.on_commit(
            lambda: self._stop_camera_for_session(str(session.session_id))
        )
        return {"ok": True, "state": "cancelled", "session_state": session.stage}

    def _stop_camera_for_session(self, session_id):
        with self._resource_lock:
            if self._camera_session_id == session_id:
                CameraService.stop_camera()
                self._camera_session_id = None
