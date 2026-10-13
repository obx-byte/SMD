"""Inspection service for VisionInspection.

Implements the full inspection state machine and a background engine thread
that coordinates camera capture, YOLO detection, trigger-zone checking,
ROI transformation, and ROI validation.

The engine runs independently of HTTP requests — it continuously processes
camera frames, evaluates the trigger condition, and performs inspection when
a part is correctly positioned.

The current state is persisted to the ``VisionInspectionState`` model so that
UI views can poll it via AJAX without requiring WebSocket/Channels.
"""

import json
import logging
import threading
import time

import cv2
import numpy as np

from django.conf import settings

from VisionInspection.models import (
    VisionInspectionLog,
    VisionInspectionPreset,
    VisionInspectionState,
    VisionROI,
    VisionTriggerZone,
    VisionYOLOModel,
)
from VisionInspection.services.camera_service import CameraService, CAMERA_STATES
from VisionInspection.services.roi_service import (
    extract_roi_region,
    filter_detections_in_roi,
    transform_rois_to_actual,
    validate_roi,
)
from VisionInspection.services.trigger_service import (
    StabilityTracker,
    check_part_in_zone,
    get_polygon_from_trigger_zone,
)
from VisionInspection.services.yolo_service import YOLOModelService

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
#  State constants
# --------------------------------------------------------------------------- #

STATE_WAITING_EMPLOYEE = "WAITING_EMPLOYEE"
STATE_EMPLOYEE_VALID = "EMPLOYEE_VALID"
STATE_WAITING_PART = "WAITING_PART"
STATE_PART_VALID = "PART_VALID"
STATE_WAITING_DETECTION = "WAITING_FOR_PART_DETECTION"
STATE_PART_DETECTED = "PART_DETECTED"
STATE_PART_IN_ZONE = "PART_IN_TRIGGER_ZONE"
STATE_STABILITY_CHECK = "STABILITY_CHECK"
STATE_AUTO_TRIGGER = "AUTO_TRIGGER"
STATE_INSPECTING = "INSPECTING"
STATE_RESULT_OK = "RESULT_OK"
STATE_RESULT_NOT_OK = "RESULT_NOT_OK"
STATE_WAIT_PART_LEAVE = "WAIT_PART_LEAVE"
STATE_RE_ARM = "RE_ARM"
STATE_ERROR = "ERROR"

CAMERA_FPS = getattr(settings, "VISION_INSPECTION_INFERENCE_FPS", 10)
STABLE_FRAMES = getattr(settings, "VISION_INSPECTION_DEFAULT_STABLE_FRAMES", 3)
TRIGGER_COOLDOWN = getattr(settings, "VISION_INSPECTION_TRIGGER_COOLDOWN", 2.0)


# --------------------------------------------------------------------------- #
#  Inspection Engine
# --------------------------------------------------------------------------- #


class InspectionEngine:
    """Singleton background engine that drives the full inspection workflow."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self._camera = CameraService()
        self._yolo = YOLOModelService()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Runtime state
        self._state = STATE_WAITING_EMPLOYEE
        self._employee_code: str | None = None
        self._employee_name: str | None = None
        self._part_number: str | None = None
        self._preset: VisionInspectionPreset | None = None
        self._trigger_zone: VisionTriggerZone | None = None
        self._rois: list[VisionROI] = []
        self._stability = StabilityTracker(STABLE_FRAMES)
        self._trigger_active = False
        self._part_was_in_zone = False
        self._last_trigger_time = 0.0
        self._last_inspection_id = None

        # Persist initial state
        self._persist_state()

    # ------------------------------------------------------------------ #
    #  Public control API
    # ------------------------------------------------------------------ #

    @classmethod
    def start(cls):
        """Start the background inspection engine thread."""
        engine = cls()
        with cls._lock:
            if engine._thread is not None and engine._thread.is_alive():
                return True
            engine._stop_event = threading.Event()
            engine._thread = threading.Thread(
                target=engine._run,
                daemon=True,
                name="VisionInspection-Engine",
            )
            engine._thread.start()
            logger.info("Inspection engine started")
        return True

    @classmethod
    def stop(cls):
        """Stop the background engine thread."""
        engine = cls()
        with cls._lock:
            if engine._thread is not None:
                engine._stop_event.set()
                engine._thread.join(timeout=5)
                engine._thread = None
            engine._camera.stop_camera()
            logger.info("Inspection engine stopped")
        return True

    @classmethod
    def set_employee(cls, employee_code: str, employee_name: str | None = None):
        """Set the current employee for the inspection session."""
        engine = cls()
        with engine._lock:
            engine._employee_code = employee_code
            engine._employee_name = employee_name or employee_code
            if employee_code:
                engine._state = STATE_EMPLOYEE_VALID
                engine._transition(STATE_WAITING_PART, employee_valid=True)
            else:
                engine._state = STATE_WAITING_EMPLOYEE
            engine._persist_state()

    @classmethod
    def set_part(cls, part_number: str):
        """Set the current part number and load its active preset."""
        engine = cls()
        with engine._lock:
            engine._part_number = part_number
            preset = VisionInspectionPreset.objects.filter(
                part_number=part_number, is_active=True
            ).first()
            engine._preset = preset

            if preset is None:
                engine._state = STATE_WAITING_PART
                engine._persist_state(error="NO ACTIVE INSPECTION CONFIGURATION")
                return False

            # Load YOLO model
            if preset.yolo_model and preset.yolo_model.model_path:
                engine._yolo.load_model(preset.yolo_model.model_path)

            # Load trigger zone
            engine._trigger_zone = VisionTriggerZone.objects.filter(
                preset=preset
            ).first()

            # Load ROIs
            engine._rois = list(
                VisionROI.objects.filter(preset=preset, is_active=True).order_by("sequence")
            )

            engine._state = STATE_PART_VALID
            engine._transition(STATE_WAITING_DETECTION)
            engine._persist_state()
            return True

    @classmethod
    def get_state(cls) -> dict:
        """Return a serialised snapshot of the current engine state."""
        engine = cls()
        with engine._lock:
            return engine._build_state_dict()

    @classmethod
    def get_or_create_state_model(cls) -> VisionInspectionState:
        """Return the singleton state model instance."""
        obj, _ = VisionInspectionState.objects.get_or_create(pk=1)
        return obj

    # ------------------------------------------------------------------ #
    #  State machine internals
    # ------------------------------------------------------------------ #

    def _transition(self, new_state: str, **extra):
        """Transition to *new_state* and persist."""
        self._state = new_state
        # Apply extra fields
        for k, v in extra.items():
            setattr(self, f"_{k}", v)
        self._persist_state()

    def _persist_state(self, error: str | None = None):
        """Save the current state to the database for UI polling."""
        try:
            obj = self.get_or_create_state_model()
            obj.current_state = self._state
            obj.employee_code = self._employee_code
            obj.employee_name = self._employee_name
            obj.employee_valid = bool(self._employee_code)
            obj.part_number = self._part_number
            obj.part_valid = bool(self._preset)
            obj.preset_name = self._preset.name if self._preset else None
            obj.preset_version = self._preset.version if self._preset else None
            obj.camera_status = self._camera.get_status()
            obj.trigger_auto = True
            obj.error_message = error
            obj.roi_results = []
            obj.final_result = self._get_final_result()
            obj.detected_objects = getattr(self, "_last_detections", [])
            obj.last_inspection_id = self._last_inspection_id
            obj.save(update_fields=[
                "current_state", "employee_code", "employee_name",
                "employee_valid", "part_number", "part_valid",
                "preset_name", "preset_version", "camera_status",
                "trigger_auto", "error_message", "roi_results",
                "final_result", "detected_objects", "last_inspection_id",
                "updated_at",
            ])
        except Exception as exc:
            logger.error("Failed to persist state: %s", exc)

    def _get_final_result(self) -> str:
        if self._state == STATE_RESULT_OK:
            return "OK"
        if self._state in (STATE_RESULT_NOT_OK, STATE_ERROR):
            if self._state == STATE_ERROR:
                return "INSPECTION ERROR"
            return "NOT OK"
        if self._state == STATE_AUTO_TRIGGER:
            return "TRIGGERED"
        if self._state == STATE_INSPECTING:
            return "INSPECTING"
        return "PENDING"

    def _build_state_dict(self) -> dict:
        """Build a serialisable state dict for API/UI responses."""
        return {
            "state": self._state,
            "employee_code": self._employee_code,
            "employee_name": self._employee_name,
            "part_number": self._part_number,
            "preset_name": self._preset.name if self._preset else None,
            "preset_version": self._preset.version if self._preset else None,
            "camera_status": self._camera.get_status(),
            "trigger_auto": self._trigger_active,
            "error": self.__dict__.get("_error_message"),
            "roi_results": [],
            "final_result": "PENDING",
            "detected_objects": [],
            "last_inspection_id": self._last_inspection_id,
        }

    def _reset_state_machine(self):
        """Reset to waiting-for-employee after re-arming."""
        self._stability.reset()
        self._trigger_active = False
        self._part_was_in_zone = False
        self._transition(STATE_WAITING_EMPLOYEE, error=None)

    def _re_arm(self):
        """Move to RE_ARM then back to waiting for part detection."""
        self._transition(STATE_RE_ARM)
        self._stability.reset()
        self._trigger_active = False
        if self._preset is not None:
            self._transition(STATE_WAITING_DETECTION)
        else:
            self._transition(STATE_WAITING_EMPLOYEE)

    # ------------------------------------------------------------------ #
    #  Main engine loop
    # ------------------------------------------------------------------ #

    def _run(self):
        """Main background loop."""
        frame_interval = 1.0 / CAMERA_FPS

        while not self._stop_event.is_set():
            try:
                self._process_frame()
            except Exception as exc:
                logger.exception("Engine loop error: %s", exc)
                self._state = STATE_ERROR
                self._persist_state(error=str(exc))

            time.sleep(frame_interval)

    def _process_frame(self):
        """Process a single frame through the state machine."""
        current_time = time.time()

        # --- Camera feed ---
        ok, frame = self._camera.get_frame()
        if not ok or frame is None:
            if self._camera.get_status() != CAMERA_STATES["ERROR"]:
                self._persist_state(error="Camera not available")
            return

        # --- State machine ---
        if self._state == STATE_WAITING_EMPLOYEE:
            return

        if self._state == STATE_EMPLOYEE_VALID or self._state == STATE_WAITING_PART:
            return

        if self._state == STATE_PART_VALID or \
           self._state == STATE_WAITING_DETECTION:
            if self._preset is None:
                self._state = STATE_WAITING_PART
                self._persist_state(error="No active preset loaded")
                return
            # Ensure camera is running
            if self._camera.get_status() != CAMERA_STATES["ON"]:
                if not self._trigger_active:
                    self._camera.start_camera(
                        camera_index=getattr(self._preset, "camera_index", 0),
                        target_fps=CAMERA_FPS,
                    )
            # Run YOLO detection
            detections = self._yolo.detect(
                frame,
                conf=self._preset.confidence_threshold if self._preset else 0.5,
            )
            self._last_detections = detections

            # Check for the expected part class
            expected_class = self._preset.part_class if self._preset else None
            part_detections = [
                d for d in detections if d["class"] == expected_class
            ]

            if part_detections:
                # Take the first (highest confidence) detection
                part_det = max(part_detections, key=lambda d: d["confidence"])
                bbox = (part_det["x1"], part_det["y1"], part_det["x2"], part_det["y2"])
                self._current_part_bbox = bbox
                self._transition(STATE_PART_DETECTED)
                self._persist_state()
            else:
                # Check for wrong part detection
                if detections:
                    wrong_classes = set(d["class"] for d in detections)
                    if expected_class not in wrong_classes:
                        self._state = STATE_WAITING_DETECTION
                        self._persist_state(error="WRONG PART")
                        return
            return

        if self._state == STATE_PART_DETECTED:
            # Check if part is in trigger zone
            tz = self._trigger_zone
            if tz is None:
                self._persist_state(error="No trigger zone configured")
                return

            polygon = [list(tz.p1), list(tz.p2), list(tz.p3), list(tz.p4)]
            # Scale polygon to current frame dimensions
            h, w = frame.shape[:2]
            frame_polygon = [[p[0] * w, p[1] * h] for p in polygon]

            result = check_part_in_zone(
                self._current_part_bbox,
                frame_polygon,
                check_mode="center",
            )

            if result["inside"]:
                self._part_was_in_zone = True
                self._transition(STATE_PART_IN_ZONE)
                self._persist_state()
            return

        if self._state == STATE_PART_IN_ZONE:
            # Stability check
            tz = self._trigger_zone
            polygon = [list(tz.p1), list(tz.p2), list(tz.p3), list(tz.p4)]
            h, w = frame.shape[:2]
            frame_polygon = [[p[0] * w, p[1] * h] for p in polygon]

            result = check_part_in_zone(
                self._current_part_bbox,
                frame_polygon,
                check_mode="center",
            )

            self._transition(STATE_STABILITY_CHECK)
            stable = self._stability.update(result["inside"])

            if stable:
                self._triggered_inspection(frame)
            else:
                self._persist_state()
            return

        if self._state == STATE_STABILITY_CHECK:
            # Already handled above — this state is transient
            tz = self._trigger_zone
            if tz is None:
                return
            polygon = [list(tz.p1), list(tz.p2), list(tz.p3), list(tz.p4)]
            h, w = frame.shape[:2]
            frame_polygon = [[p[0] * w, p[1] * h] for p in polygon]

            result = check_part_in_zone(
                self._current_part_bbox,
                frame_polygon,
                check_mode="center",
            )

            if not result["inside"]:
                self._stability.reset()
                self._transition(STATE_PART_DETECTED)
                self._persist_state()
                return

            stable = self._stability.update(True)
            if stable:
                self._triggered_inspection(frame)
            else:
                self._persist_state()
            return

        if self._state in (STATE_RESULT_OK, STATE_RESULT_NOT_OK,
                           STATE_WAIT_PART_LEAVE, STATE_RE_ARM):
            # Check if part has left the trigger zone
            tz = self._trigger_zone
            if tz is None:
                self._re_arm()
                return

            polygon = [list(tz.p1), list(tz.p2), list(tz.p3), list(tz.p4)]
            h, w = frame.shape[:2]
            frame_polygon = [[p[0] * w, p[1] * h] for p in polygon]

            # Re-detect to get current part position
            detections = self._yolo.detect(
                frame,
                conf=self._preset.confidence_threshold if self._preset else 0.5,
            )
            expected_class = self._preset.part_class if self._preset else None
            part_detections = [
                d for d in detections if d["class"] == expected_class
            ]

            if part_detections:
                part_det = max(part_detections, key=lambda d: d["confidence"])
                bbox = (part_det["x1"], part_det["y1"], part_det["x2"], part_det["y2"])
                result = check_part_in_zone(bbox, frame_polygon, check_mode="center")
                if not result["inside"]:
                    self._re_arm()
            else:
                self._re_arm()
            return

        # Default: stay in current state
        return

    # ------------------------------------------------------------------ #
    #  Inspection logic
    # ------------------------------------------------------------------ #

    def _triggered_inspection(self, frame):
        """Perform the actual ROI inspection on the current frame."""
        self._transition(STATE_AUTO_TRIGGER)
        self._persist_state()

        try:
            self._transition(STATE_INSPECTING)
            self._persist_state()

            if self._yolo is None or not self._yolo.is_loaded():
                self._state = STATE_ERROR
                self._persist_state(error="YOLO model not loaded")
                return

            if self._trigger_zone is None or self._preset is None:
                self._state = STATE_ERROR
                self._persist_state(error="Missing preset or trigger zone")
                return

            # Get master image dimensions from the preset
            master_w, master_h = 1280, 720
            if self._preset.master_image:
                try:
                    img = cv2.imread(self._preset.master_image.path)
                    if img is not None:
                        master_h, master_w = img.shape[:2]
                except Exception:
                    pass

            # Master outer part bounding box (stored as normalized coordinates
            # in the trigger zone model, or reconstruct from p1-p4)
            tz = self._trigger_zone
            master_polygon = [[p[0] * master_w, p[1] * master_h] for p in
                             [list(tz.p1), list(tz.p2), list(tz.p3), list(tz.p4)]]
            x_coords = [p[0] for p in master_polygon]
            y_coords = [p[1] for p in master_polygon]
            master_bbox = (min(x_coords), min(y_coords), max(x_coords), max(y_coords))

            actual_bbox = self._current_part_bbox

            # Build ROI configs
            roi_configs = []
            for roi in self._rois:
                roi_configs.append({
                    "roi_name": roi.roi_name,
                    "sequence": roi.sequence,
                    "polygon": roi.polygon,
                    "p1": list(roi.p1),
                    "p2": list(roi.p2),
                    "p3": list(roi.p3),
                    "p4": list(roi.p4),
                    "is_active": roi.is_active,
                    "expected_class": roi.expected_class,
                    "min_count": roi.min_count,
                    "max_count": roi.max_count,
                    "confidence_threshold": roi.confidence_threshold,
                    "inspection_rule": roi.inspection_rule,
                    "ref_width": master_w,
                    "ref_height": master_h,
                })

            # Transform ROIs
            transformed_rois = transform_rois_to_actual(
                roi_configs,
                master_bbox,
                actual_bbox,
                master_w,
                master_h,
            )

            # Run detection on the frame
            detections = self._yolo.detect(
                frame,
                conf=self._preset.confidence_threshold,
            )

            # Validate each ROI
            roi_results = []
            all_match = True

            for tr in transformed_rois:
                if not tr.get("transformed", False) or tr.get("pixel_polygon") is None:
                    roi_results.append({
                        "roi_name": tr["roi_name"],
                        "result": "SKIPPED",
                        "details": "ROI not transformed or disabled",
                    })
                    continue

                # Extract ROI region and re-detect within it
                cropped, mask = extract_roi_region(frame, tr["pixel_polygon"])
                if cropped is None:
                    roi_results.append({
                        "roi_name": tr["roi_name"],
                        "result": "ERROR",
                        "details": "Failed to extract ROI region",
                    })
                    all_match = False
                    continue

                roi_detections = self._yolo.detect(
                    cropped,
                    conf=tr["confidence_threshold"],
                )

                # Also filter full-frame detections by ROI polygon
                full_filtered = filter_detections_in_roi(detections, tr["pixel_polygon"])
                roi_detections = list(set(full_filtered + roi_detections))

                # Deduplicate
                seen = set()
                unique = []
                for d in roi_detections:
                    key = (d["class"], round(d["x1"]), round(d["y1"]), round(d["x2"]), round(d["y2"]))
                    if key not in seen:
                        seen.add(key)
                        unique.append(d)
                roi_detections = unique

                result = validate_roi(tr, roi_detections)
                result["roi_name"] = tr["roi_name"]
                roi_results.append(result)

                if result["result"] not in ("MATCH", "SKIPPED"):
                    all_match = False

            self._last_detections = detections

            # Save inspection log
            log_entry = VisionInspectionLog.objects.create(
                employee_code=self._employee_code,
                part_number=self._part_number,
                preset_name=self._preset.name if self._preset else None,
                preset_version=self._preset.version if self._preset else None,
                yolo_model_name=self._preset.yolo_model.name if self._preset and self._preset.yolo_model else None,
                yolo_model_version=self._preset.yolo_model.version if self._preset and self._preset.yolo_model else None,
                result="OK" if all_match else "NOT_OK",
                roi_results=roi_results,
                detected_objects=detections,
                error_details=None if all_match else "One or more ROIs did not match",
                stable_frames_used=self._stability.count,
            )
            self._last_inspection_id = log_entry.pk

            # Store result image (encode frame as JPEG in memory)
            # This would be saved to the inspection_image field
            self._last_frame_with_results = frame

            # Determine final result
            if all_match:
                self._transition(STATE_RESULT_OK)
            else:
                self._transition(STATE_RESULT_NOT_OK)

            self._persist_state()

        except Exception as exc:
            logger.exception("Inspection error: %s", exc)
            self._state = STATE_ERROR
            self._persist_state(error=f"INSPECTION ERROR: {exc}")
            return

    # ------------------------------------------------------------------ #
    #  Re-arm logic
    # ------------------------------------------------------------------ #

    def _check_rearm(self):
        """Check whether the system can re-arm (part has left the zone)."""
        if self._state in (STATE_RESULT_OK, STATE_RESULT_NOT_OK):
            self._state = STATE_WAIT_PART_LEAVE
            self._persist_state()


# --------------------------------------------------------------------------- #
#  Module-level convenience
# --------------------------------------------------------------------------- #

_engine = InspectionEngine()

start_engine = InspectionEngine.start
stop_engine = InspectionEngine.stop
set_employee = InspectionEngine.set_employee
set_part = InspectionEngine.set_part
get_state = InspectionEngine.get_state
get_or_create_state_model = InspectionEngine.get_or_create_state_model
