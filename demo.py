
from pathlib import Path
import time

import cv2
import numpy as np
from ultralytics import YOLO


# =========================================================
# CONFIGURATION
# =========================================================

MODEL_PATH = Path(r"C:\Users\LGBIT\Downloads\best (1).pt")

OUTPUT_DIR = (
    Path(__file__).resolve().parent
    / "runs"
    / "stitch_mask_iou"
)

CAMERA_INDEX = 0
CAMERA_WIDTH = 1920
CAMERA_HEIGHT = 1080

SLICE_HEIGHT = 700
SLICE_WIDTH = 960
OVERLAP = 0.20

CONF_THRESHOLD = 0.25
BOX_IOU_THRESHOLD = 0.50
MASK_IOU_THRESHOLD = 0.50

UNWANTED_CLASSES = {"cushion"}

# ROI coordinates are defined relative to a 1920 x 1080 frame.
USE_ROI = False
ROI_REFERENCE_WIDTH = 1920
ROI_REFERENCE_HEIGHT = 1080

ROI_POLYGON = np.array(
    [
        [400, 200],
        [1500, 200],
        [1500, 950],
        [350, 950],
    ],
    dtype=np.int32,
)

# Display controls
WINDOW_NAME = "Live Airbag Stitch Segmentation"
MASK_ALPHA = 0.35


# =========================================================
# LOAD MODEL
# =========================================================

if not MODEL_PATH.is_file():
    raise FileNotFoundError(
        f"Model not found: {MODEL_PATH}"
    )

model = YOLO(str(MODEL_PATH))

print("Model loaded:", MODEL_PATH)
print("Model classes:", model.names)


# =========================================================
# CAMERA INITIALIZATION
# =========================================================

# CAP_DSHOW is suitable for many Windows webcams.
# Use cv2.VideoCapture(CAMERA_INDEX) if necessary.
camera = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)

if not camera.isOpened():
    camera.release()
    camera = cv2.VideoCapture(CAMERA_INDEX)

if not camera.isOpened():
    raise RuntimeError(
        "Cannot open camera. Check CAMERA_INDEX, "
        "camera permissions, and whether another app "
        "is using the camera."
    )

camera.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
camera.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)

# Reduce camera buffering where supported.
camera.set(cv2.CAP_PROP_BUFFERSIZE, 1)

print("Camera opened successfully.")
print("Press Q or ESC to quit.")
print("Press S to save the processed frame.")
print("Press P to toggle ROI display/edit mode.")


# =========================================================
# SLICE POSITIONS
# =========================================================

def get_positions(length, slice_size, overlap):
    if length <= slice_size:
        return [0]

    stride = max(
        1,
        int(slice_size * (1.0 - overlap))
    )

    positions = list(
        range(0, length - slice_size + 1, stride)
    )

    last = length - slice_size

    if positions[-1] != last:
        positions.append(last)

    return positions


# =========================================================
# MASK IoU
# =========================================================

def mask_iou(mask_a, mask_b):
    intersection = np.count_nonzero(
        (mask_a != 0) & (mask_b != 0)
    )

    if intersection == 0:
        return 0.0

    union = np.count_nonzero(
        (mask_a != 0) | (mask_b != 0)
    )

    return intersection / union if union else 0.0


# =========================================================
# CLASS COLORS
# =========================================================

def class_color(class_id):
    rng = np.random.default_rng(class_id + 123)
    color = rng.integers(60, 256, size=3)

    return tuple(int(value) for value in color)


# =========================================================
# PROCESS ONE LIVE FRAME
# =========================================================

def process_frame(image):
    height, width = image.shape[:2]

    # Scale the configured ROI to the actual camera frame.
    sx = width / ROI_REFERENCE_WIDTH
    sy = height / ROI_REFERENCE_HEIGHT

    scaled_polygon = np.round(
        ROI_POLYGON.astype(np.float32)
        * np.array([sx, sy], dtype=np.float32)
    ).astype(np.int32)

    roi_mask = np.ones(
        (height, width),
        dtype=np.uint8,
    )

    if USE_ROI:
        roi_mask[:] = 0
        cv2.fillPoly(
            roi_mask,
            [scaled_polygon],
            1,
        )

    x_positions = get_positions(
        width, SLICE_WIDTH, OVERLAP
    )
    y_positions = get_positions(
        height, SLICE_HEIGHT, OVERLAP
    )

    detections = []

    # -----------------------------------------------------
    # TILE-BASED SEGMENTATION
    # -----------------------------------------------------

    for y in y_positions:
        for x in x_positions:

            x2 = min(x + SLICE_WIDTH, width)
            y2 = min(y + SLICE_HEIGHT, height)

            crop = image[y:y2, x:x2]
            crop_h, crop_w = crop.shape[:2]

            if USE_ROI:
                local_roi = roi_mask[y:y2, x:x2]

                if not np.any(local_roi):
                    continue

            results = model.predict(
                source=crop,
                imgsz=640,
                conf=CONF_THRESHOLD,
                iou=BOX_IOU_THRESHOLD,
                retina_masks=True,
                verbose=False,
            )

            for result in results:
                if (
                    result.boxes is None
                    or result.masks is None
                ):
                    continue

                masks = result.masks.data.cpu().numpy()

                for index, box in enumerate(result.boxes):
                    class_id = int(box.cls.item())
                    confidence = float(box.conf.item())

                    class_name = model.names[class_id]

                    if class_name in UNWANTED_CLASSES:
                        continue

                    mask = masks[index]

                    if mask.shape != (crop_h, crop_w):
                        mask = cv2.resize(
                            mask.astype(np.float32),
                            (crop_w, crop_h),
                            interpolation=cv2.INTER_NEAREST,
                        )

                    local_mask = (
                        mask > 0.5
                    ).astype(np.uint8)

                    if not np.any(local_mask):
                        continue

                    # Keep only pixels inside the configured ROI.
                    if USE_ROI:
                        local_mask *= roi_mask[y:y2, x:x2]

                    if not np.any(local_mask):
                        continue

                    # Map the tile mask to full-frame coordinates.
                    full_mask = np.zeros(
                        (height, width),
                        dtype=np.uint8,
                    )

                    full_mask[y:y2, x:x2] = local_mask

                    detections.append({
                        "class_id": class_id,
                        "confidence": confidence,
                        "mask": full_mask,
                    })

    # -----------------------------------------------------
    # MASK-BASED NON-MAXIMUM SUPPRESSION
    # -----------------------------------------------------

    detections.sort(
        key=lambda item: item["confidence"],
        reverse=True,
    )

    kept = []

    for candidate in detections:
        duplicate = False

        for accepted in kept:

            # Do not suppress masks from different classes.
            if (
                candidate["class_id"]
                != accepted["class_id"]
            ):
                continue

            overlap = mask_iou(
                candidate["mask"],
                accepted["mask"],
            )

            if overlap >= MASK_IOU_THRESHOLD:
                duplicate = True
                break

        if not duplicate:
            kept.append(candidate)

    # -----------------------------------------------------
    # DRAW SEGMENTATION ONLY - NO BOUNDING BOXES
    # -----------------------------------------------------

    overlay = image.copy()
    output = image.copy()

    for item in kept:
        mask = item["mask"]
        class_id = item["class_id"]
        confidence = item["confidence"]

        color = class_color(class_id)

        overlay[mask != 0] = color

    output = cv2.addWeighted(
        image,
        1.0 - MASK_ALPHA,
        overlay,
        MASK_ALPHA,
        0,
    )

    for item in kept:
        mask = item["mask"]
        class_id = item["class_id"]
        confidence = item["confidence"]

        class_name = model.names[class_id]
        color = class_color(class_id)

        contours, _ = cv2.findContours(
            mask,
            cv2.RETR_EXTERNAL,
            cv2.CHAIN_APPROX_SIMPLE,
        )

        cv2.drawContours(
            output,
            contours,
            -1,
            color,
            2,
        )

        # Find the mask centroid for the label.
        moments = cv2.moments(mask)

        if moments["m00"] > 0:
            cx = int(moments["m10"] / moments["m00"])
            cy = int(moments["m01"] / moments["m00"])

            label = f"{class_name} {confidence:.2f}"

            cv2.putText(
                output,
                label,
                (cx, max(20, cy)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                color,
                2,
                cv2.LINE_AA,
            )

    # Show ROI boundary when enabled.
    if USE_ROI:
        cv2.polylines(
            output,
            [scaled_polygon],
            True,
            (255, 255, 0),
            2,
        )

    return output, len(detections), len(kept)


# =========================================================
# LIVE CAMERA LOOP
# =========================================================

OUTPUT_DIR.mkdir(
    parents=True,
    exist_ok=True,
)

last_output = None
last_detection_count = 0
last_kept_count = 0

frame_count = 0
fps_start = time.perf_counter()
display_fps = 0.0

try:
    while True:
        success, frame = camera.read()

        if not success:
            print("Failed to read camera frame.")
            break

        frame_count += 1

        processed, raw_count, kept_count = process_frame(
            frame
        )

        last_output = processed
        last_detection_count = raw_count
        last_kept_count = kept_count

        elapsed = time.perf_counter() - fps_start

        if elapsed >= 1.0:
            display_fps = frame_count / elapsed
            frame_count = 0
            fps_start = time.perf_counter()

        # Live status overlay.
        cv2.putText(
            processed,
            f"FPS: {display_fps:.1f}",
            (20, 35),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )

        cv2.putText(
            processed,
            f"Raw masks: {raw_count} | Final masks: {kept_count}",
            (20, 70),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

        cv2.imshow(WINDOW_NAME, processed)

        key = cv2.waitKey(1) & 0xFF

        if key in (ord("q"), 27):
            break

        elif key == ord("s"):
            timestamp = time.strftime("%Y%m%d_%H%M%S")

            output_path = (
                OUTPUT_DIR
                / f"live_segmentation_{timestamp}.png"
            )

            if cv2.imwrite(
                str(output_path),
                processed,
            ):
                print("Saved:", output_path)
            else:
                print("Failed to save:", output_path)

        elif key == ord("p"):
            USE_ROI = not USE_ROI
            print("ROI enabled:", USE_ROI)

finally:
    camera.release()
    cv2.destroyAllWindows()
    print("Camera released. Application stopped.")
