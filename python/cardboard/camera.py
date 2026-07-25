"""Camera capture — port of the C# Services/CameraService.cs.

Device *names* are the one genuinely per-OS piece: OpenCV can open a device by index but
cannot tell you what it is called. Each platform is handled separately, and every backend
falls back to probing indices so the app still works if name lookup is unavailable.

Opening a device is deliberately non-throwing: a camera already in use by another app
(Discord, Zoom, OBS...) often "opens" but never delivers a frame, so a test grab decides.
"""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

DEVICE_BUSY_MESSAGE = (
    "This video device can't be opened — it's most likely already in use by another "
    "application (Discord, Zoom, OBS, Teams, the Camera app, etc.).\n\n"
    "Close the other app that's using the camera, or pick a different device, then try again."
)


@dataclass(frozen=True)
class CameraDevice:
    """A selectable video input: its OpenCV device index and friendly name."""

    index: int
    name: str

    def __str__(self) -> str:
        return self.name


# ---------------- device enumeration (per-OS) ----------------

def _windows_devices() -> list[CameraDevice]:
    """DirectShow enumeration order matches OpenCV's CAP_DSHOW index.

    Runs on a dedicated thread that initialises COM itself. DirectShow requires COM to be
    initialised per-thread, and the caller's apartment state cannot be relied upon: a Qt GUI
    thread may already have COM in a conflicting mode, and thread-pool workers have none at
    all. Either case makes enumeration throw, which would silently degrade the device list to
    "Camera 0", "Camera 1".
    """
    result: list[CameraDevice] = []

    def enumerate_with_com() -> None:
        initialised = False
        try:
            import comtypes
            try:
                comtypes.CoInitialize()
                initialised = True
            except Exception:
                pass  # already initialised in a compatible mode

            from pygrabber.dshow_graph import FilterGraph
            names = FilterGraph().get_input_devices()
            result.extend(CameraDevice(i, n or f"Camera {i}") for i, n in enumerate(names))
        except Exception:
            result.clear()
        finally:
            if initialised:
                try:
                    import comtypes
                    comtypes.CoUninitialize()
                except Exception:
                    pass

    thread = threading.Thread(target=enumerate_with_com, name="dshow-enumerate", daemon=True)
    thread.start()
    thread.join(timeout=15)
    return result


def _linux_devices() -> list[CameraDevice]:
    """/dev/videoN maps to OpenCV index N; the friendly name lives in sysfs."""
    devices: list[CameraDevice] = []
    for path in sorted(glob.glob("/dev/video*")):
        m = re.search(r"(\d+)$", path)
        if not m:
            continue
        index = int(m.group(1))
        name = f"Camera {index}"
        sysfs = Path(f"/sys/class/video4linux/video{index}/name")
        try:
            if sysfs.exists():
                name = sysfs.read_text(encoding="utf-8").strip() or name
        except OSError:
            pass
        devices.append(CameraDevice(index, name))

    # V4L2 exposes several nodes per physical camera; only the first is capturable.
    seen: set[str] = set()
    unique: list[CameraDevice] = []
    for d in devices:
        if d.name in seen:
            continue
        seen.add(d.name)
        unique.append(d)
    return unique


def _macos_devices() -> list[CameraDevice]:
    """system_profiler lists cameras in the same order AVFoundation indexes them."""
    try:
        out = subprocess.run(
            ["system_profiler", "-json", "SPCameraDataType"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        data = json.loads(out.stdout or "{}")
        cams = data.get("SPCameraDataType", []) or []
        names = [c.get("_name") or f"Camera {i}" for i, c in enumerate(cams)]
        return [CameraDevice(i, n) for i, n in enumerate(names)]
    except Exception:
        return []


def _preferred_backend() -> int:
    if sys.platform == "win32":
        return cv2.CAP_DSHOW      # most reliable for USB webcams on Windows
    if sys.platform == "darwin":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_V4L2


def enumerate_devices(max_probe: int = 8) -> list[CameraDevice]:
    """List video inputs with friendly names, falling back to index probing."""
    if sys.platform == "win32":
        found = _windows_devices()
    elif sys.platform == "darwin":
        found = _macos_devices()
    else:
        found = _linux_devices()

    if found:
        return found

    probed: list[CameraDevice] = []
    for i in range(max_probe):
        cap = None
        try:
            cap = cv2.VideoCapture(i, _preferred_backend())
            if cap.isOpened():
                probed.append(CameraDevice(i, f"Camera {i}"))
        except Exception:
            pass
        finally:
            if cap is not None:
                cap.release()
    return probed


# ---------------- capture ----------------

class CameraService:
    """Captures frames on a background thread, invoking ``on_frame`` for each one.

    Property changes (focus etc.) are queued and applied on the capture thread, since
    VideoCapture is not safe to touch while another thread is reading from it.
    """

    def __init__(self) -> None:
        self._capture: Optional[cv2.VideoCapture] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._commands: list[Callable[[cv2.VideoCapture], None]] = []
        self._commands_lock = threading.Lock()
        self._auto_focus = True

        self.device_index = 0
        self.frame_width = 0
        self.frame_height = 0
        self.last_error: Optional[str] = None

        #: Called with each BGR frame (owned by the callee).
        self.on_frame: Optional[Callable[[np.ndarray], None]] = None
        #: Called if the device cannot be opened or the stream ends unexpectedly.
        self.on_error: Optional[Callable[[str], None]] = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, device_index: int, width: int = 1280, height: int = 720) -> bool:
        """Open a device and begin capturing. Returns False (setting ``last_error``)
        rather than raising when the device is missing or in use."""
        self.stop()
        self.last_error = None
        self.device_index = device_index

        capture = None
        try:
            capture = self._try_open(device_index, _preferred_backend())
            if capture is None:
                capture = self._try_open(device_index, cv2.CAP_ANY)
            if capture is None:
                self.last_error = DEVICE_BUSY_MESSAGE
                return False

            capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

            # A busy device often opens but never delivers a frame — prove it works.
            if not self._can_grab_frame(capture):
                capture.release()
                self.last_error = DEVICE_BUSY_MESSAGE
                return False

            self.frame_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            self.frame_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            try:
                capture.set(cv2.CAP_PROP_AUTOFOCUS, 1 if self._auto_focus else 0)
            except Exception:
                pass

            with self._commands_lock:
                self._commands.clear()  # drop stale commands from a prior session

            self._capture = capture
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="camera", daemon=True)
            self._thread.start()
            return True
        except Exception as e:
            if capture is not None:
                try:
                    capture.release()
                except Exception:
                    pass
            self.last_error = f"{DEVICE_BUSY_MESSAGE}\n\n(Details: {e})"
            return False

    @staticmethod
    def _try_open(index: int, backend: int) -> Optional[cv2.VideoCapture]:
        try:
            cap = cv2.VideoCapture(index, backend)
            if cap.isOpened():
                return cap
            cap.release()
        except Exception:
            pass
        return None

    @staticmethod
    def _can_grab_frame(capture: cv2.VideoCapture, attempts: int = 20) -> bool:
        """Try to read one frame within ~1s to prove the device is actually usable."""
        for _ in range(attempts):
            try:
                ok, frame = capture.read()
                if ok and frame is not None and frame.size:
                    return True
            except Exception:
                return False
            time.sleep(0.05)
        return False

    def _loop(self) -> None:
        consecutive_failures = 0
        while not self._stop.is_set() and self._capture is not None:
            with self._commands_lock:
                pending, self._commands = self._commands, []
            for command in pending:
                try:
                    command(self._capture)
                except Exception:
                    pass

            try:
                ok, frame = self._capture.read()
            except Exception:
                ok, frame = False, None

            if not ok or frame is None or not frame.size:
                consecutive_failures += 1
                if consecutive_failures > 30:
                    self._emit_error("Video stream ended or device was disconnected.")
                    break
                time.sleep(0.015)
                continue
            consecutive_failures = 0

            if self.on_frame is not None:
                try:
                    self.on_frame(frame)
                except Exception:
                    pass

            time.sleep(0.01)

    def _emit_error(self, message: str) -> None:
        if self.on_error is not None:
            try:
                self.on_error(message)
            except Exception:
                pass

    # ---------------- focus controls ----------------
    # Best-effort: whether these take effect depends on driver support.

    def _enqueue(self, command: Callable[[cv2.VideoCapture], None]) -> None:
        if self.is_running:
            with self._commands_lock:
                self._commands.append(command)

    def set_auto_focus(self, on: bool) -> None:
        self._auto_focus = on
        self._enqueue(lambda c: c.set(cv2.CAP_PROP_AUTOFOCUS, 1 if on else 0))

    def set_focus(self, value: float) -> None:
        """Set a manual focus position (also disables autofocus)."""
        self._auto_focus = False

        def apply(c: cv2.VideoCapture) -> None:
            c.set(cv2.CAP_PROP_AUTOFOCUS, 0)
            c.set(cv2.CAP_PROP_FOCUS, value)

        self._enqueue(apply)

    def trigger_refocus(self) -> None:
        """Nudge the camera to re-run autofocus (toggle it off then on)."""

        def apply(c: cv2.VideoCapture) -> None:
            c.set(cv2.CAP_PROP_AUTOFOCUS, 0)
            time.sleep(0.06)
            c.set(cv2.CAP_PROP_AUTOFOCUS, 1)

        self._enqueue(apply)
        self._auto_focus = True

    def open_native_settings(self) -> None:
        """Open the driver's own settings dialog (Windows/DirectShow only)."""
        self._enqueue(lambda c: c.set(cv2.CAP_PROP_SETTINGS, 1))

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        if self._capture is not None:
            try:
                self._capture.release()
            except Exception:
                pass
            self._capture = None

    def __enter__(self) -> "CameraService":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()
