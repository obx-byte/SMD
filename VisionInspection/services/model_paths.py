"""Shared path resolution for registered YOLO model files."""

import os
from pathlib import Path, PureWindowsPath

from django.conf import settings


class ModelPathResolutionError(ValueError):
    """Raised when a model path is invalid or escapes model storage."""


def model_storage_root():
    """Return the configured absolute root for model files."""
    configured = getattr(
        settings,
        "VISION_MODEL_OUTPUT_DIR",
        Path(settings.BASE_DIR) / "data" / "models",
    )
    return Path(configured).expanduser().resolve()


def resolve_model_path(model_path):
    """Resolve a model reference consistently for registration and inference.

    Relative references are rooted at ``VISION_MODEL_OUTPUT_DIR``. An absolute
    reference from another computer can be recovered only when it ends in the
    configured storage directory suffix and its filename exists in this root.
    """
    configured_path = str(model_path or "").strip()
    if not configured_path:
        raise ModelPathResolutionError("No model file was supplied.")

    root = model_storage_root()
    path = Path(configured_path).expanduser()
    windows_path = PureWindowsPath(configured_path)
    is_absolute = path.is_absolute() or windows_path.is_absolute()

    if is_absolute:
        if path.is_file():
            return path.resolve()

        stored_parts = windows_path.parts if windows_path.is_absolute() else path.parts
        root_parts = root.parts
        suffix_length = min(2, len(root_parts))
        storage_suffix = tuple(part.casefold() for part in root_parts[-suffix_length:])
        stored_directories = tuple(
            part.casefold() for part in stored_parts[-suffix_length - 1 : -1]
        )
        if stored_directories == storage_suffix:
            filename = windows_path.name if windows_path.is_absolute() else path.name
            relocated = root / filename
            if relocated.is_file():
                return relocated.resolve()
        return path.resolve()

    relative = Path(configured_path.replace("\\", os.sep))
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ModelPathResolutionError(
            f"Relative model path escapes the configured model-storage root: {configured_path}"
        ) from exc
    return resolved