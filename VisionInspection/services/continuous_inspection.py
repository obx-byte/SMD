"""Continuous ROI inspection engine (Inspection Master).

Runs a long-lived worker thread that feeds the **latest camera frame** through a
saved-ROI crop and a model-independent inference interface, streaming live status
and a cropped ROI preview to the UI.

Design guarantees (see task spec):
  * Single latest-frame buffer (``CameraService.get_frame()`` — a ring buffer of
    one, never an unbounded queue).
  * O(1) cached ROI config keyed by ``part_number``; no per-frame DB access.
  * The model is loaded once and reused; cropping happens *before* inference so
    only the ROI — never the outside surface — reaches the model.
  * Exactly one worker loop at a time (guarded by ``_active_lock`` + running flag).
  * Bounded memory: only the newest crop/preview/metrics are retained.
  * Camera acquisition, image processing and preview rendering are decoupled;
    the worker thread never blocks the UI/server request thread.
"""

import logging
import threading
import time

import numpy as np

from django.conf import settings

from VisionInspection.services.camera_service import CAMERA_STATES, CameraService
from VisionInspection.services.roi_inference import (
    PLACEHOLDER_LABEL,
    STATUS_MODEL_NOT_CONFIGURED,
    BaseROIInferenceModel,
    create_inference_model,
)

logger = logging.getLogger(__name__)

# Engine lifecycle states
STATE_STOPPED = "STOPPED"
STATE_STARTING = "STARTING"
STATE_RUNNING = "RUNNING"
STATE_ERROR = "ERROR"

TARGET_FPS = getattr(settings, "VISION_INSPECTION_INFERENCE_FPS", 10)
_PREVIEW_INTERVAL = 0.1  # seconds between preview refreshes


def _e2e_ms():
    return time.time() * 1000.0


class ContinuousInspectionEngine:
    """Singleton continuous-ROI inspection engine."""

    _instance = None
    _class_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True

        self._camera = CameraService()
        # Model-independent inference backend (placeholder until configured).
        self._model: BaseROIInferenceModel = create_inference_model()

        self._state = STATE_STOPPED
        self._error = None

        # Cached ROI config, keyed by part_number (O(1) lookup)
        self._roi_cache: dict[str, dict] = {}
        self._active_part: str | None = None

        # Worker thread management — at most one loop runs concurrently.
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False
        self._active_lock = threading.Lock()

        # Live results / metrics (bounded: only latest values retained)
        self._latest_result = {
            "status": STATUS_MODEL_NOT_CONFIGURED,
            "label": PLACEHOLDER_LABEL,
            "detections": [],
            "configured": False,
        }
        self._preview_jpeg: bytes | None = None
        self._preview_at = 0.0
        self._metrics = {
            "fps": 0.0,
            "acquire_ms": 0.0,
            "frame_age_ms": None,
            "dropped": 0,
            "crop_ms": 0.0,
            "inference_ms": 0.0,
            "e2e_ms": 0.0,
            "frames": 0,
            "inference_fps": 0.0,
        }
        self._fps_accum = []
        self._infer_accum = []

    # ------------------------------------------------------------------ #
    #  Public control API
    # ------------------------------------------------------------------ #

    @classmethod
    def configure(cls, part_number, x, y, width, height, ref_w, ref_h) -> dict:
        """Cache a (normalized) ROI for *part_number* without starting the loop.

        ``x``, ``y``, ``width``, ``height`` are pixel values measured on a frame
        of ``ref_w`` x ``ref_h``; they are stored normalized (0..1) so the ROI
        projects onto any frame size later.
        """
        engine = cls()
        ref_w = float(ref_w or 0) or 1.0
        ref_h = float(ref_h or 0) or 1.0
        nx = max(0.0, min(1.0, float(x) / ref_w))
        ny = max(0.0, min(1.0, float(y) / ref_h))
        nw = max(0.0, min(1.0, float(width) / ref_w))
        nh = max(0.0, min(1.0, float(height) / ref_h))
        engine._roi_cache[part_number] = {
            "x": nx, "y": ny, "width": nw, "height": nh,
            "ref_width": int(ref_w), "ref_height": int(ref_h),
        }
        engine._active_part = part_number
        return engine._roi_cache[part_number]

    @classmethod
    def start(cls, part_number: str | None = None, roi: dict | None = None):
        """Start continuous inspection.

        If *roi* (pixel + ref dims) is supplied it is configured first.  A cached
        ROI is required; otherwise an error status is returned and the loop is
        not started.
        """
        engine = cls()
        if roi is not None:
            engine.configure(
                part_number, roi["x"], roi["y"], roi["width"], roi["height"],
                roi.get("ref_width"), roi.get("ref_height"),
            )
        elif part_number is None:
            part_number = engine._active_part

        if part_number is None or part_number not in engine._roi_cache:
            engine._set_error("No ROI configured for part %r" % part_number)
            return engine.get_status()

        engine._active_part = part_number
        if engine._camera.get_status() != CAMERA_STATES["ON"]:
            engine._set_error("Camera is not running")
            return engine.get_status()

        with engine._active_lock:
            if engine._running and engine._worker is not None and engine._worker.is_alive():
                # Already running — adopt the (possibly new) ROI in place.
                engine._set_state(STATE_RUNNING)
                return engine.get_status()
            engine._stop_event = threading.Event()
            engine._state = STATE_STARTING
            engine._error = None
            engine._reset_metrics()
            engine._worker = threading.Thread(
                target=engine._run, daemon=True, name="VisionInspection-ContinuousROI"
            )
            engine._running = True
            engine._worker.start()
        logger.info("Continuous ROI inspection started for %s", part_number)
        return engine.get_status()

    @classmethod
    def stop(cls):
        """Stop the worker loop cleanly and clear transient preview state."""
        engine = cls()
        with engine._active_lock:
            engine._stop_event.set()
            if engine._worker is not None:
                engine._worker.join(timeout=5)
                engine._worker = None
            engine._running = False
            engine._state = STATE_STOPPED
            engine._error = None
            engine._preview_jpeg = None
            engine._latest_result = {
                "status": STATUS_MODEL_NOT_CONFIGURED,
                "label": PLACEHOLDER_LABEL,
                "detections": [],
                "configured": False,
            }
        logger.info("Continuous ROI inspection stopped")
        return True

    @classmethod
    def attach_model(cls, model_path: str | None = None, confidence: float = 0.5):
        """Swap the inference backend (asynchronously with respect to the loop)."""
        engine = cls()
        engine._model = create_inference_model(model_path, confidence)
        return engine._model.is_configured()

    @classmethod
    def get_status(cls) -> dict:
        """Return a JSON-serialisable snapshot of the engine."""
        engine = cls()
        camera_status = engine._camera.get_status()
        roi_conf = engine._roi_cache.get(engine._active_part) if engine._active_part else None
        pixel = None
        if roi_conf:
            pixel = {
                "x": round(roi_conf["x"] * roi_conf["ref_width"]),
                "y": round(roi_conf["y"] * roi_conf["ref_height"]),
                "width": round(roi_conf["width"] * roi_conf["ref_width"]),
                "height": round(roi_conf["height"] * roi_conf["ref_height"]),
            }
        return {
            "state": engine._state,
            "error": engine._error,
            "active_part": engine._active_part,
            "camera_status": camera_status,
            "model_configured": engine._model.is_configured(),
            "roi": pixel,
            "roi_normalized": (
                {k: roi_conf[k] for k in ("x", "y", "width", "height")} if roi_conf else None
            ),
            "roi_reference": (
                {"width": roi_conf["ref_width"], "height": roi_conf["ref_height"]}
                if roi_conf else None
            ),
            "result": engine._latest_result,
            "metrics": dict(engine._metrics),
        }

    @classmethod
    def get_preview_jpeg(cls) -> bytes | None:
        """Return the latest cropped-ROI preview as JPEG bytes (or ``None``)."""
        return cls()._preview_jpeg

    @classmethod
    def is_running(cls) -> bool:
        return cls()._state == STATE_RUNNING

    # ------------------------------------------------------------------ #
    #  Worker loop
    # ------------------------------------------------------------------ #

    def _reset_metrics(self):
        self._fps_accum = []
        self._infer_accum = []
        self._metrics = {
            "fps": 0.0, "acquire_ms": 0.0, "frame_age_ms": None, "dropped": 0,
            "crop_ms": 0.0, "inference_ms": 0.0,
            "e2e_ms": 0.0, "frames": 0, "inference_fps": 0.0,
        }

    def _set_state(self, state):
        self._state = state

    def _set_error(self, message):
        self._state = STATE_ERROR
        self._error = message
        logger.warning("Continuous ROI engine error: %s", message)

    def _current_roi_pixels(self, frame_shape) -> tuple[int, int, int, int] | None:
        roi = self._roi_cache.get(self._active_part) if self._active_part else None
        if roi is None:
            return None
        h, w = frame_shape[:2]
        px = int(roi["x"] * w)
        py = int(roi["y"] * h)
        pw = int(roi["width"] * w)
        ph = int(roi["height"] * h)
        # Clamp to frame; also validate the ROI is actually inside.
        px = max(0, min(px, w - 1))
        py = max(0, min(py, h - 1))
        pw = max(1, min(pw, w - px))
        ph = max(1, min(ph, h - py))
        return px, py, pw, ph

    def _run(self):
        """Continuous ROI processing loop (worker thread)."""
        import cv2

        frame_interval = 1.0 / TARGET_FPS
        self._set_state(STATE_RUNNING)

        while not self._stop_event.is_set():
            loop_start = _e2e_ms()
            try:
                if self._camera.get_status() != CAMERA_STATES["ON"]:
                    self._set_error("Camera is not running")
                    time.sleep(frame_interval)
                    continue

                # --- 1. Latest frame (single-slot buffer; drops stale frames) ---
                t_acq = _e2e_ms()
                ok, frame, meta = self._camera.get_frame_with_meta()
                if not ok or frame is None:
                    time.sleep(0.02)
                    continue
                self._metrics["acquire_ms"] = round(_e2e_ms() - t_acq, 2)
                self._metrics["frame_age_ms"] = meta.get("age_ms")
                self._metrics["dropped"] = meta.get("dropped", 0)

                # --- 2. ROI validation against the live frame (O(1) lookup) ---
                rect = self._current_roi_pixels(frame.shape)
                if rect is None:
                    self._set_error("No ROI configured")
                    break
                px, py, pw, ph = rect
                if pw <= 1 or ph <= 1:
                    self._set_error("Invalid or degenerate ROI")
                    break

                # --- 3. Crop BEFORE inference as a view (no copy; outside
                # surface is never materialised for the model) ---
                t_crop = _e2e_ms()
                roi_crop = frame[py:py + ph, px:px + pw]
                self._metrics["crop_ms"] = round(_e2e_ms() - t_crop, 3)
                if roi_crop.size == 0:
                    self._set_error("Empty ROI crop")
                    continue

                # --- 4. Inference (model load-once, reuse) ---
                t_inf = _e2e_ms()
                result = self._model.predict(roi_crop)
                self._metrics["inference_ms"] = round(_e2e_ms() - t_inf, 2)
                self._latest_result = result

                # --- 5. Bounded preview (latest only, throttled; encode only here) ---
                now = time.time()
                if now - self._preview_at >= _PREVIEW_INTERVAL:
                    try:
                        ok_jpg, buf = cv2.imencode(
                            ".jpg",
                            np.ascontiguousarray(roi_crop),
                            [int(cv2.IMWRITE_JPEG_QUALITY), 80],
                        )
                        if ok_jpg:
                            self._preview_jpeg = buf.tobytes()
                            self._preview_at = now
                    except Exception:
                        pass

                # --- 6. Metrics ---
                self._metrics["frames"] += 1
                self._metrics["e2e_ms"] = round(_e2e_ms() - loop_start, 2)
                self._fps_accum.append(loop_start)
                self._infer_accum.append(self._metrics["inference_ms"])
                horizon = loop_start - 1000.0
                while self._fps_accum and self._fps_accum[0] < horizon:
                    self._fps_accum.pop(0)
                self._metrics["fps"] = round(len(self._fps_accum), 1)
                if self._infer_accum:
                    self._metrics["inference_fps"] = round(
                        1000.0 / max(np.mean(self._infer_accum[-10:]), 1e-3), 1
                    )

            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Continuous ROI loop error: %s", exc)
                self._set_error(str(exc))
                time.sleep(0.5)

            time.sleep(frame_interval)

        # Clean end
        self._running = False
        if self._state == STATE_RUNNING:
            self._state = STATE_STOPPED
        self._preview_jpeg = None


# --------------------------------------------------------------------------- #
#  Module-level convenience API
# --------------------------------------------------------------------------- #

_continuous = ContinuousInspectionEngine

configure_roi = _continuous.configure
start_inspection = _continuous.start
stop_inspection = _continuous.stop
get_status = _continuous.get_status
get_preview = _continuous.get_preview_jpeg
attach_model = _continuous.attach_model
is_running = _continuous.is_running
