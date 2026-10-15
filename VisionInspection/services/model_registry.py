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
import logging
import os
import threading
import time
import zipfile

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = (".pt", ".onnx")
MAX_MODEL_BYTES = 1024 * 1024 * 1024  # 1 GiB hard limit for uploads


class ModelLoadError(RuntimeError):
    """Raised when a model cannot be loaded or inspected."""


class _CacheEntry:
    __slots__ = (
        "path",
        "model",
        "names",
        "task",
        "mtime",
        "size",
        "loaded_at",
        "lock",
        "segmentation_model",
        "segmentation_mtime",
        "segmentation_size",
    )

    def __init__(self, path):
        self.path = path
        self.model = None
        self.names = {}
        self.task = ""
        self.mtime = 0.0
        self.size = 0
        self.loaded_at = 0.0
        self.segmentation_model = None
        self.segmentation_mtime = 0.0
        self.segmentation_size = 0
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
    def inspect(self, model_path):
        """Return the real metadata of *model_path* without running inference.

        The result is always derived from the file.  ``classes`` is a list of
        ``{"id": int, "name": str}`` in the model's own class order.
        """
        info = {
            "ok": False,
            "path": model_path,
            "classes": [],
            "num_classes": 0,
            "task": "",
            "error": "",
        }
        path = str(model_path or "").strip()
        if not path:
            info["error"] = "No model file was supplied."
            return info
        if not os.path.isfile(path):
            info["error"] = "The model file does not exist on this server."
            return info
        if not path.lower().endswith(SUPPORTED_SUFFIXES):
            info["error"] = (
                "Unsupported model format. Only .pt and .onnx models are accepted."
            )
            return info

        entry = self._entry(path)
        with entry.lock:
            self._refresh_if_stale(entry)
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
    def predict(self, model_path, frame, conf=0.5, iou=0.45, classes=None):
        """Run inference with the cached model for *model_path*.

        Returns ``(detections, error)``.  Detections carry the real class id /
        name / confidence / box of the model output; segmentation masks are
        returned as polygon point lists so the caller can draw a real overlay.
        Nothing is fabricated: an error is returned verbatim.
        """
        entry = self._entry(str(model_path or "").strip())
        with entry.lock:
            self._refresh_if_stale(entry)
            if entry.model is None:
                return [], "The model could not be loaded."
            model = entry.model
            backend = entry.task
        t0 = time.perf_counter()
        try:
            if backend == "onnx":
                detections = self._predict_onnx(model, frame, conf, iou, classes)
            else:
                detections = self._predict_ultralytics(model, frame, conf, iou, classes)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Inference failed for %s: %s", entry.path, exc)
            return [], f"Inference failed: {exc}"
        return detections, round((time.perf_counter() - t0) * 1000.0, 2)

    def predict_segmentation(
        self, model_path, frame, conf=0.50, show_masks=True, show_boxes=True
    ):
        """Run cached segmentation inference and return detections and its rendered frame."""
        path = str(model_path or "").strip()
        if not path.lower().endswith(SUPPORTED_SUFFIXES):
            raise ModelLoadError("Only .pt and .onnx segmentation models are supported.")

        entry = self._entry(path)
        with self._preview_lock, entry.lock:
            if path.lower().endswith(".onnx"):
                try:
                    stat = os.stat(entry.path)
                except OSError as exc:
                    raise ModelLoadError(f"Cannot read the model file: {exc}") from exc
                stale = (
                    entry.segmentation_model is None
                    or entry.segmentation_mtime != stat.st_mtime
                    or entry.segmentation_size != stat.st_size
                )
                if stale:
                    YOLO = self._import_ultralytics()
                    if YOLO is None:
                        raise ModelLoadError(
                            "Ultralytics is required to run ONNX segmentation preview."
                        )
                    try:
                        entry.segmentation_model = YOLO(entry.path, task="segment")
                    except Exception as exc:
                        entry.segmentation_model = None
                        raise ModelLoadError(
                            f"The ONNX segmentation model could not be loaded: {exc}"
                        ) from exc
                    entry.segmentation_mtime = stat.st_mtime
                    entry.segmentation_size = stat.st_size
                model = entry.segmentation_model
                task = str(getattr(model, "task", "") or "")
            else:
                self._refresh_if_stale(entry)
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
                    if show_masks and masks is None and boxes is not None and len(boxes):
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

    def _predict_ultralytics(self, model, frame, conf, iou, classes):
        results = model(frame, conf=conf, iou=iou, verbose=False)
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
                name = names.get(cls_id, str(cls_id))
                confidence = float(box.conf[0])
                x1, y1, x2, y2 = [int(v) for v in box.xyxy[0]]
                detection = {
                    "class": str(name),
                    "class_id": cls_id,
                    "confidence": round(confidence, 4),
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

    def _predict_onnx(self, session, frame, conf, iou, classes):
        import cv2
        import numpy as np

        input_meta = session.get_inputs()[0]
        shape = [int(d) if isinstance(d, int) else 640 for d in input_meta.shape]
        height, width = shape[2] or 640, shape[3] or 640
        blob = cv2.resize(frame, (width, height))
        blob = cv2.cvtColor(blob, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.transpose(blob, (2, 0, 1))[None, ...]
        outputs = session.run(None, {input_meta.name: blob})[0]
        detections = []
        names = getattr(session, "_vi_names", {}) or {}
        scale_x = frame.shape[1] / float(width)
        scale_y = frame.shape[0] / float(height)
        for row in outputs:
            values = np.asarray(row).ravel()
            if values.size < 6:
                continue
            scores = values[4:]
            cls_id = int(np.argmax(scores))
            confidence = float(scores[cls_id])
            if confidence < conf:
                continue
            if classes is not None and cls_id not in classes:
                continue
            x1, y1, x2, y2 = [float(v) for v in values[:4]]
            detections.append(
                {
                    "class": str(names.get(cls_id, cls_id)),
                    "class_id": cls_id,
                    "confidence": round(confidence, 4),
                    "x1": int(x1 * scale_x),
                    "y1": int(y1 * scale_y),
                    "x2": int(x2 * scale_x),
                    "y2": int(y2 * scale_y),
                }
            )
        return detections

    # ------------------------------------------------------------------ #
    #  Loading internals
    # ------------------------------------------------------------------ #
    def _entry(self, path):
        key = os.path.abspath(path)
        with self._entries_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _CacheEntry(key)
                self._entries[key] = entry
        return entry

    def _refresh_if_stale(self, entry):
        """Load / reload a model when its file changed or was never loaded."""
        try:
            stat = os.stat(entry.path)
        except OSError as exc:
            raise ModelLoadError(f"Cannot read the model file: {exc}") from exc
        if entry.model is not None and entry.mtime == stat.st_mtime and entry.size == stat.st_size:
            return
        self._load(entry, stat)

    def _load(self, entry, stat):
        path = entry.path
        if path.lower().endswith(".pt"):
            self._load_pt(entry, stat)
        else:
            self._load_onnx(entry, stat)

    def _load_pt(self, entry, stat):
        YOLO = self._import_ultralytics()
        if YOLO is not None:
            try:
                model = YOLO(entry.path)
            except Exception as exc:
                raise ModelLoadError(
                    f"The model file could not be loaded by ultralytics: {exc}"
                ) from exc
            names = getattr(model, "names", None) or {}
            entry.model = model
            entry.names = {int(k): str(v) for k, v in names.items()}
            entry.task = str(getattr(model, "task", "") or "")
        else:
            # No ultralytics: read the class table straight out of the .pt
            # archive.  A PyTorch checkpoint is a ZIP file that carries the
            # training/validation meta (including ``names``) — reading it needs
            # no inference runtime and no extra dependency.
            names, error = self._read_pt_class_table(entry.path)
            if names is None:
                raise ModelLoadError(error or "The model class list could not be read.")
            entry.model = None
            entry.names = names
            entry.task = ""
        entry.mtime = stat.st_mtime
        entry.size = stat.st_size
        entry.loaded_at = time.time()

    def _load_onnx(self, entry, stat):
        ort = self._import_onnx()
        if ort is None:
            raise ModelLoadError(
                "onnxruntime is not installed; .onnx models cannot be loaded on "
                "this host."
            )
        try:
            session = ort.InferenceSession(entry.path, providers=["CPUExecutionProvider"])
        except Exception as exc:
            raise ModelLoadError(f"The ONNX model could not be loaded: {exc}") from exc
        entry.model = session
        entry.names = {}
        entry.task = "onnx"
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
            return None

    @staticmethod
    def _import_onnx():
        try:
            import onnxruntime

            return onnxruntime
        except Exception:
            return None

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
