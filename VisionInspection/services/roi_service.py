"""ROI service for VisionInspection.

Handles coordinate transformation of master ROIs to match the actual detected
part, ROI extraction from frames, and validation of ROI detection results.
"""

import logging

import cv2
import numpy as np

logger = logging.getLogger(__name__)


def normalize_point(point, ref_width, ref_height):
    """Normalise a pixel ``[x, y]`` to ``[0, 1]`` range using reference dimensions."""
    if ref_width <= 0 or ref_height <= 0:
        return [0.0, 0.0]
    return [float(point[0]) / ref_width, float(point[1]) / ref_height]


def denormalize_point(norm_point, target_width, target_height):
    """Convert a normalised ``[0, 1]`` point back to pixel coordinates."""
    return [float(norm_point[0]) * target_width, float(norm_point[1]) * target_height]


def denormalize_polygon(norm_polygon, target_width, target_height):
    """Convert a list of normalised points to pixel coordinates."""
    return [denormalize_point(p, target_width, target_height) for p in norm_polygon]


def get_perspective_transform_matrix(src_points, dst_points):
    """Compute a perspective transform matrix mapping *src_points* → *dst_points*.

    Both arguments must be lists of ``[x, y]`` with at least 4 points.
    Returns a 3×3 transform matrix, or ``None`` on failure.
    """
    if len(src_points) < 4 or len(dst_points) < 4:
        return None

    src = np.array(src_points[:4], dtype=np.float32)
    dst = np.array(dst_points[:4], dtype=np.float32)

    if src.shape != (4, 2) or dst.shape != (4, 2):
        return None

    try:
        matrix = cv2.getPerspectiveTransform(src, dst)
        return matrix
    except cv2.error:
        # Fallback to affine transform (3 points)
        try:
            matrix = cv2.getAffineTransform(src[:3], dst[:3])
            return matrix
        except cv2.error:
            logger.error("Failed to compute perspective transform")
            return None


def transform_point(point, matrix):
    """Apply a perspective/affine transform matrix to a single ``[x, y]`` point."""
    if matrix is None:
        return list(point)
    pt = np.array([point[0], point[1]], dtype=np.float32).reshape(-1, 1, 2)
    try:
        transformed = cv2.perspectiveTransform(pt, matrix)
        return [float(transformed[0, 0, 0]), float(transformed[0, 0, 1])]
    except cv2.error:
        return list(point)


def transform_polygon(polygon, matrix):
    """Apply a transform matrix to a list of ``[x, y]`` points."""
    return [transform_point(p, matrix) for p in polygon]


def compute_affine_transform(master_bounds, actual_bounds):
    """Compute a similarity (translation + scale) transform.

    Parameters
    ----------
    master_bounds : tuple
        ``(x1, y1, x2, y2)`` — bounding box of the master part in master image.
    actual_bounds : tuple
        ``(x1, y1, x2, y2)`` — bounding box of the actual detected part in current frame.

    Returns
    -------
    tuple
        ``(scale_x, scale_y, offset_x, offset_y)``
    """
    mx1, my1, mx2, my2 = master_bounds
    ax1, ay1, ax2, ay2 = actual_bounds

    m_w = mx2 - mx1
    m_h = my2 - my1
    a_w = ax2 - ax1
    a_h = ay2 - ay1

    scale_x = a_w / m_w if m_w != 0 else 1.0
    scale_y = a_h / m_h if m_h != 0 else 1.0

    offset_x = ax1 - mx1 * scale_x
    offset_y = ay1 - my1 * scale_y

    return scale_x, scale_y, offset_x, offset_y


def apply_similarity_transform(polygon, scale_x, scale_y, offset_x, offset_y):
    """Apply a translation + scale transform to a polygon."""
    return [
        [p[0] * scale_x + offset_x, p[1] * scale_y + offset_y]
        for p in polygon
    ]


def transform_rois_to_actual(
    rois,
    master_part_bbox,
    actual_part_bbox,
    master_width=None,
    master_height=None,
    use_perspective: bool = True,
):
    """Transform a list of master ROIs to match the actual detected part.

    This implements both similarity (translation + scale) and perspective
    transforms.  The transform is computed from the outer part bounding box
    correspondence.

    Parameters
    ----------
    rois : list
        Each element is a dict with ``polygon`` (list of ``[x, y]`` normalized
        coords) and ``ref_width`` / ``ref_height``.
    master_part_bbox : tuple
        ``(x1, y1, x2, y2)`` of the outer part bounding box in master-image
        coordinates (pixel).
    actual_part_bbox : tuple
        ``(x1, y1, x2, y2)`` of the detected part bounding box in current-frame
        coordinates (pixel).
    master_width, master_height : int
        Dimensions of the master image (for normalization).
    use_perspective : bool
        If ``True``, attempt a perspective transform using the four corners of
        the outer part bbox.  Falls back to similarity if not possible.

    Returns
    -------
    list[dict]
        Transformed ROIs, each with ``pixel_polygon`` in actual-frame coordinates.
    """
    transformed = []

    mx1, my1, mx2, my2 = master_part_bbox
    ax1, ay1, ax2, ay2 = actual_part_bbox

    master_w = master_width or (mx2 - mx1)
    master_h = master_height or (my2 - my1)

    if use_perspective:
        # Build 4-corner correspondence
        master_corners = [[mx1, my1], [mx2, my1], [mx2, my2], [mx1, my2]]
        actual_corners = [[ax1, ay1], [ax2, ay1], [ax2, ay2], [ax1, ay2]]
        matrix = get_perspective_transform_matrix(master_corners, actual_corners)
    else:
        matrix = None

    sx, sy, ox, oy = compute_affine_transform(master_part_bbox, actual_part_bbox)

    for roi in rois:
        if not roi.get("is_active", True):
            transformed.append({**roi, "pixel_polygon": None, "transformed": False})
            continue

        polygon = roi["polygon"]
        # Denormalize to pixels using master image dimensions
        if max(max(p) for p in polygon) <= 1.0:
            pixel_polygon = denormalize_polygon(polygon, master_w, master_h)
        else:
            pixel_polygon = list(polygon)

        # Apply transform
        if matrix is not None:
            pixel_polygon = transform_polygon(pixel_polygon, matrix)
        else:
            pixel_polygon = apply_similarity_transform(pixel_polygon, sx, sy, ox, oy)

        transformed.append({**roi, "pixel_polygon": pixel_polygon, "transformed": True})

    return transformed


def extract_roi_region(frame, polygon):
    """Extract the region of *frame* defined by *polygon* as a cropped image.

    Also returns a binary mask for overlap-based filtering.

    Parameters
    ----------
    frame : numpy.ndarray
        BGR image.
    polygon : list[list[float]]
        List of ``[x, y]`` vertex coordinates (pixel space).

    Returns
    -------
    tuple
        ``(cropped, mask)`` where *cropped* is the sub-image and *mask* is the
        binary mask.  Returns ``(None, None)`` on failure.
    """
    if frame is None or not polygon or len(polygon) < 3:
        return None, None

    try:
        h, w = frame.shape[:2]
        pts = np.array(polygon, dtype=np.int32).reshape((-1, 1, 2))

        x, y, pw, ph = cv2.boundingRect(pts)
        x = max(0, x)
        y = max(0, y)
        pw = min(pw, w - x)
        ph = min(ph, h - y)

        if pw <= 0 or ph <= 0:
            return None, None

        # Shift polygon to be relative to the bounding rect
        shifted_pts = pts - np.array([x, y], dtype=np.int32)
        mask = np.zeros((ph, pw), dtype=np.uint8)
        cv2.fillPoly(mask, [shifted_pts], 255)

        cropped = frame[y : y + ph, x : x + pw].copy()
        return cropped, mask

    except Exception as exc:
        logger.error("ROI extraction error: %s", exc)
        return None, None


def filter_detections_in_roi(detections, polygon, frame_shape=None):
    """Return only those detections whose center lies within *polygon*.

    Parameters
    ----------
    detections : list[dict]
        Each dict has ``x1, y1, x2, y2`` keys.
    polygon : list[list[float]]
        Polygon in pixel coordinates.

    Returns
    -------
    list[dict]
        Filtered detections.
    """
    if not polygon or len(polygon) < 3:
        return detections

    from VisionInspection.services.trigger_service import is_point_inside_polygon

    pts = np.array(polygon, dtype=np.int32).reshape((-1, 1, 2))
    filtered = []

    for det in detections:
        cx = (det["x1"] + det["x2"]) / 2
        cy = (det["y1"] + det["y2"]) / 2
        if is_point_inside_polygon((cx, cy), polygon):
            filtered.append(det)

    return filtered


def count_detections_by_class(detections, class_name=None):
    """Count detections, optionally filtered by *class_name*.

    Returns ``(count, {class_name: count})``.
    """
    if class_name:
        count = sum(1 for d in detections if d["class"] == class_name)
    else:
        count = len(detections)

    breakdown = {}
    for d in detections:
        cls = d["class"]
        breakdown[cls] = breakdown.get(cls, 0) + 1

    return count, breakdown


def validate_roi(roi_config, detections):
    """Validate a single ROI's detections against its configuration.

    Parameters
    ----------
    roi_config : dict
        Contains ``expected_class``, ``min_count``, ``max_count``,
        ``confidence_threshold``, ``inspection_rule``.
    detections : list[dict]
        Detection results within the ROI (already filtered by the ROI polygon).

    Returns
    -------
    dict
        ``{"result": "MATCH"|"NOT_MATCH"|"ERROR"|"SKIPPED", "details": str}``
    """
    expected_class = roi_config.get("expected_class", "")
    min_count = roi_config.get("min_count", 0)
    max_count = roi_config.get("max_count", 0)
    conf_thresh = roi_config.get("confidence_threshold", 0.5)
    rule = roi_config.get("inspection_rule", "count")
    is_active = roi_config.get("is_active", True)

    if not is_active:
        return {"result": "SKIPPED", "details": "ROI is disabled"}

    if not detections:
        if rule == "count":
            if min_count == 0 and max_count == 0:
                return {"result": "MATCH", "details": "Expected 0, detected 0"}
            if min_count > 0:
                return {
                    "result": "NOT_MATCH",
                    "details": f"Expected {expected_class} x{min_count}-{max_count}, detected 0",
                }
        return {"result": "NOT_MATCH", "details": f"No {expected_class} detected"}

    # Filter by expected class
    class_detections = [d for d in detections if d["class"] == expected_class]

    # Apply confidence threshold
    class_detections = [d for d in class_detections if d["confidence"] >= conf_thresh]

    count, breakdown = count_detections_by_class(class_detections, expected_class)

    if rule == "count":
        if min_count <= count <= max_count:
            return {
                "result": "MATCH",
                "details": f"Expected {expected_class} x[{min_count}-{max_count}], detected {count}",
                "breakdown": breakdown,
            }
        else:
            return {
                "result": "NOT_MATCH",
                "details": f"Expected {expected_class} x[{min_count}-{max_count}], detected {count}",
                "breakdown": breakdown,
            }
    elif rule == "presence":
        if count >= 1:
            return {"result": "MATCH", "details": f"At least 1 {expected_class} present"}
        else:
            return {"result": "NOT_MATCH", "details": f"No {expected_class} detected"}
    elif rule == "absence":
        if count == 0:
            return {"result": "MATCH", "details": f"No {expected_class} detected (expected absence)"}
        else:
            return {"result": "NOT_MATCH", "details": f"{count} {expected_class} detected (expected absence)"}

    return {"result": "ERROR", "details": f"Unknown inspection rule: {rule}"}
