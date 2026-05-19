"""
Real-time shot meter vision engine for NBA 2K26 (offline).

Pipeline (dedicated thread, up to 240 Hz):
  dxcam (DirectX Desktop Duplication) ─▶  ROI frame  (~3-5 ms)
      fallback: mss (~9 ms)
  cv2.cvtColor BGR→HSV + cv2.inRange ─▶  fill mask   (~2 ms)
  KalmanTracker1D ─▶  smoothed fill position + velocity
  MeterSnapshot ─▶  consumed by ShotMeterController

Detection heuristic:
  The shot meter is a vertical bar. The fill rises from bottom to top.
  Any pixel with brightness (HSV Value) above fill_v_threshold is
  considered fill.  The topmost such row determines fill percentage.
  When the fill enters the green zone it turns bright green — the
  green_hsv bounds detect that separately.

Coordinate convention:
  ROI is specified as (left, top, right, bottom) pixel coordinates
  on the primary display (dxcam native format).  mss uses the same
  region after internal conversion.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# ── Optional imports (graceful degradation) ───────────────────────────────────
try:
    import dxcam
    _DXCAM_OK = True
except ImportError:
    _DXCAM_OK = False

try:
    import cv2
    _CV2_OK = True
except ImportError:
    _CV2_OK = False

try:
    import mss
    _MSS_OK = True
except ImportError:
    _MSS_OK = False


@dataclass
class MeterConfig:
    """All tunable parameters for shot meter detection."""
    roi: tuple[int, int, int, int] = (860, 600, 1060, 900)
    # (left, top, right, bottom) — default: centre-bottom, adjust per resolution

    # Brightness threshold — pixels above this are considered "fill"
    fill_v_threshold: int = 100   # HSV Value channel, 0-255

    # Green zone HSV bounds (when fill turns green inside the green window)
    green_h_lo: int = 45
    green_h_hi: int = 95
    green_s_lo: int = 60
    green_v_lo: int = 60

    # Kalman process noise / measurement noise
    kalman_Q: float = 0.02   # larger → trusts measurements more
    kalman_R: float = 4.0    # larger → smoother but laggier

    # Total display→CPU→USB→game latency to pre-compensate (ms)
    latency_ms: float = 8.0

    # Minimum fill-pixel column fraction required to confirm a row is "filled"
    min_col_fraction: float = 0.25

    # Poll rate target (Hz); actual rate depends on capture backend
    target_hz: int = 240

    def roi_mss(self) -> dict[str, int]:
        """Convert to mss monitor dict."""
        l, t, r, b = self.roi
        return {"left": l, "top": t, "width": r - l, "height": b - t}


@dataclass
class MeterSnapshot:
    """Output from one detection frame."""
    fill_pct: float                 # 0.0 (empty) → 1.0 (full)
    velocity_pct_per_ms: float      # fill speed (% per ms); Kalman-smoothed
    fill_detected: bool             # fill bar found in ROI
    green_detected: bool            # fill currently in green zone
    timestamp: float                # time.perf_counter() of capture

    def predict_ms_to(self, target_pct: float) -> float:
        """
        Estimate ms until fill reaches target_pct.
        Returns inf if fill is not moving or already past target.
        """
        if not self.fill_detected or self.velocity_pct_per_ms <= 0.0:
            return float("inf")
        gap = target_pct - self.fill_pct
        if gap <= 0.0:
            return 0.0
        return gap / self.velocity_pct_per_ms


class KalmanTracker1D:
    """
    Constant-velocity 1D Kalman filter.
    State vector: [position, velocity]

    Adapted from Welch & Bishop (2006) "An Introduction to the Kalman Filter".
    Manual numpy — no external filterpy dependency.
    """

    def __init__(self, Q: float = 0.02, R: float = 4.0) -> None:
        self.Q_base = Q
        self.R = R
        self.x = np.array([0.0, 0.0])          # [pos, vel]
        self.P = np.eye(2) * 10.0              # initial covariance (high uncertainty)
        self._last_t: Optional[float] = None
        self._initialized = False

    def reset(self) -> None:
        self.x = np.array([0.0, 0.0])
        self.P = np.eye(2) * 10.0
        self._last_t = None
        self._initialized = False

    def update(self, measurement: float, t: float) -> tuple[float, float]:
        """
        Feed a new fill_pct measurement at time t (perf_counter seconds).
        Returns (smoothed_position, velocity_per_second).
        """
        if not self._initialized or self._last_t is None:
            self.x[0] = measurement
            self.x[1] = 0.0
            self._last_t = t
            self._initialized = True
            return float(self.x[0]), float(self.x[1])

        dt = t - self._last_t
        if dt <= 0.0:
            return float(self.x[0]), float(self.x[1])
        self._last_t = t

        # State transition
        F = np.array([[1.0, dt], [0.0, 1.0]])
        dt2, dt3, dt4 = dt * dt, dt ** 3, dt ** 4
        Q = np.array([
            [dt4 / 4, dt3 / 2],
            [dt3 / 2, dt2],
        ]) * self.Q_base

        # H is 1D so matrix products stay 1D (avoids shape-mismatch with (2,1) K)
        H = np.array([1.0, 0.0])

        x_pred = F @ self.x                          # (2,)
        P_pred = F @ self.P @ F.T + Q               # (2,2)

        innov = measurement - float(H @ x_pred)      # scalar
        S = float(H @ P_pred @ H) + self.R           # scalar
        K = (P_pred @ H) / S                         # (2,)

        self.x = x_pred + K * innov                  # (2,)
        self.P = (np.eye(2) - np.outer(K, H)) @ P_pred  # (2,2)

        return float(self.x[0]), float(self.x[1])

    @property
    def velocity_per_ms(self) -> float:
        """Kalman-smoothed fill velocity in % per millisecond."""
        return float(self.x[1]) / 1000.0   # convert per-sec → per-ms


class MeterDetector:
    """
    Captures the shot meter ROI and continuously tracks fill position.

    Thread-safe: call get_snapshot() from any thread.
    Call start() / stop() once per session.
    """

    def __init__(self, config: Optional[MeterConfig] = None) -> None:
        self._cfg = config or MeterConfig()
        self._lock = threading.Lock()
        self._snapshot = MeterSnapshot(
            fill_pct=0.0,
            velocity_pct_per_ms=0.0,
            fill_detected=False,
            green_detected=False,
            timestamp=time.perf_counter(),
        )
        self._kalman = KalmanTracker1D(Q=self._cfg.kalman_Q, R=self._cfg.kalman_R)
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Backend selection
        self._backend: str = "none"
        self._camera: Optional[object] = None
        self._mss_ctx: Optional[object] = None

    # ── Configuration (hot-swap safe) ─────────────────────────────────────────

    def update_config(self, cfg: MeterConfig) -> None:
        """Replace config while detector is running (thread-safe)."""
        with self._lock:
            self._cfg = cfg
            self._kalman = KalmanTracker1D(Q=cfg.kalman_Q, R=cfg.kalman_R)

    def get_config(self) -> MeterConfig:
        with self._lock:
            return self._cfg

    # ── Snapshot API ──────────────────────────────────────────────────────────

    def get_snapshot(self) -> MeterSnapshot:
        with self._lock:
            return self._snapshot

    @property
    def backend(self) -> str:
        return self._backend

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """
        Initialise capture backend and start the poll thread.
        Returns True if a working backend was found.
        """
        if not _CV2_OK:
            print("[MeterDetector] cv2 not installed — vision mode disabled.")
            return False

        if _DXCAM_OK:
            try:
                self._camera = dxcam.create(output_color="BGR")
                self._backend = "dxcam"
                print("[MeterDetector] Backend: dxcam (DirectX Desktop Duplication)")
            except Exception as exc:
                print(f"[MeterDetector] dxcam init failed ({exc}); trying mss…")

        if self._backend == "none" and _MSS_OK:
            try:
                self._mss_ctx = mss.mss()
                self._backend = "mss"
                print("[MeterDetector] Backend: mss (fallback)")
            except Exception as exc:
                print(f"[MeterDetector] mss init failed: {exc}")

        if self._backend == "none":
            print("[MeterDetector] No capture backend available — install dxcam or mss.")
            return False

        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, name="meter-detector", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        try:
            if self._camera is not None:
                del self._camera
            if self._mss_ctx is not None:
                self._mss_ctx.close()
        except Exception:
            pass

    # ── Capture ───────────────────────────────────────────────────────────────

    def _capture_bgr(self, cfg: MeterConfig) -> Optional[np.ndarray]:
        """Grab the ROI as a BGR numpy array. Returns None on failure."""
        if self._backend == "dxcam" and self._camera is not None:
            try:
                frame = self._camera.grab(region=cfg.roi)
                return frame  # already BGR, or None if no new frame
            except Exception:
                return None

        if self._backend == "mss" and self._mss_ctx is not None:
            try:
                mon = cfg.roi_mss()
                shot = self._mss_ctx.grab(mon)
                arr = np.array(shot)          # BGRA
                return arr[:, :, :3]          # drop alpha
            except Exception:
                return None

        return None

    # ── Detection ─────────────────────────────────────────────────────────────

    def _analyze(
        self, bgr: np.ndarray, cfg: MeterConfig
    ) -> tuple[float, bool, bool]:
        """
        Returns (fill_pct, fill_detected, green_detected).
        fill_pct: 0.0→1.0, fraction of meter bar that is filled.
        """
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        h, w = hsv.shape[:2]

        # ── Green zone detection ───────────────────────────────────────────
        g_lo = np.array([cfg.green_h_lo, cfg.green_s_lo, cfg.green_v_lo])
        g_hi = np.array([cfg.green_h_hi, 255, 255])
        green_mask = cv2.inRange(hsv, g_lo, g_hi)
        green_detected = bool(cv2.countNonZero(green_mask) > (h * w * 0.02))

        # ── Fill position detection (brightness-based) ─────────────────────
        v_channel = hsv[:, :, 2]
        bright_mask = v_channel > cfg.fill_v_threshold

        min_cols = max(1, int(w * cfg.min_col_fraction))
        row_counts = bright_mask.sum(axis=1)   # per-row bright pixel count
        fill_rows = np.where(row_counts >= min_cols)[0]

        if len(fill_rows) == 0:
            return 0.0, False, green_detected

        # Vertical meter: fill rises from bottom. Topmost bright row = fill top.
        topmost = int(fill_rows.min())
        fill_pct = (h - topmost) / h
        fill_pct = max(0.0, min(1.0, fill_pct))

        return fill_pct, True, green_detected

    # ── Poll loop ─────────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        interval = 1.0 / self._cfg.target_hz
        deadline = time.perf_counter()

        while self._running:
            deadline += interval
            now = time.perf_counter()
            sleep_for = deadline - now
            if sleep_for > 0.001:
                time.sleep(sleep_for - 0.0005)
            while time.perf_counter() < deadline:
                pass

            with self._lock:
                cfg = self._cfg

            bgr = self._capture_bgr(cfg)
            if bgr is None:
                continue

            t = time.perf_counter()
            fill_pct, fill_detected, green_detected = self._analyze(bgr, cfg)

            pos, vel_per_sec = self._kalman.update(fill_pct, t)
            vel_per_ms = vel_per_sec / 1000.0

            snap = MeterSnapshot(
                fill_pct=max(0.0, min(1.0, pos)),
                velocity_pct_per_ms=max(0.0, vel_per_ms),
                fill_detected=fill_detected,
                green_detected=green_detected,
                timestamp=t,
            )
            with self._lock:
                self._snapshot = snap
