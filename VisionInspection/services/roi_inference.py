"""Model-independent ROI inference interface.

This module defines a small, stable contract for running inference on a *cropped
ROI frame* so a detection / segmentation model can be plugged in later without
touching ROI selection, camera handling, or the UI pipeline.

A model is loaded **once** and reused for subsequent frames.  Until a model is
configured the placeholder implementation reports
``ROI READY — MODEL NOT CONFIGURED`` and never fabricates OK / NOT_OK results.
"""

import logging
import threading
from abc import ABC, abstractmethod

from VisionInspection.services.model_registry import YOLO_IMAGE_SIZE

logger = logging.getLogger(__name__)

STATUS_MODEL_NOT_CONFIGURED = "MODEL_NOT_CONFIGURED"
STATUS_OK = "OK"
STATUS_ERROR = "ERROR"
PLACEHOLDER_LABEL = "ROI READY — MODEL NOT CONFIGURED"


# --------------------------------------------------------------------------- #
#  Interface
# --------------------------------------------------------------------------- #


class BaseROIInferenceModel(ABC):
    """Contract for a ROI inference model.

    Responsibilities match the requested inspection interface:
    ``load_model`` / ``predict`` / ``get_result`` / ``release``.
    """

    @abstractmethod
    def load_model(self, model_path: str) -> bool:
        """Load (and cache) a model.  Returns ``True`` on success."""

    @abstractmethod
    def predict(self, roi_frame):
        """Run inference on a single cropped ROI frame (BGR ndarray).

        Must return a JSON-serialisable dict with at least::
            {"status": ..., "label": ..., "detections": [...], "configured": bool}
        """

    @abstractmethod
    def get_result(self) -> dict:
        """Return the most recent prediction result (no new inference)."""

    @abstractmethod
    def release(self) -> None:
        """Release the loaded model and any GPU/resources."""

    @abstractmethod
    def is_configured(self) -> bool:
        """Return ``True`` if a real model is loaded and ready."""


# --------------------------------------------------------------------------- #
#  Placeholder (used until a YOLO model is supplied)
# --------------------------------------------------------------------------- #


class PlaceholderROIInferenceModel(BaseROIInferenceModel):
    """Reports readiness without producing any predictions.

    This keeps the continuous ROI pipeline fully functional (cropping, metrics,
    live preview, status) while making it explicit that no model is configured.
    """

    def __init__(self):
        self._last_result = {
            "status": STATUS_MODEL_NOT_CONFIGURED,
            "label": PLACEHOLDER_LABEL,
            "detections": [],
            "configured": False,
        }
        self._lock = threading.Lock()

    def load_model(self, model_path: str) -> bool:
        # The placeholder deliberately holds no model.
        logger.info("PlaceholderROIInferenceModel.load_model(%r) — no-op", model_path)
        return False

    def predict(self, roi_frame):
        with self._lock:
            self._last_result = {
                "status": STATUS_MODEL_NOT_CONFIGURED,
                "label": PLACEHOLDER_LABEL,
                "detections": [],
                "configured": False,
                "roi_shape": (
                    list(roi_frame.shape[:2]) if roi_frame is not None else None
                ),
            }
            return dict(self._last_result)

    def get_result(self) -> dict:
        with self._lock:
            return dict(self._last_result)

    def release(self) -> None:
        return None

    def is_configured(self) -> bool:
        return False


# --------------------------------------------------------------------------- #
#  YOLO adapter — ready for later, not activated unless a path is provided
# --------------------------------------------------------------------------- #


class YOLOROIInferenceModel(BaseROIInferenceModel):
    """Loads a YOLO model once and reuses it for every ``predict`` call.

    Only cropped ROI frames are ever passed here; the outside surface is never
    processed.  Instantiated with a real ``model_path`` to activate.
    """

    def __init__(self, model_path: str, confidence: float = 0.5):
        self._model_path = model_path
        self._confidence = confidence
        self._model = None
        self._class_names: list[str] = []
        self._loaded = False
        self._last_result = {
            "status": STATUS_MODEL_NOT_CONFIGURED,
            "label": PLACEHOLDER_LABEL,
            "detections": [],
            "configured": False,
        }
        self._lock = threading.Lock()
        self._loaded = self.load_model(model_path)

    def load_model(self, model_path: str) -> bool:
        with self._lock:
            if self._loaded and self._model_path == model_path:
                return True
            if not model_path:
                return False
            try:
                import os

                if not os.path.exists(model_path):
                    logger.error("YOLO ROI model path missing: %s", model_path)
                    return False
                from ultralytics import YOLO

                self._model = YOLO(model_path)
                self._model_path = model_path
                self._class_names = (
                    list(self._model.names.values()) if getattr(self._model, "names", None) else []
                )
                self._loaded = True
                logger.info("YOLO ROI model loaded: %s", model_path)
                return True
            except Exception as exc:
                logger.error("Failed to load YOLO ROI model %s: %s", model_path, exc)
                self._model = None
                self._loaded = False
                return False

    def predict(self, roi_frame):
        with self._lock:
            if not self._loaded or self._model is None:
                self._last_result = {
                    "status": STATUS_MODEL_NOT_CONFIGURED,
                    "label": PLACEHOLDER_LABEL,
                    "detections": [],
                    "configured": False,
                }
                return dict(self._last_result)
            try:
                results = self._model(
                    roi_frame,
                    conf=self._confidence,
                    verbose=False,
                    imgsz=YOLO_IMAGE_SIZE,
                )[0]
                detections = []
                if results.boxes is not None and len(results.boxes):
                    for box in results.boxes:
                        cls_id = int(box.cls[0])
                        x1, y1, x2, y2 = (int(v) for v in box.xyxy[0])
                        detections.append(
                            {
                                "class": (
                                    self._class_names[cls_id]
                                    if 0 <= cls_id < len(self._class_names)
                                    else str(cls_id)
                                ),
                                "confidence": round(float(box.conf[0]), 4),
                                "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                            }
                        )
                self._last_result = {
                    "status": STATUS_OK,
                    "label": f"{len(detections)} detection(s) in ROI",
                    "detections": detections,
                    "configured": True,
                }
                return dict(self._last_result)
            except Exception as exc:
                logger.exception("YOLO ROI inference error: %s", exc)
                self._last_result = {
                    "status": STATUS_ERROR,
                    "label": f"Inference error: {exc}",
                    "detections": [],
                    "configured": True,
                }
                return dict(self._last_result)

    def get_result(self) -> dict:
        with self._lock:
            return dict(self._last_result)

    def release(self) -> None:
        with self._lock:
            self._model = None
            self._loaded = False
            self._class_names = []

    def is_configured(self) -> bool:
        return self._loaded and self._model is not None


# --------------------------------------------------------------------------- #
#  Factory
# --------------------------------------------------------------------------- #


def create_inference_model(model_path: str | None = None, confidence: float = 0.5):
    """Return a configured model if a valid path is given, else the placeholder.

    Keeps the pipeline model-agnostic: callers never instantiate a specific
    backend, so adding YOLO later is a configuration change, not a code change.
    """
    if model_path:
        model = YOLOROIInferenceModel(model_path, confidence=confidence)
        if model.is_configured():
            return model
        logger.warning("Requested model_path could not be loaded; using placeholder")
    return PlaceholderROIInferenceModel()
