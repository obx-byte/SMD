"""Model registry — loads and caches *multiple* YOLO models per part.

The inspection station needs several models per part (airbag, stitch, ...) and
must not reload a model for every inspection.  This service keeps one cached,
thread-safe instance per model file and is the single place where inference is
executed, so duplicate model loading and duplicate inference are impossible.

Class discovery is *real*: the class names and ids are read from the model file
itself, never from a YAML file, never configured by hand.  When the inference
runtime (ultralytics / onnxruntime) is unavailable the failure is reported
verbatim instead of returning invented classes.
"""

import hashlib
import importlib
import logging
import os
import threading
import time
import zipfile

from VisionInspection.services.model_paths import (
    ModelPathResolutionError,
    resolve_model_path,
)

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = (".pt", ".onnx")
MAX_MODEL_BYTES = 1024 * 1024 * 1024  # 1 GiB hard limit for uploads


class ModelLoadError(RuntimeError):
    """Raised when a model cannot be loaded or inspected."""


class ModelRuntimeUnavailableError(ModelLoadError):
    """Raised when the runtime required by a model format is unavailable."""


class _CacheEntry:
    __slots__ = (
        "path",
        "model",
        "names",
        "task",
        "mtime",
        "size",
        "loaded_at",
        "requested_task",
        "lock",
    )

    def __init__(self, path):
        self.path = path
        self.model = None
        self.names = {}
        self.task = ""
        self.mtime = 0.0
        self.size = 0
        self.loaded_at = 0.0
        self.requested_task = ""
        # Per-model lock: two threads asking for the same model load it once.
        self.lock = threading.Lock()


class ModelRegistry:
    """Process-wide cache of loaded models keyed by absolute file path."""

    _instance = None
    _singleton_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._entries = {}
                    cls._instance._entries_lock = threading.Lock()
                    cls._instance._preview_lock = threading.Lock()
        return cls._instance

    # ------------------------------------------------------------------ #
    #  Discovery
    # ------------------------------------------------------------------ #
    def inspect(self, model_path, task=None):
        """Return the real metadata of *model_path* without running inference.

        The result is always derived from the file.  ``classes`` is a list of
        ``{"id": int, "name": str}`` in the model's own class order.
        """
        configured_path = str(model_path or "").strip()
        info = {
            "ok": False,
            "path": model_path,
            "classes": [],
            "num_classes": 0,
            "task": "",
            "error": "",
        }
        try:
            path = str(resolve_model_path(configured_path))
        except ModelPathResolutionError as exc:
            info["error"] = str(exc)
            return info
        info["path"] = path
        if not os.path.isfile(path):
            info["error"] = (
                "The model file does not exist. "
                f"Configured path: {configured_path}. Checked path: {path}."
            )
            return info
        if not path.lower().endswith(SUPPORTED_SUFFIXES):
            info["error"] = (
                "Unsupported model format. Only .pt and .onnx models are accepted."
            )
            return info

        entry = self._entry(path, task=task)
        with entry.lock:
            self._refresh_if_stale(entry, task=task)
            if entry.model is not None:
                info.update(self._payload(entry))
                return info
            if entry.names:
                info.update(
                    {
                        "ok": True,
                        "classes": [
                            {"id": int(cid), "name": str(name)}
                            for cid, name in sorted(entry.names.items(), key=lambda kv: int(kv[0]))
                        ],
                        "num_classes": len(entry.names),
                        "task": entry.task,
                    }
                )
                return info
            info["error"] = (
                "The inference runtime could not read this model's classes. "
                "Install the runtime matching the file (ultralytics for .pt, "
                "onnxruntime for .onnx) and re-upload."
            )
            if entry.task:
                info["task"] = entry.task
            return info

    def _payload(self, entry):
        classes = [
            {"id": int(cid), "name": str(name)}
            for cid, name in sorted(entry.names.items(), key=lambda kv: int(kv[0]))
        ]
        return {
            "ok": True,
            "classes": classes,
            "num_classes": len(classes),
            "task": entry.task,
        }

    def runtime_available(self):
        """True when at least one supported inference runtime can be loaded."""
        return self._import_ultralytics() is not None or self._import_onnx() is not None

    # ------------------------------------------------------------------ #
    #  Inference
    # ------------------------------------------------------------------ #
    def predict(
        self, model_path, frame, conf=0.5, iou=0.45, classes=None, task=None
    ):
        """Run inference with the cached model for *model_path*.

        Returns ``(detections, error)``.  Detections carry the real class id /
        name / confidence / box of the model output; segmentation masks are
        returned as polygon point lists so the caller can draw a real overlay.
        Nothing is fabricated: an error is returned verbatim.
        """
        configured_path = str(model_path or "").strip()
        try:
            path = str(resolve_model_path(configured_path))
        except ModelPathResolutionError as exc:
            return [], str(exc)
        if not os.path.isfile(path):
            return [], (
                "The model file does not exist. "
                f"Configured path: {configured_path}. Checked path: {path}."
            )
        entry = self._entry(path, task=task)
        with entry.lock:
            self._refresh_if_stale(entry, task=task)
            if entry.model is None:
                return [], "The model could not be loaded."
            model = entry.model
            task = entry.task
            t0 = time.perf_counter()
            try:
                detections = self._predict_ultralytics(
                    model, frame, conf, iou, classes, task
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Inference failed for %s: %s", entry.path, exc)
                return [], f"Inference failed: {exc}"
            return detections, round((time.perf_counter() - t0) * 1000.0, 2)

    def predict_segmentation(
        self, model_path, frame, conf=0.50, show_masks=True, show_boxes=True
    ):
        """Run cached segmentation inference and return detections and its rendered frame."""
        configured_path = str(model_path or "").strip()
        try:
            path = str(resolve_model_path(configured_path))
        except ModelPathResolutionError as exc:
            raise ModelLoadError(str(exc)) from exc
        if not path.lower().endswith(SUPPORTED_SUFFIXES):
            raise ModelLoadError("Only .pt and .onnx segmentation models are supported.")
        if not os.path.isfile(path):
            raise ModelLoadError(
                "The model file does not exist. "
                f"Configured path: {configured_path}. Checked path: {path}."
            )

        entry = self._entry(path, task="segment")
        with self._preview_lock, entry.lock:
            self._refresh_if_stale(entry, task="segment")
            model = entry.model
            task = entry.task

            if model is None:
                raise ModelLoadError("The segmentation model could not be loaded.")
            if task != "segment":
                raise ModelLoadError(
                    "The selected model does not expose Ultralytics segmentation results."
                )

            started = time.perf_counter()
            try:
                results = model.predict(
                    source=frame,
                    conf=conf,
                    task="segment",
                    verbose=False,
                    imgsz=640,
                    show_boxes=show_boxes,
                )
                detections = []
                rendered = frame.copy()
                for result in results:
                    names = getattr(result, "names", {}) or {}
                    boxes = getattr(result, "boxes", None)
                    masks = getattr(result, "masks", None)
                    if boxes is not None:
                        for box in boxes:
                            class_id = int(box.cls[0])
                            confidence = float(box.conf[0])
                            if isinstance(names, dict):
                                class_name = names.get(class_id, str(class_id))
                            elif class_id < len(names):
                                class_name = names[class_id]
                            else:
                                class_name = str(class_id)
                            x1, y1, x2, y2 = [
                                int(value) for value in box.xyxy[0]
                            ]
                            detections.append(
                                {
                                    "class_id": class_id,
                                    "class": str(class_name),
                                    "confidence": confidence,
                                    "x1": x1,
                                    "y1": y1,
                                    "x2": x2,
                                    "y2": y2,
                                }
                            )
                    if masks is None and boxes is not None and len(boxes):
                        raise ModelLoadError(
                            "The selected segmentation model returned detections without masks."
                        )
                    try:
                        rendered = result.plot(
                            masks=show_masks,
                            boxes=show_boxes,
                            labels=show_boxes,
                            conf=show_boxes,
                        )
                    except (AttributeError, TypeError):
                        if show_masks or show_boxes:
                            raise
                        rendered = frame.copy()
            except ModelLoadError:
                raise
            except Exception as exc:
                logger.warning(
                    "Segmentation preview failed for %s: %s", entry.path, exc
                )
                raise ModelLoadError(f"Segmentation inference failed: {exc}") from exc
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return detections, rendered, elapsed_ms

    def _predict_ultralytics(self, model, frame, conf, iou, classes, task=""):
        options = {
            "source": frame,
            "conf": conf,
            "iou": iou,
            "imgsz": 640,
            "verbose": False,
        }
        if task == "segment":
            options["task"] = "segment"
        results = model.predict(**options)
        detections = []
        for result in results:
            names = getattr(result, "names", {}) or {}
            boxes = getattr(result, "boxes", None)
            masks = getattr(result, "masks", None)
            mask_xy = None
            if masks is not None:
                try:
                    mask_xy = masks.xy
                except Exception:  # pragma: no cover - defensive
                    mask_xy = None
            if boxes is None:
                continue
            for index, box in enumerate(boxes):
                cls_id = int(box.cls[0])
                if classes is not None and cls_id not in classes:
                    continue
                name = (
                    names.get(cls_id, str(cls_id))
                    if isinstance(names, dict)
                    else names[cls_id]
                    if 0 <= cls_id < len(names)
                    else str(cls_id)
                )
                confidence = float(box.conf[0])
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0]]
                detection = {
                    "class": str(name),
                    "class_id": cls_id,
                    "confidence": confidence,
                    "x1": x1,
                    "y1": y1,
                    "x2": x2,
                    "y2": y2,
                }
                if mask_xy is not None and index < len(mask_xy):
                    try:
                        polygon = [[float(px), float(py)] for px, py in mask_xy[index]]
                        if len(polygon) >= 3:
                            detection["polygon"] = polygon
                    except Exception:  # pragma: no cover - defensive
                        pass
                detections.append(detection)
        return detections

    # ------------------------------------------------------------------ #
    #  Loading internals
    # ------------------------------------------------------------------ #
    def _entry(self, path, task=None):
        absolute_path = os.path.abspath(path)
        key = (absolute_path, str(task or ""))
        with self._entries_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _CacheEntry(absolute_path)
                self._entries[key] = entry
        return entry

    def _refresh_if_stale(self, entry, task=None):
        """Load / reload a model when its file changed or was never loaded."""
        try:
            stat = os.stat(entry.path)
        except OSError as exc:
            raise ModelLoadError(f"Cannot read the model file: {exc}") from exc
        requested_task = str(task or "")
        if (
            entry.model is not None
            and entry.mtime == stat.st_mtime
            and entry.size == stat.st_size
            and entry.requested_task == requested_task
        ):
            return
        self._load(entry, stat, requested_task)

    def _load(self, entry, stat, task=""):
        suffix = os.path.splitext(entry.path)[1].lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise ModelLoadError(
                "Unsupported model format. Only .pt and .onnx models are accepted."
            )
        if suffix == ".pt":
            self._load_pt(entry, stat, task)
        else:
            self._load_onnx(entry, stat, task)
        entry.requested_task = task

    def _load_pt(self, entry, stat, task=""):
        YOLO = self._import_ultralytics()
        if YOLO is None:
            # Class discovery is still useful without the inference runtime;
            # inspection validation will reject the unloaded model.
            names, error = self._read_pt_class_table(entry.path)
            if names is None:
                raise ModelRuntimeUnavailableError(
                    "Ultralytics is unavailable, and the .pt model class metadata "
                    f"could not be read: {error or 'unknown format'}"
                )
            entry.model = None
            entry.names = names
            entry.task = ""
        else:
            try:
                model = YOLO(entry.path, task=task) if task else YOLO(entry.path)
            except Exception as exc:
                raise ModelLoadError(
                    f"The .pt model is invalid or incompatible with Ultralytics: {exc}"
                ) from exc
            names = self._normalize_names(getattr(model, "names", None))
            entry.model = model
            entry.names = names
            entry.task = str(getattr(model, "task", "") or "")
        entry.mtime = stat.st_mtime
        entry.size = stat.st_size
        entry.loaded_at = time.time()

    def _load_onnx(self, entry, stat, task=""):
        YOLO = self._import_ultralytics()
        if YOLO is None:
            raise ModelRuntimeUnavailableError(
                "Ultralytics is unavailable because it could not be imported "
                "for ONNX model loading. "
                "Install the pinned dependencies from "
                "requirements-model-runtime.txt in the server environment; "
                "the original import traceback is in the server log."
            )
        if self._import_onnx() is None:
            raise ModelRuntimeUnavailableError(
                "ONNX Runtime is unavailable because it could not be imported. "
                "Install the pinned "
                "dependencies from requirements-model-runtime.txt in the server "
                "environment; the original import traceback is in the server log."
            )
        try:
            model = YOLO(entry.path, task=task) if task else YOLO(entry.path)
        except Exception as exc:
            raise ModelLoadError(
                f"The ONNX model is invalid or incompatible with Ultralytics/ONNX Runtime: {exc}"
            ) from exc
        names = self._normalize_names(getattr(model, "names", None))
        entry.model = model
        entry.names = names
        entry.task = str(getattr(model, "task", "") or "")
        entry.mtime = stat.st_mtime
        entry.size = stat.st_size
        entry.loaded_at = time.time()

    # ------------------------------------------------------------------ #
    #  Runtime imports (never imported at module load)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _import_ultralytics():
        try:
            from ultralytics import YOLO

            return YOLO
        except Exception:
            logger.exception("Failed to import the Ultralytics YOLO runtime")
            return None

    @staticmethod
    def _import_onnx():
        try:
            return importlib.import_module("onnxruntime")
        except Exception:
            logger.exception("Failed to import the ONNX Runtime package")
            return None

    @staticmethod
    def _normalize_names(names):
        if isinstance(names, dict):
            return {int(class_id): str(name) for class_id, name in names.items()}
        if isinstance(names, (list, tuple)):
            return {class_id: str(name) for class_id, name in enumerate(names)}
        return {}

    @staticmethod
    def _read_pt_class_table(path):
        """Read ``names`` from a ``.pt`` checkpoint without any ML runtime.

        Ultralytics checkpoints are ZIP archives.  The class table lives in the
        embedded ``model.yaml`` (``names:`` mapping) or in a ``*.yaml`` meta
        file.  Returns ``(names_dict, error)`` — ``names_dict`` is ``None`` when
        the class list is genuinely absent.
        """
        try:
            with zipfile.ZipFile(path) as archive:
                candidates = [
                    name
                    for name in archive.namelist()
                    if name.lower().endswith((".yaml", ".yml"))
                ]
                candidates.sort(key=lambda n: (0 if "model" in n.lower() else 1, len(n)))
                for name in candidates[:6]:
                    try:
                        raw = archive.read(name).decode("utf-8", "replace")
                    except Exception:  # pragma: no cover - defensive
                        continue
                    names = _parse_names_block(raw)
                    if names:
                        return names, ""
        except zipfile.BadZipFile:
            return None, "The .pt file is not a readable model archive."
        except Exception as exc:  # pragma: no cover - defensive
            return None, f"The .pt file could not be read: {exc}"
        return None, (
            "The model file does not expose a class table that can be read "
            "without the ultralytics runtime."
        )


def _parse_names_block(text):
    """Parse a ``names:`` mapping (id -> class name) out of YAML text.

    Handles the two shapes ultralytics writes::

        names:
          0: outer_circle
          1: inner_circle
        names: ['outer_circle', 'inner_circle']

    Only scalar string/int values are accepted; no YAML tags are executed.
    """
    lines = text.splitlines()
    for position, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        if key.strip() != "names":
            continue
        value = value.strip()
        if value.startswith("["):
            # Inline list form: names: ['a', 'b', ...]
            names = {}
            for item in value.strip("[]").split(","):
                item = item.strip().strip("'\"")
                if item:
                    names[len(names)] = item
            if names:
                return names
            continue
        # Block mapping: id -> name on the following indented lines.
        base_indent = len(line) - len(line.lstrip())
        names = {}
        for follow in lines[position + 1:]:
            if not follow.strip():
                continue
            indent = len(follow) - len(follow.lstrip())
            if indent <= base_indent:
                break
            item = follow.strip()
            if ":" not in item:
                continue
            raw_id, _, raw_name = item.partition(":")
            raw_name = raw_name.strip().strip("'\"")
            if raw_name.startswith("[") or raw_name.endswith("]"):
                continue
            try:
                cid = int(raw_id.strip())
            except ValueError:
                continue
            if raw_name:
                names[cid] = raw_name
        if names:
            return names
    return {}


def sha256_file(path, chunk_size=1024 * 1024):
    """Streaming SHA-256 of *path* (bounded memory)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Module-level convenience API (uses the singleton).
registry = ModelRegistry()
inspect_model = registry.inspect
predict = registry.predict
