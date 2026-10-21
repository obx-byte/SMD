"""Camera service for VisionInspection.

Manages a single USB camera instance with a background thread for continuous
frame capture.  Provides thread-safe access to the latest frame and the current
camera status (CONNECTING / ON / OFF / ERROR).

This module is imported lazily so that OpenCV is only required when the camera
is actually used.
"""

import threading
import time
from collections import deque

import cv2
import numpy as np

# --------------------------------------------------------------------------- #
#  Constants
# --------------------------------------------------------------------------- #

CAMERA_STATES = {
    "CONNECTING": "CONNECTING",
    "ON": "ON",
    "OFF": "OFF",
    "ERROR": "ERROR",
}

# Camera lifecycle state machine: STOPPED → STARTING → RUNNING → STOPPING → STOPPED
# RUNNING transitions to ERROR on disconnection / failed reads.
CAMERA_LIFECYCLE = {
    "STOPPED": "STOPPED",
    "STARTING": "STARTING",
    "RUNNING": "RUNNING",
    "STOPPING": "STOPPING",
    "ERROR": "ERROR",
}

# Camera sources.  OpenCV (USB/V4L2) is always available; the Raspberry Pi
# backend (Picamera2) is imported lazily only when selected.
CAMERA_SOURCES = {
    "OPENCV": "opencv",
    "PICAMERA": "picamera",
}


def picamera_available() -> bool:
    """Return True if the Raspberry Pi ``picamera2`` backend can be imported.

    Import is attempted lazily so Windows / non-Pi hosts never import it at
    module load time.
    """
    try:
        import picamera2  # noqa: F401

        return True
    except Exception:
        return False


def get_camera_capabilities() -> dict:
    """Report which camera sources this platform supports."""
    return {
        "sources": [
            {
                "id": CAMERA_SOURCES["OPENCV"],
                "label": "OpenCV Camera",
                "available": True,
                "needs_index": True,
            },
            {
                "id": CAMERA_SOURCES["PICAMERA"],
                "label": "Raspberry Pi Camera",
                "available": picamera_available(),
                "needs_index": False,
            },
        ],
        "default_source": CAMERA_SOURCES["OPENCV"],
        "default_index": 0,
    }

# Maximum number of frames in the bounded ring buffer used by get_frame_buffer().
# The real-time inspection path uses a single latest-frame slot instead.
_FRAME_BUFFER_SIZE = 5

# ---------------------------------------------------------------------------
#  Process-wide device ownership
#
#  ``cv2.VideoCapture`` is not re-entrant for the same physical device: opening
#  the same index from two threads at once (e.g. a retried ``start_camera``
#  racing a still-connecting background loop) raises an access violation in the
#  native DirectShow/MSMF layer, which aborts the whole process — Python cannot
#  catch it.  So device open/close is serialized process-wide, and a device that
#  is already held by another capture loop is refused with a readable error
#  instead of being opened twice.
# ---------------------------------------------------------------------------
_DEVICE_LOCK = threading.Lock()
_OPEN_DEVICES: set = set()


def _device_key(camera_index):
    return (int(camera_index or 0), "opencv")


def _claim_device(camera_index):
    """Reserve a physical camera index.  True when this caller owns it."""
    key = _device_key(camera_index)
    with _DEVICE_LOCK:
        if key in _OPEN_DEVICES:
            return False
        _OPEN_DEVICES.add(key)
        return True


def _release_device(camera_index):
    key = _device_key(camera_index)
    with _DEVICE_LOCK:
        _OPEN_DEVICES.discard(key)


def _safe_release(cap):
    """Release a capture device that was never published to the shared slot.

    Only used for objects created locally by ``_connect_*`` and never handed to
    any other thread — the shared slot is emptied by ``_take_capture_slot``.
    """
    try:
        if cap is not None:
            cap.release()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
#  Singleton camera service
# --------------------------------------------------------------------------- #


class CameraService:
    """Thread-safe singleton camera service.

    Usage::
        CameraService.start(camera_index=0)
        ret, frame = CameraService.get_frame()
        status = CameraService.get_status()
        CameraService.stop()
    """

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

        self._cap = None
        self._status = CAMERA_STATES["OFF"]
        self._state = CAMERA_LIFECYCLE["STOPPED"]
        self._error_message = None
        self._source = CAMERA_SOURCES["OPENCV"]  # default backend
        self._camera_index = 0
        self._thread = None
        self._stop_event = threading.Event()
        self._frame_lock = threading.Lock()
        self._connect_lock = threading.Lock()  # guards device open (one opener)
        # Guards the ``self._cap`` slot only (assign / snapshot / release).
        #
        # The capture device is NOT thread-safe in its native backend: two
        # concurrent ``release()`` calls -- or a ``read()`` racing a
        # ``release()`` -- raise an access violation in the OpenCV C++ layer
        # that Python cannot catch and which aborts the whole process.  So the
        # slot is emptied *before* the device is released, and only the capture
        # thread that owns the current epoch may release it.  ``self._epoch`` is
        # bumped by every ``start_camera()`` so a stale thread whose join timed
        # out can never release a device a newer owner is using.
        self._cap_lock = threading.Lock()
        self._epoch = 0
        self._latest_frame = None
        self._frame_buffer = deque(maxlen=_FRAME_BUFFER_SIZE)
        self._reconnect_delay = 2.0
        self._target_fps = 30
        self._frame_time = 1.0 / self._target_fps
        # Latest-frame bookkeeping (single-slot, bounded memory)
        self._frame_seq = 0          # monotonic id of the last stored frame
        self._frames_dropped = 0     # latest-frame overwrites (stale frames discarded)
        self._last_frame_monotonic = None

    # ------------------------------------------------------------------ #
    #  Public API
    # ------------------------------------------------------------------ #

    @classmethod
    def start_camera(cls, camera_index=0, target_fps=30, source=None, reuse_existing=False):
        """Start the camera capture thread.

        Parameters
        ----------
        camera_index : int
            USB camera device index (0, 1, 2, ...).  Ignored for Picamera2.
        target_fps : int
            Target capture frames-per-second.
        source : str | None
            ``"opencv"`` (default) or ``"picamera"``.
        reuse_existing : bool
            Do not restart a live capture. Returns ``False`` when a live camera
            is reused or has incompatible settings; callers can inspect the
            current source and index to distinguish those cases.

        Returns ``True`` when a capture thread was started, ``False`` when a
        previous thread could not be retired in time (as a restart could race it
        on the shared capture device, corrupting the process).
        """
        service = cls()
        with cls._lock:
            requested_index = int(camera_index or 0)
            requested_source = source or CAMERA_SOURCES["OPENCV"]
            if reuse_existing and service._status in (
                CAMERA_STATES["ON"],
                CAMERA_STATES["CONNECTING"],
            ):
                return False
            if not service._retire_capture_thread():
                service._error_message = (
                    "The previous camera thread did not stop; "
                    "camera was not restarted."
                )
                service._status = CAMERA_STATES["ERROR"]
                service._state = CAMERA_LIFECYCLE["ERROR"]
                return False

            service._camera_index = requested_index
            service._target_fps = max(1, target_fps)
            service._frame_time = 1.0 / service._target_fps
            service._source = requested_source
            service._error_message = None
            service._stop_event = threading.Event()
            service._status = CAMERA_STATES["CONNECTING"]
            service._state = CAMERA_LIFECYCLE["STARTING"]
            with service._frame_lock:
                service._latest_frame = None
                service._frame_buffer.clear()
                service._frame_seq = 0
                service._frames_dropped = 0
                service._last_frame_monotonic = None

            # Bump the epoch *before* the new thread starts so the thread owns a
            # capture device no older thread will release.
            service._epoch += 1
            service._thread = threading.Thread(
                target=service._capture_loop,
                args=(service._stop_event, service._epoch),
                daemon=True,
                name="VisionInspection-CameraThread",
            )
            service._thread.start()

        return True

    def _retire_capture_thread(self, timeout=10.0):
        """Stop and join the running capture thread; True when it is gone.

        Must be called with ``self._lock`` held.  Never releases the capture
        device itself: the owning capture thread does that in its own clean-up
        path, exactly once, under its own epoch.
        """
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._stop_event.set()
            deadline = time.monotonic() + timeout
            while thread.is_alive() and time.monotonic() < deadline:
                thread.join(timeout=0.1)
            if thread.is_alive():
                # Leave the (still live) thread running with its epoch; do NOT
                # start a second thread that would share its capture device.
                return False

        self._thread = None
        # The thread has finished, so it has already released its device unless
        # it died before its clean-up ran.  Release anything left behind.
        self._release_owned_capture(self._epoch)
        return True

    @classmethod
    def get_error(cls):
        """Return the last camera error message (or None)."""
        return cls()._error_message

    @classmethod
    def get_source(cls):
        """Return the active camera source id ('opencv' / 'picamera')."""
        return cls()._source

    @classmethod
    def get_camera_index(cls):
        """Return the index of the active camera source."""
        service = cls()
        with cls._lock:
            return service._camera_index

    @classmethod
    def stop_camera(cls):
        """Stop the camera capture thread and release the device."""
        service = cls()
        with cls._lock:
            service._stop_camera_internal()
        return True

    @classmethod
    def get_frame(cls):
        """Return ``(True, frame)`` if a frame is available, else ``(False, None)``.

        Returns a copy so callers may safely mutate it.  For the real-time
        inspection hot-path use :meth:`get_frame_with_meta`, which returns a
        zero-copy read intended for slicing (view) consumers only.
        """
        service = cls()
        with service._frame_lock:
            if service._latest_frame is None:
                return False, None
            return True, service._latest_frame.copy()

    @classmethod
    def push_frame(cls, frame):
        """Store the newest frame in the single latest-frame slot.

        Any previously unprocessed frame is discarded (latest-frame semantics),
        so the buffer never grows.  Updates frame id / drop counters and the
        acquisition timestamp used to report frame age.  ``frame`` is assumed to
        be owned by the producer (e.g. the return value of ``cap.read()``); it
        is stored by reference — producers never mutate stored frames.
        """
        service = cls()
        with service._frame_lock:
            if service._latest_frame is not None:
                service._frames_dropped += 1
            service._frame_seq += 1
            service._last_frame_monotonic = time.perf_counter()
            service._latest_frame = frame
            service._frame_buffer.append(frame)

    @classmethod
    def get_frame_with_meta(cls):
        """Zero-copy latest-frame read for the inspection loop.

        Returns ``(ok, frame, meta)`` where *meta* carries frame id, age in ms
        and cumulative dropped-frame count.  The returned array is a shared
        reference — the caller MUST NOT mutate it (slicing produces a view,
        which is safe).
        """
        service = cls()
        with service._frame_lock:
            if service._latest_frame is None:
                return False, None, {
                    "seq": 0, "age_ms": None, "dropped": service._frames_dropped,
                }
            age_ms = None
            if service._last_frame_monotonic is not None:
                age_ms = (time.perf_counter() - service._last_frame_monotonic) * 1000.0
            meta = {
                "seq": service._frame_seq,
                "age_ms": round(age_ms, 2) if age_ms is not None else None,
                "dropped": service._frames_dropped,
            }
            return True, service._latest_frame, meta

    @classmethod
    def get_frame_buffer(cls, max_frames=5):
        """Return the last *max_frames* frames as a list of copies."""
        service = cls()
        with service._frame_lock:
            frames = list(service._frame_buffer)[-max_frames:]
        return [f.copy() for f in frames]

    @classmethod
    def get_status(cls):
        """Return the current camera status string."""
        service = cls()
        return service._status

    @classmethod
    def get_state(cls):
        """Return the camera lifecycle state (STOPPED/STARTING/RUNNING/STOPPING/ERROR)."""
        service = cls()
        return service._state

    @classmethod
    def get_resolution(cls):
        """Return ``(width, height)`` of the current camera, or ``(0, 0)``."""
        service = cls()
        with service._frame_lock:
            if service._latest_frame is not None:
                h, w = service._latest_frame.shape[:2]
                return w, h
        return 0, 0

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    def _stop_camera_internal(self):
        """Internal: stop the capture thread (device release stays with the owner)."""
        self._state = CAMERA_LIFECYCLE["STOPPING"]
        self._retire_capture_thread()

        with self._frame_lock:
            self._latest_frame = None
            self._frame_buffer.clear()

        self._status = CAMERA_STATES["OFF"]
        self._state = CAMERA_LIFECYCLE["STOPPED"]
        self._error_message = None

    def _take_capture_slot(self):
        """Empty the capture slot and return the device handle (idempotent)."""
        with self._cap_lock:
            cap = self._cap
            self._cap = None
        return cap

    def _release_owned_capture(self, epoch):
        """Release the capture device only if *epoch* still owns it.

        Emptying the slot *before* releasing is what makes this safe to call
        from more than one path: the second caller sees ``None`` and does
        nothing, so the native device is never released twice.
        """
        if epoch is not None and epoch != self._epoch:
            return False  # a newer owner has taken over; never touch its device

        was_holding = self._cap is not None
        cap = self._take_capture_slot()
        if cap is None:
            return False

        try:
            if self._source == CAMERA_SOURCES["PICAMERA"]:
                cap.stop()
                cap.close()
            else:
                cap.release()
        except Exception:
            pass
        if was_holding and self._source != CAMERA_SOURCES["PICAMERA"]:
            _release_device(self._camera_index)
        return True

    def _connect(self):
        """Attempt to (re)connect using the selected backend (one opener)."""
        with self._connect_lock:
            self._status = CAMERA_STATES["CONNECTING"]
            if self._source == CAMERA_SOURCES["PICAMERA"]:
                return self._connect_picamera()
            return self._connect_opencv()

    def _store_capture(self, cap):
        """Publish an opened capture device to the slot (guarded)."""
        with self._cap_lock:
            self._cap = cap

    def _connect_opencv(self):
        """Open the USB/V4L2 camera via OpenCV."""
        self._status = CAMERA_STATES["CONNECTING"]
        if not _claim_device(self._camera_index):
            self._error_message = (
                f"Camera index {self._camera_index} is already in use by another "
                "capture thread. Stop the running inspection first."
            )
            self._status = CAMERA_STATES["ERROR"]
            self._state = CAMERA_LIFECYCLE["ERROR"]
            return False

        cap = None
        try:
            cap = cv2.VideoCapture(self._camera_index, cv2.CAP_DSHOW)
            if cap is None or not cap.isOpened():
                self._error_message = f"Could not open OpenCV camera index {self._camera_index}."
                self._status = CAMERA_STATES["ERROR"]
                self._state = CAMERA_LIFECYCLE["ERROR"]
                _release_device(self._camera_index)
                return False

            # Set a reasonable default resolution
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            cap.set(cv2.CAP_PROP_FPS, self._target_fps)

            # Verify a frame can actually be read.
            ok, _probe = cap.read()
            if not ok:
                self._error_message = "Camera opened but no frames could be read."
                self._status = CAMERA_STATES["ERROR"]
                self._state = CAMERA_LIFECYCLE["ERROR"]
                _safe_release(cap)
                _release_device(self._camera_index)
                return False

            # Keep the first successful probe frame so the very first
            # inspection request sees a valid image immediately instead of
            # waiting for the background thread to capture the next frame.
            self.push_frame(_probe)

            # Publish only a fully working device to the shared slot.
            self._store_capture(cap)
            self._status = CAMERA_STATES["ON"]
            self._state = CAMERA_LIFECYCLE["RUNNING"]
            return True
        except Exception as exc:
            self._error_message = f"OpenCV camera error: {exc}"
            self._status = CAMERA_STATES["ERROR"]
            self._state = CAMERA_LIFECYCLE["ERROR"]
            if cap is not None:
                _safe_release(cap)
            _release_device(self._camera_index)
            return False

    def _connect_picamera(self):
        """Open the Raspberry Pi camera via Picamera2 (imported lazily).

        Never imports ``picamera2`` on hosts (e.g. Windows) where it is absent;
        reports unavailability clearly and fails into the ERROR state.
        """
        self._status = CAMERA_STATES["CONNECTING"]
        if not picamera_available():
            self._error_message = (
                "Raspberry Pi camera backend (picamera2) is not available on this system."
            )
            self._status = CAMERA_STATES["ERROR"]
            self._state = CAMERA_LIFECYCLE["ERROR"]
            return False
        try:
            from picamera2 import Picamera2  # noqa: WPS433 (local import by design)

            self._cap = Picamera2()
            config = self._cap.create_video_configuration(
                main={"size": (1280, 720), "format": "RGB888"}
            )
            self._cap.configure(config)
            self._cap.start()
            self._status = CAMERA_STATES["ON"]
            self._state = CAMERA_LIFECYCLE["RUNNING"]
            return True
        except Exception as exc:
            self._error_message = f"Raspberry Pi camera error: {exc}"
            self._status = CAMERA_STATES["ERROR"]
            self._state = CAMERA_LIFECYCLE["ERROR"]
            self._release_owned_capture(self._epoch)
            return False

    def _read_frame(self):
        """Return ``(ok, bgr_frame)`` from the active backend.

        Reads the slot snapshot *after* acquiring the capture lock so a
        concurrent release cannot free the device while a frame is being read.
        """
        with self._cap_lock:
            cap = self._cap
            if cap is None:
                return False, None
            if self._source == CAMERA_SOURCES["PICAMERA"]:
                rgb = cap.capture_array()
                if rgb is None:
                    return False, None
                return True, cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            return cap.read()

    def _capture_loop(self, stop_event=None, epoch=None):
        """Background thread: continuously read frames from the camera.

        This thread owns the capture device it opened: it is the only place
        that may release it.  ``stop_event`` and ``epoch`` are captured so a
        thread whose restart raced an old one stops immediately and never
        touches a device owned by a newer epoch.
        """
        if stop_event is None:
            stop_event = self._stop_event
        if epoch is None:
            epoch = self._epoch

        if stop_event.is_set() or epoch != self._epoch:
            return

        if not self._connect():
            return

        while not stop_event.is_set() and epoch == self._epoch:
            try:
                ret, frame = self._read_frame()
                if not ret or frame is None:
                    # Camera likely disconnected
                    if self._status != CAMERA_STATES["ERROR"]:
                        self._status = CAMERA_STATES["ERROR"]
                    self._state = CAMERA_LIFECYCLE["ERROR"]
                    if not self._error_message:
                        self._error_message = "Camera stopped returning frames."
                    time.sleep(self._reconnect_delay)
                    # Try to reconnect (guarded, one opener)
                    if stop_event.is_set() or epoch != self._epoch:
                        break
                    self._connect()
                    continue

                self._status = CAMERA_STATES["ON"]
                self._state = CAMERA_LIFECYCLE["RUNNING"]

                # Latest-frame store (single slot, bounded; drops stale frames)
                self.push_frame(frame)

                # Maintain target FPS
                time.sleep(self._frame_time)

            except Exception as exc:
                self._error_message = f"Camera capture error: {exc}"
                self._status = CAMERA_STATES["ERROR"]
                self._state = CAMERA_LIFECYCLE["ERROR"]
                if stop_event.is_set() or epoch != self._epoch:
                    break
                time.sleep(self._reconnect_delay)

        # Release only what this thread still owns — after the snapshot is
        # cleared — so the device is never released twice or under another
        # epoch's feet (a double release aborts the process natively).
        self._release_owned_capture(epoch)
        if epoch == self._epoch:
            self._status = CAMERA_STATES["OFF"]
            self._state = CAMERA_LIFECYCLE["STOPPED"]


# --------------------------------------------------------------------------- #
#  Module-level convenience API (uses the singleton)
# --------------------------------------------------------------------------- #

start_camera = CameraService.start_camera
stop_camera = CameraService.stop_camera
get_frame = CameraService.get_frame
get_frame_with_meta = CameraService.get_frame_with_meta
push_frame = CameraService.push_frame
get_frame_buffer = CameraService.get_frame_buffer
get_status = CameraService.get_status
get_state = CameraService.get_state
get_error = CameraService.get_error
get_source = CameraService.get_source
get_resolution = CameraService.get_resolution
