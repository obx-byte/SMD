
from pathlib import Path
import threading
import time

import cv2
import numpy as np
from picamera2 import Picamera2
from ultralytics import YOLO


# =========================================================
# CONFIGURATION
# =========================================================

MODEL_PATH = Path("/home/pi/models/best.onnx")

OUTPUT_DIR = (
    Path(__file__).resolve().parent
    / "runs"
    / "live_stitch"
)

CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720

# Start here; test 640 if small stitches are missed.
INFER_SIZE = 512
CONF_THRESHOLD = 0.25
BOX_IOU_THRESHOLD = 0.50
MASK_ALPHA = 0.30

UNWANTED_CLASSES = {"cushion"}

# Optional ROI, defined for a 1920 x 1080 reference image.
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

WINDOW_NAME = "Pi 4 - Live Stitch Segmentation"


# =========================================================
# LOAD ONNX MODEL
# =========================================================

if not MODEL_PATH.is_file():
    raise FileNotFoundError(
        f"ONNX model not found: {MODEL_PATH}"
    )

model = YOLO(str(MODEL_PATH), task="segment")

print("Model:", MODEL_PATH)
print("Classes:", model.names)
print("Inference device: CPU")


# =========================================================
# CAMERA
# =========================================================

picam2 = Picamera2()

camera_config = picam2.create_video_configuration(
    main={
        "size": (CAMERA_WIDTH, CAMERA_HEIGHT),
        "format": "RGB888",
    },
    buffer_count=4,
)

picam2.configure(camera_config)
picam2.start()
time.sleep(1.0)


# =========================================================
# SHARED STATE
# =========================================================

lock = threading.Lock()
stop_event = threading.Event()

latest_frame = None
latest_frame_id = 0

latest_output = None
latest_output_id = -1
latest_inference_ms = 0.0
latest_mask_count = 0
latest_result_time = 0.0


# =========================================================
# CAMERA CAPTURE THREAD
# =========================================================

def capture_worker():
    global latest_frame, latest_frame_id

    while not stop_event.is_set():
        try:
            frame = picam2.capture_array()

            if frame is None:
                continue

            # Picamera2 RGB888 output is commonly used directly
            # with OpenCV on Raspberry Pi.
            with lock:
                latest_frame = frame
                latest_frame_id += 1

        except Exception as exc:
            if not stop_event.is_set():
                print("Camera capture error:", exc)
            time.sleep(0.05)


# =========================================================
# ROI HELPERS
# =========================================================

def get_roi_polygon(width, height):
    sx = width / ROI_REFERENCE_WIDTH
    sy = height / ROI_REFERENCE_HEIGHT

    return np.round(
        ROI_POLYGON.astype(np.float32)
        * np.array([sx, sy], dtype=np.float32)
    ).astype(np.int32)


# =========================================================
# INFERENCE THREAD
# =========================================================

def inference_worker():
    global latest_output, latest_output_id
    global latest_inference_ms, latest_mask_count
    global latest_result_time

    last_processed_id = -1

    while not stop_event.is_set():

        with lock:
            if latest_frame is None:
                frame = None
                frame_id = -1
            else:
                frame = latest_frame.copy()
                frame_id = latest_frame_id

        if frame is None or frame_id == last_processed_id:
            stop_event.wait(0.003)
            continue

        # Always process the newest frame available.
        # Intermediate frames are intentionally skipped if
        # inference is slower than camera capture.
        last_processed_id = frame_id

        height, width = frame.shape[:2]
        roi_polygon = get_roi_polygon(width, height)

        start = time.perf_counter()

        try:
            with np.errstate(all="ignore"):
                results = model.predict(
                    source=frame,
                    imgsz=INFER_SIZE,
                    conf=CONF_THRESHOLD,
                    iou=BOX_IOU_THRESHOLD,
                    retina_masks=False,
                    device="cpu",
                    verbose=False,
                )

            elapsed_ms = (
                time.perf_counter() - start
            ) * 1000.0

            # Draw on the exact frame used for inference.
            overlay = frame.copy()
            output = frame.copy()
            mask_count = 0

            for result in results:

                if result.masks is None or result.boxes is None:
                    continue

                # With retina_masks=False, masks may be in
                # model-input coordinates. Map them to frame size.
                masks = result.masks.data.cpu().numpy()

                for index, box in enumerate(result.boxes):

                    class_id = int(box.cls.item())
                    confidence = float(box.conf.item())
                    class_name = model.names[class_id]

                    if class_name in UNWANTED_CLASSES:
                        continue

                    mask = cv2.resize(
                        masks[index].astype(np.float32),
                        (width, height),
                        interpolation=cv2.INTER_LINEAR,
                    )

                    binary_mask = mask > 0.5

                    if USE_ROI:
                        roi_mask = np.zeros(
                            (height, width),
                            dtype=np.uint8,
                        )
                        cv2.fillPoly(
                            roi_mask,
                            [roi_polygon],
                            1,
                        )
                        binary_mask &= roi_mask != 0

                    if not np.any(binary_mask):
                        continue

                    # Stable class color.
                    rng = np.random.default_rng(class_id + 123)
                    color = tuple(
                        int(v)
                        for v in rng.integers(60, 256, size=3)
                    )

                    overlay[binary_mask] = color
                    mask_count += 1

                    contours, _ = cv2.findContours(
                        binary_mask.astype(np.uint8),
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

                    moments = cv2.moments(
                        binary_mask.astype(np.uint8)
                    )

                    if moments["m00"] > 0:
                        cx = int(
                            moments["m10"] / moments["m00"]
                        )
                        cy = int(
                            moments["m01"] / moments["m00"]
                        )

                        cv2.putText(
                            output,
                            f"{class_name} {confidence:.2f}",
                            (cx, max(20, cy)),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.5,
                            color,
                            1,
                            cv2.LINE_AA,
                        )

            # Blend only once after all masks are drawn.
            output = cv2.addWeighted(
                output,
                1.0 - MASK_ALPHA,
                overlay,
                MASK_ALPHA,
                0,
            )

            if USE_ROI:
                cv2.polylines(
                    output,
                    [roi_polygon],
                    True,
                    (255, 255, 0),
                    2,
                )

            with lock:
                latest_output = output
                latest_output_id = frame_id
                latest_inference_ms = elapsed_ms
                latest_mask_count = mask_count
                latest_result_time = time.monotonic()

        except Exception as exc:
            print("Inference error:", exc)
            stop_event.wait(0.1)


# =========================================================
# MAIN DISPLAY LOOP
# =========================================================

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    capture_thread = threading.Thread(
        target=capture_worker,
        daemon=True,
    )
    infer_thread = threading.Thread(
        target=inference_worker,
        daemon=True,
    )

    capture_thread.start()
    infer_thread.start()

    print("Q / ESC: quit")
    print("S: save the latest processed image")
    print("P: toggle ROI")

    roi_enabled = USE_ROI

    try:
        while not stop_event.is_set():

            with lock:
                output = (
                    latest_output.copy()
                    if latest_output is not None
                    else None
                )

                infer_ms = latest_inference_ms
                mask_count = latest_mask_count
                result_time = latest_result_time

            if output is None:
                time.sleep(0.01)
                continue

            age_ms = (
                (time.monotonic() - result_time) * 1000.0
                if result_time > 0
                else 0.0
            )

            cv2.putText(
                output,
                f"Inference: {infer_ms:.0f} ms",
                (15, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (0, 255, 0),
                2,
            )

            cv2.putText(
                output,
                f"Masks: {mask_count} | Result age: {age_ms:.0f} ms",
                (15, 60),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                (255, 255, 255),
                2,
            )

            if age_ms > 500:
                cv2.putText(
                    output,
                    "STALE INFERENCE",
                    (15, 95),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 0, 255),
                    2,
                )

            cv2.imshow(WINDOW_NAME, output)

            key = cv2.waitKey(1) & 0xFF

            if key in (ord("q"), 27):
                break

            elif key == ord("s"):
                filename = (
                    OUTPUT_DIR
                    / f"stitch_{time.strftime('%Y%m%d_%H%M%S')}.png"
                )

                if cv2.imwrite(str(filename), output):
                    print("Saved:", filename)

            elif key == ord("p"):
                roi_enabled = not roi_enabled
                # Update the shared setting safely by stopping
                # and restarting if you change ROI at runtime.
                print(
                    "ROI toggle requested:",
                    roi_enabled,
                    "(edit USE_ROI before launch to apply it)"
                )

    finally:
        stop_event.set()
        capture_thread.join(timeout=2)
        infer_thread.join(timeout=2)

        picam2.stop()
        picam2.close()
        cv2.destroyAllWindows()
        print("Camera and application stopped.")


if __name__ == "__main__":
    main()
