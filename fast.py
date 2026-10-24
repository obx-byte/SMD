
# ============================================================
# Raspberry Pi 4 + Camera Module 3 + FastAPI + YOLO Segmentation
# Optimized for low latency at imgsz=320
# ============================================================

import os

# Configure CPU threads before importing PyTorch / NumPy.
CPU_THREADS = 4

os.environ["OMP_NUM_THREADS"] = str(CPU_THREADS)
os.environ["OPENBLAS_NUM_THREADS"] = str(CPU_THREADS)
os.environ["NUMEXPR_NUM_THREADS"] = str(CPU_THREADS)

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from time import perf_counter

import cv2
import numpy as np
import torch
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from ultralytics import YOLO


# ============================================================
# CONFIGURATION
# ============================================================

# Change this to your actual model path on the Raspberry Pi.
# You can use either the .pt model or an exported NCNN model folder.
MODEL_PATH = r"C:\Users\v.vinoth\Downloads\train-3.pt"

IMAGE_SIZE = 640
CONFIDENCE = 0.30
IOU_THRESHOLD = 0.45
MAX_DETECTIONS = 30

# Preserve camera detail; YOLO input size remains 320.
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480

JPEG_QUALITY = 75
THREAD_JOIN_TIMEOUT = 5


# ============================================================
# LOGGING AND CPU CONFIGURATION
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger("yolo-api")

try:
    torch.set_num_threads(CPU_THREADS)
except RuntimeError:
    pass

try:
    torch.set_num_interop_threads(1)
except RuntimeError:
    pass

# OpenCV can otherwise use additional CPU threads.
cv2.setNumThreads(1)


# ============================================================
# SHARED STATE
# ============================================================

app = None

model = None
picam2 = None

capture_thread = None
inference_thread = None

stop_event = threading.Event()
frame_available = threading.Event()

state_lock = threading.Lock()
inference_lock = threading.Lock()

latest_frame = None
latest_frame_id = 0

latest_jpeg = None
latest_jpeg_id = 0

latest_stats = {
    "inference_time_ms": 0.0,
    "processing_time_ms": 0.0,
    "processed_fps": 0.0,
    "detections": 0,
    "last_error": None
}


# ============================================================
# YOLO INFERENCE
# ============================================================

def run_prediction(frame):
    """
    Runs one prediction using the shared model.
    The lock prevents concurrent access to the same model.
    """

    start = perf_counter()

    with inference_lock:
        results = model.predict(
            source=frame,
            imgsz=IMAGE_SIZE,
            conf=CONFIDENCE,
            iou=IOU_THRESHOLD,
            max_det=MAX_DETECTIONS,
            retina_masks=False,
            verbose=False
        )

    inference_ms = (perf_counter() - start) * 1000

    return results[0], inference_ms


def build_detections(result):
    detections = []

    if result.boxes is None:
        return detections

    names = model.names

    for index, box in enumerate(result.boxes):
        class_id = int(box.cls.item())

        if isinstance(names, dict):
            class_name = names.get(class_id, str(class_id))
        else:
            class_name = names[class_id]

        x1, y1, x2, y2 = map(
            int,
            box.xyxy[0].tolist()
        )

        has_mask = (
            result.masks is not None
            and index < len(result.masks.data)
        )

        detections.append({
            "class_id": class_id,
            "class_name": class_name,
            "confidence": round(float(box.conf.item()), 4),
            "box": [x1, y1, x2, y2],
            "has_mask": has_mask
        })

    return detections


# ============================================================
# CAMERA CAPTURE THREAD
# ============================================================

def camera_capture_worker():
    """
    Continuously captures frames.
    Overwrites old frames rather than building a queue.
    """

    global latest_frame, latest_frame_id

    logger.info("Camera capture worker started")

    while not stop_event.is_set():
        try:
            camera = picam2

            if camera is None:
                break

            # Picamera2 RGB888 arrays are BGR-ordered for OpenCV.
            # No extra cv2.cvtColor copy is required.
            frame = camera.capture_array()

            with state_lock:
                latest_frame = frame
                latest_frame_id += 1
                frame_available.set()

        except Exception as exc:
            if stop_event.is_set():
                break

            logger.warning("Camera capture error: %s", exc)
            time.sleep(0.1)

    logger.info("Camera capture worker stopped")


# ============================================================
# INFERENCE WORKER
# ============================================================

def inference_worker():
    """
    Reads only the latest available frame.
    Performs one inference at a time and publishes one JPEG result.
    """

    global latest_jpeg
    global latest_jpeg_id
    global latest_stats

    last_processed_id = 0

    logger.info("Inference worker started")

    while not stop_event.is_set():

        frame_available.wait(timeout=1.0)

        if stop_event.is_set():
            break

        # Atomically retrieve the newest frame and clear its event.
        with state_lock:
            if (
                latest_frame is None
                or latest_frame_id == last_processed_id
            ):
                frame_available.clear()
                continue

            frame = latest_frame.copy()
            current_frame_id = latest_frame_id
            frame_available.clear()

        last_processed_id = current_frame_id
        processing_start = perf_counter()

        try:
            result, inference_ms = run_prediction(frame)

            # Draw segmentation masks and bounding boxes.
            annotated = result.plot(
                line_width=1,
                labels=True,
                conf=False,
                boxes=True,
                masks=True
            )

            cv2.putText(
                annotated,
                f"Inference: {inference_ms:.1f} ms",
                (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (0, 255, 0),
                2,
                cv2.LINE_AA
            )

            success, encoded = cv2.imencode(
                ".jpg",
                annotated,
                [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
            )

            processing_ms = (
                perf_counter() - processing_start
            ) * 1000

            if not success:
                continue

            detection_count = (
                len(result.boxes)
                if result.boxes is not None
                else 0
            )

            with state_lock:
                latest_jpeg = encoded.tobytes()
                latest_jpeg_id += 1

                latest_stats = {
                    "inference_time_ms": round(inference_ms, 2),
                    "processing_time_ms": round(processing_ms, 2),
                    "processed_fps": round(
                        1000.0 / processing_ms
                        if processing_ms > 0 else 0.0,
                        2
                    ),
                    "detections": detection_count,
                    "last_error": None
                }

            logger.info(
                "Inference=%.1f ms | Processing=%.1f ms | "
                "Detections=%d | FPS=%.2f",
                inference_ms,
                processing_ms,
                detection_count,
                1000.0 / processing_ms if processing_ms > 0 else 0.0
            )

        except Exception as exc:
            logger.exception("Inference failed")

            with state_lock:
                latest_stats["last_error"] = str(exc)

    logger.info("Inference worker stopped")


# ============================================================
# APPLICATION LIFECYCLE
# ============================================================

@asynccontextmanager
async def lifespan(application: FastAPI):
    global model
    global picam2
    global capture_thread
    global inference_thread

    stop_event.clear()
    frame_available.clear()

    logger.info("Loading YOLO model: %s", MODEL_PATH)

    try:
        # Load the model exactly once.
        model = YOLO(MODEL_PATH)

        # Warm up the model before processing real camera frames.
        dummy_frame = np.zeros(
            (CAMERA_HEIGHT, CAMERA_WIDTH, 3),
            dtype=np.uint8
        )

        logger.info("Warming up model at imgsz=%d", IMAGE_SIZE)
        run_prediction(dummy_frame)
        del dummy_frame

        # Initialize Camera Module 3 only once.
        logger.info("Initializing Picamera2")

        picam2 = Picamera2()

        config = picam2.create_video_configuration(
            main={
                "size": (CAMERA_WIDTH, CAMERA_HEIGHT),
                "format": "RGB888"
            },
            buffer_count=2
        )

        picam2.configure(config)
        picam2.start()

        # Capture and inference run independently.
        capture_thread = threading.Thread(
            target=camera_capture_worker,
            name="camera-capture",
            daemon=True
        )

        inference_thread = threading.Thread(
            target=inference_worker,
            name="yolo-inference",
            daemon=True
        )

        capture_thread.start()
        inference_thread.start()

        logger.info("FastAPI camera inference is ready")

        yield

    finally:
        logger.info("Shutting down camera and inference")

        stop_event.set()
        frame_available.set()

        if capture_thread is not None:
            capture_thread.join(timeout=THREAD_JOIN_TIMEOUT)

        if inference_thread is not None:
            inference_thread.join(timeout=THREAD_JOIN_TIMEOUT)

        if picam2 is not None:
            try:
                picam2.stop()
            except Exception:
                logger.exception("Error stopping camera")

            try:
                picam2.close()
            except Exception:
                logger.exception("Error closing camera")

            picam2 = None

        model = None

        logger.info("Shutdown complete")


app = FastAPI(
    title="Raspberry Pi YOLO Segmentation API",
    version="2.0.0",
    lifespan=lifespan
)


# ============================================================
# HOME
# ============================================================

@app.get("/")
def home():
    return {
        "message": "YOLO Segmentation API is running",
        "image_size": IMAGE_SIZE,
        "confidence": CONFIDENCE,
        "endpoints": {
            "health": "/health",
            "stats": "/stats",
            "predict": "/predict",
            "camera": "/camera",
            "docs": "/docs"
        }
    }




# ============================================================
# IMAGE UPLOAD PREDICTION
# ============================================================

@app.post("/predict")
def predict(file: UploadFile = File(...)):
    if model is None:
        raise HTTPException(
            status_code=503,
            detail="Model is not ready"
        )

    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(
            status_code=400,
            detail="Please upload an image file"
        )

    image_bytes = file.file.read()

    if not image_bytes:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is empty"
        )

    image_array = np.frombuffer(
        image_bytes,
        dtype=np.uint8
    )

    frame = cv2.imdecode(
        image_array,
        cv2.IMREAD_COLOR
    )

    if frame is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid image file"
        )

    result, inference_ms = run_prediction(frame)

    return {
        "inference_time_ms": round(inference_ms, 2),
        "image_size": list(result.orig_shape),
        "detection_count": (
            len(result.boxes)
            if result.boxes is not None
            else 0
        ),
        "detections": build_detections(result)
    }


# ============================================================
# SHARED LIVE CAMERA STREAM
# ============================================================

async def mjpeg_generator():
    """
    All browser clients receive the same processed frames.
    Opening another browser tab does not start another inference loop.
    """

    last_sent_id = -1

    while not stop_event.is_set():

        with state_lock:
            jpeg = latest_jpeg
            jpeg_id = latest_jpeg_id

        if jpeg is not None and jpeg_id != last_sent_id:
            last_sent_id = jpeg_id

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-cache\r\n\r\n"
                + jpeg
                + b"\r\n"
            )
        else:
            await asyncio.sleep(0.01)


@app.get("/camera")
async def camera_stream():
    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no"
        }
    )

