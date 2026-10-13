"""YOLO service for VisionInspection.

Loads and caches a YOLO model (Ultralytics .pt format).  Detection results
include class label, confidence, and bounding box coordinates.

The model is loaded once on first use and reused for subsequent detections.
"""

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
#  Singleton YOLO service
# --------------------------------------------------------------------------- #


class YOLOModelService:
    """Singleton service that manages a cached YOLO model and runs inference."""

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
        self._model: Any | None = None
        self._model_path: str | None = None
        self._class_names: list[str] = []

    # ------------------------------------------------------------------ #
    #  Model management
    # ------------------------------------------------------------------ #

    def load_model(self, model_path: str) -> bool:
        """Load (and cache) a YOLO model from *model_path*.

        Parameters
        ----------
        model_path : str
            Path to a ``.pt`` file or an Ultralytics model name.

        Returns
        -------
        bool
            ``True`` if the model was loaded successfully.
        """
        with self._lock:
            if self._model is not None and self._model_path == model_path:
                return True

            if not model_path or not os.path.exists(model_path):
                logger.error("YOLO model path does not exist: %s", model_path)
                return False

            try:
                from ultralytics import YOLO

                self._model = YOLO(model_path)
                self._model_path = model_path

                # Cache class names
                if hasattr(self._model, "names") and self._model.names:
                    self._class_names = list(self._model.names.values())
                else:
                    self._class_names = []

                logger.info("YOLO model loaded: %s (classes: %s)", model_path, self._class_names)
                return True

            except ImportError:
                logger.error("ultralytics package is not installed")
                return False
            except Exception as exc:
                logger.error("Failed to load YOLO model %s: %s", model_path, exc)
                self._model = None
                self._model_path = None
                self._class_names = []
                return False

    def get_model(self):
        """Return the cached model object (or ``None``)."""
        return self._model

    def get_class_names(self) -> list[str]:
        """Return the list of class names from the loaded model."""
        return self._class_names

    def is_loaded(self) -> bool:
        """Return ``True`` if a model is currently loaded."""
        return self._model is not None

    def get_model_path(self) -> str | None:
        """Return the path of the currently loaded model."""
        return self._model_path

    # ------------------------------------------------------------------ #
    #  Detection
    # ------------------------------------------------------------------ #

    def detect(
        self,
        frame,
        conf: float = 0.5,
        iou: float = 0.45,
    ) -> list[dict[str, Any]]:
        """Run YOLO detection on a single frame.

        Parameters
        ----------
        frame : numpy.ndarray
            BGR image array from OpenCV.
        conf : float
            Minimum confidence threshold.
        iou : float
            IoU threshold for NMS.

        Returns
        -------
        list[dict]
            Each dict has keys: ``class``, ``class_id``, ``confidence``,
            ``x1``, ``y1``, ``x2``, ``y2``.
        """
        if self._model is None:
            logger.warning("YOLO model is not loaded; cannot detect")
            return []

        try:
            results = self._model(frame, conf=conf, iou=iou, verbose=False)[0]

            detections: list[dict[str, Any]] = []
            if results.boxes is None or len(results.boxes) == 0:
                return detections

            for box in results.boxes:
                cls_id = int(box.cls[0])
                class_name = (
                    self._class_names[cls_id]
                    if 0 <= cls_id < len(self._class_names)
                    else str(cls_id)
                )
                conf_val = float(box.conf[0])

                x1, y1, x2, y2 = box.xyxy[0]
                x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)

                detections.append(
                    {
                        "class": class_name,
                        "class_id": cls_id,
                        "confidence": conf_val,
                        "x1": x1,
                        "y1": y1,
                        "x2": x2,
                        "y2": y2,
                    }
                )

            return detections

        except Exception as exc:
            logger.error("YOLO detection error: %s", exc)
            return []


# --------------------------------------------------------------------------- #
#  Module-level convenience API
# --------------------------------------------------------------------------- #

_service = YOLOModelService()

load_model = _service.load_model
get_model = _service.get_model
get_class_names = _service.get_class_names
is_loaded = _service.is_loaded
get_model_path = _service.get_model_path
detect = _service.detect
