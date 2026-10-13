"""Trigger zone service for VisionInspection.

Uses OpenCV's ``cv2.pointPolygonTest`` and polygon intersection logic to
determine whether a detected part has entered the trigger zone.
"""

import cv2
import numpy as np

from VisionInspection.models import VisionTriggerZone


def polygon_to_array(polygon: list[list[float]]) -> np.ndarray:
    """Convert a list of [x, y] points to a NumPy int32 array shaped (N, 1, 2)."""
    if not polygon:
        return np.array([], dtype=np.int32).reshape((0, 1, 2))
    pts = np.array(polygon, dtype=np.float32)
    if pts.ndim == 1:
        pts = pts.reshape((-1, 2))
    return pts.astype(np.int32).reshape((-1, 1, 2))


def is_point_inside_polygon(point, polygon) -> bool:
    """Return ``True`` if *point* is inside the *polygon*.

    Uses ``cv2.pointPolygonTest``.  A positive return means inside; zero
    means on the boundary.
    """
    if not polygon or len(polygon) < 3:
        return False

    pts = polygon_to_array(polygon)
    if pts.size == 0:
        return False

    px, py = float(point[0]), float(point[1])
    result = cv2.pointPolygonTest(pts, (px, py), False)
    return result >= 0


def is_bbox_inside_polygon(bbox, polygon) -> bool:
    """Return ``True`` if *all* corners of *bbox* are inside *polygon*.

    Parameters
    ----------
    bbox : tuple
        ``(x1, y1, x2, y2)``.
    polygon : list[list[float]]
        List of ``[x, y]`` vertex coordinates.
    """
    x1, y1, x2, y2 = bbox
    corners = [(x1, y1), (x2, y1), (x1, y2), (x2, y2)]
    return all(is_point_inside_polygon(c, polygon) for c in corners)


def is_center_inside_polygon(bbox, polygon) -> bool:
    """Return ``True`` if the *center* of *bbox* is inside *polygon*."""
    x1, y1, x2, y2 = bbox
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    return is_point_inside_polygon((cx, cy), polygon)


def get_polygon_from_trigger_zone(tz: VisionTriggerZone) -> list[list[float]]:
    """Extract the four-corner polygon from a ``VisionTriggerZone`` model."""
    return [list(tz.p1), list(tz.p2), list(tz.p3), list(tz.p4)]


def check_part_in_zone(
    bbox,
    polygon,
    check_mode: str = "center",
    containment_threshold: float = 0.5,
) -> dict:
    """Determine whether a part bounding box is inside a trigger polygon.

    Parameters
    ----------
    bbox : tuple
        ``(x1, y1, x2, y2)`` in pixel coordinates.
    polygon : list[list[float]]
        Polygon vertices as ``[[x, y], ...]`` in the **same** coordinate system
        as *bbox*.
    check_mode : str
        ``"center"`` — center point must be inside.
        ``"all"`` — all four corners must be inside.
        ``"majority"`` — at least *containment_threshold* fraction of corners
        must be inside.
    containment_threshold : float
        Fraction of corners that must be inside (0.0 – 1.0) when
        ``check_mode == "majority"``.

    Returns
    -------
    dict
        ``{"inside": bool, "corners_inside": int, "total_corners": int}``
    """
    if not polygon or len(polygon) < 3:
        return {"inside": False, "corners_inside": 0, "total_corners": 0}

    x1, y1, x2, y2 = bbox
    corners = [(x1, y1), (x2, y1), (x1, y2), (x2, y2)]
    total = len(corners)

    inside_count = 0
    for corner in corners:
        if is_point_inside_polygon(corner, polygon):
            inside_count += 1

    if check_mode == "center":
        result = is_center_inside_polygon(bbox, polygon)
    elif check_mode == "all":
        result = inside_count == total
    elif check_mode == "majority":
        required = int(np.ceil(total * containment_threshold))
        result = inside_count >= required
    else:
        result = is_center_inside_polygon(bbox, polygon)

    return {
        "inside": result,
        "corners_inside": inside_count,
        "total_corners": total,
    }


class StabilityTracker:
    """Tracks consecutive frames where a condition is true.

    Resets the counter when the condition becomes false.
    """

    def __init__(self, required_frames: int = 3):
        self._required = required_frames
        self._counter = 0
        self._stable = False

    @property
    def required(self):
        return self._required

    @required.setter
    def required(self, value: int):
        self._required = max(1, value)

    def update(self, condition: bool) -> bool:
        """Feed a boolean *condition* and return ``True`` if stable threshold met."""
        if condition:
            self._counter += 1
        else:
            self._counter = 0

        self._stable = self._counter >= self._required
        return self._stable

    def reset(self):
        self._counter = 0
        self._stable = False

    @property
    def count(self):
        return self._counter

    @property
    def is_stable(self):
        return self._stable
