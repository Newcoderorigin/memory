"""
Real-time shot meter and green window detector.

NBA 2K26 shot meter anatomy (from screenshots):
  - A white/light-gray curved arc appears to the left of the player
  - As the animation plays, a fill colour travels along the arc
  - A bright green indicator marks the optimal release zone (green window)
  - "Excellent" / "Slightly Early" / "Late" text appears above the player
    on shot completion

Detection pipeline (per frame, runs at 60–120 FPS):
  1. Grab BGR frame from ScreenCapture
  2. Convert to HSV
  3. Green-mask  → isolate green window pixels
  4. White-mask  → isolate meter arc fill
  5. Result-mask → detect outcome text region (green colour, upper frame)
  6. Estimate fill_pct from topmost white pixel row
  7. Estimate green_window_pct from centroid of green region
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass, field
from typing import Optional, Callable

import numpy as np

try:
    import cv2
    _CV2_OK = True
except ImportError:
    _CV2_OK = False
    print("[MeterDetector] opencv-python not installed. Run: pip install opencv-python")

from .screen_capture import ScreenCapture

# ── HSV colour ranges ─────────────────────────────────────────────────────────
# Bright green (shot meter green window indicator)
_GREEN_LO = np.array([38,  110, 100], dtype=np.uint8)
_GREEN_HI = np.array([88,  255, 255], dtype=np.uint8)

# White / very light gray (meter arc body + fill)
_WHITE_LO = np.array([0,   0,  195], dtype=np.uint8)
_WHITE_HI = np.array([180, 35, 255], dtype=np.uint8)

# "Excellent" result text (slightly different green, rendered with anti-alias)
_RESULT_LO = np.array([38,  70, 140], dtype=np.uint8)
_RESULT_HI = np.array([92, 255, 255], dtype=np.uint8)

# Minimum pixel counts to consider a detection valid
_MIN_GREEN_PX  = 25
_MIN_WHITE_PX  = 80
_MIN_RESULT_PX = 45


@dataclass
class DetectionResult:
    timestamp:            float = field(default_factory=time.perf_counter)
    meter_found:          bool  = False
    green_window_visible: bool  = False
    green_window_pct:     float = 0.0   # position of green window on arc (0=start, 1=end)
    fill_pct:             float = 0.0   # current animation fill (0=empty, 1=full)
    outcome_detected:     bool  = False # "Excellent" / result text visible
    confidence:           float = 0.0   # [0,1] based on pixel count
    debug_frame:          Optional[np.ndarray] = field(default=None, compare=False)


class MeterDetector:
    """
    Detects the shot meter fill level and green window position in real time.

    Designed to run at 60+ FPS; each detect() call is ~2–4 ms on a modern CPU
    when the capture region is ~520×480 pixels.
    """

    def __init__(
        self,
        capture: ScreenCapture,
        debug:   bool = False,
        on_result: Optional[Callable[[DetectionResult], None]] = None,
    ) -> None:
        self._cap       = capture
        self._debug     = debug
        self._on_result = on_result
        self._lock      = threading.Lock()
        self._latest    = DetectionResult()
        self._running   = False

    @property
    def latest(self) -> DetectionResult:
        with self._lock:
            return self._latest

    # ── Single-frame detection ────────────────────────────────────────────────

    def detect(self) -> DetectionResult:
        """Grab one frame and run the full detection pipeline."""
        if not _CV2_OK:
            return DetectionResult()
        frame = self._cap.grab()
        if frame is None:
            return DetectionResult()
        result = self._analyze(frame)
        with self._lock:
            self._latest = result
        if self._on_result:
            try:
                self._on_result(result)
            except Exception:
                pass
        return result

    # ── Background loop ───────────────────────────────────────────────────────

    def start(self, fps: int = 120) -> None:
        """Launch a daemon detection loop at the requested frame rate."""
        if self._running:
            return
        self._running = True
        interval = 1.0 / fps
        t = threading.Thread(
            target=self._loop,
            args=(interval,),
            daemon=True,
            name="meter-detector",
        )
        t.start()

    def stop(self) -> None:
        self._running = False

    def _loop(self, interval: float) -> None:
        import time as _time
        while self._running:
            t0 = _time.perf_counter()
            self.detect()
            elapsed = _time.perf_counter() - t0
            remaining = interval - elapsed
            if remaining > 0:
                _time.sleep(remaining)

    # ── Analysis ──────────────────────────────────────────────────────────────

    def _analyze(self, frame: np.ndarray) -> DetectionResult:
        h, w = frame.shape[:2]
        hsv  = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        r    = DetectionResult()

        # ── Green window detection ────────────────────────────────────────────
        green_mask = cv2.inRange(hsv, _GREEN_LO, _GREEN_HI)
        # Morphological open to kill noise
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        green_mask = cv2.morphologyEx(green_mask, cv2.MORPH_OPEN, kernel)
        green_px = int(cv2.countNonZero(green_mask))

        if green_px >= _MIN_GREEN_PX:
            r.green_window_visible = True
            r.confidence = min(1.0, green_px / 300.0)
            M = cv2.moments(green_mask)
            if M["m00"] > 0:
                # cy normalised to frame height — arc fills from bottom upward
                cy = M["m01"] / M["m00"]
                r.green_window_pct = 1.0 - (cy / h)

        # ── Meter fill detection ──────────────────────────────────────────────
        white_mask = cv2.inRange(hsv, _WHITE_LO, _WHITE_HI)
        white_px   = int(cv2.countNonZero(white_mask))

        if white_px >= _MIN_WHITE_PX:
            r.meter_found = True
            # Topmost non-zero row → arc fill travels upward
            rows_with_white = np.any(white_mask > 0, axis=1)
            if rows_with_white.any():
                top_row  = int(np.argmax(rows_with_white))
                r.fill_pct = 1.0 - (top_row / h)

        # ── Shot outcome detection ────────────────────────────────────────────
        # Result text appears in the upper third of the capture region
        result_roi  = hsv[: h // 3, :]
        result_mask = cv2.inRange(result_roi, _RESULT_LO, _RESULT_HI)
        if int(cv2.countNonZero(result_mask)) >= _MIN_RESULT_PX:
            r.outcome_detected = True

        # ── Debug visualisation ───────────────────────────────────────────────
        if self._debug:
            dbg = frame.copy()
            dbg[green_mask > 0]  = [0,   255, 80]
            dbg[white_mask > 0]  = [200, 200, 255]
            r.debug_frame = dbg

        return r
