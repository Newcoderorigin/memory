"""
Vision-based shot release controller for NBA 2K26.

ShotMeterController replaces the fixed-timer release logic when vision
mode is active.  It reads MeterDetector snapshots in a tight loop,
applies Kalman-predicted timing, and fires the virtual X release at the
instant that puts the fill bar in the center of the green window.

Release strategy:
  1. Poll MeterDetector until fill is detected.
  2. Each frame: predict ms until fill reaches green_center_pct.
  3. When predicted_ms ≤ latency_ms, fire release immediately.
  4. If fill is not detected within detect_timeout_ms, fall back to
     profile-based timing (animation_ms × aim_percentile after press).

The controller is single-fire per arm() call.  Call cancel() if the
physical X button was released early (ShotTimingEngine will do this).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .meter_detector import MeterDetector, MeterSnapshot
from .hbr import HumanButtonResponder, precise_sleep


class ShotMeterController:
    """
    Vision-based release controller.

    on_release_fn  — callable to fire (wraps vpad.release_x).
    detector       — shared MeterDetector instance.
    hbr            — used to add ex-Gaussian jitter to vision-based release.
    """

    def __init__(
        self,
        detector: MeterDetector,
        on_release_fn: Callable[[], None],
        hbr: Optional[HumanButtonResponder] = None,
        detect_timeout_ms: float = 200.0,
    ) -> None:
        self._detector = detector
        self._on_release = on_release_fn
        self._hbr = hbr
        self._detect_timeout_ms = detect_timeout_ms

        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._fired = False
        self._armed = False
        self._thread: Optional[threading.Thread] = None

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def is_armed(self) -> bool:
        with self._lock:
            return self._armed and not self._fired

    def arm(
        self,
        shot_start: float,
        green_start_pct: float,
        green_end_pct: float,
        animation_ms: float,
        aim_percentile: float,
    ) -> None:
        """
        Called immediately when X is pressed.  Starts the vision release thread.

        shot_start       — time.perf_counter() at the moment X was pressed.
        green_start_pct  — green window open  (fraction of animation, 0–1).
        green_end_pct    — green window close (fraction of animation, 0–1).
        animation_ms     — total shot animation length.
        aim_percentile   — target point within green window (0=start, 0.5=mid, 1=end).
        """
        with self._lock:
            self._fired = False
            self._armed = True
        self._cancel.clear()

        cfg = self._detector.get_config()
        green_center_pct = green_start_pct + (green_end_pct - green_start_pct) * aim_percentile
        fallback_ms = animation_ms * (
            green_start_pct + (green_end_pct - green_start_pct) * aim_percentile
        )

        self._thread = threading.Thread(
            target=self._release_thread,
            args=(shot_start, green_center_pct, fallback_ms, cfg.latency_ms),
            daemon=True,
            name="shot-meter-release",
        )
        self._thread.start()

    def cancel(self) -> None:
        """
        X was released early (physical button up before timer fired).
        Abort vision thread and fire release immediately (once only).
        """
        self._cancel.set()
        with self._lock:
            if self._fired:
                return
            self._fired = True
            self._armed = False

        try:
            self._on_release()
        except Exception as exc:
            print(f"[ShotMeter] cancel release error: {exc}")

    # ── Release thread ────────────────────────────────────────────────────────

    def _release_thread(
        self,
        shot_start: float,
        green_center_pct: float,
        fallback_ms: float,
        latency_ms: float,
    ) -> None:
        """
        Polls MeterDetector until the optimal release moment.
        Falls back to timing-based release if meter is not detected.
        """
        detect_deadline = shot_start + self._detect_timeout_ms / 1000.0
        fallback_deadline = shot_start + (fallback_ms + (self._hbr.jitter_ms() if self._hbr else 0.0)) / 1000.0

        snap: Optional[MeterSnapshot] = None

        # Phase 1: Wait for fill detection (or timeout / cancel)
        while time.perf_counter() < detect_deadline:
            if self._cancel.is_set():
                return  # cancel() already fired the release

            snap = self._detector.get_snapshot()
            if snap.fill_detected:
                break
            time.sleep(0.001)   # 1 ms poll — detector runs at 240 Hz

        if not (snap and snap.fill_detected):
            # Meter not detected — fall back to timing
            self._fire_at(fallback_deadline)
            return

        # Phase 2: Predictive release loop
        while True:
            if self._cancel.is_set():
                return

            snap = self._detector.get_snapshot()

            if snap.fill_detected:
                ms_to_target = snap.predict_ms_to(green_center_pct)
                if ms_to_target <= latency_ms:
                    # Target is within the latency window — fire now
                    self._fire()
                    return
            else:
                # Lost detection — fall back immediately to timing
                self._fire_at(fallback_deadline)
                return

            # Check fallback deadline (prevents infinite wait if velocity → 0)
            if time.perf_counter() >= fallback_deadline:
                self._fire()
                return

            time.sleep(0.001)   # 1 ms poll

    def _fire_at(self, target_time: float) -> None:
        """Busy-wait to target_time then fire release."""
        remaining = target_time - time.perf_counter()
        if remaining > 0.001:
            precise_sleep(remaining)
        self._fire()

    def _fire(self) -> None:
        """Single-fire release guard."""
        with self._lock:
            if self._fired:
                return
            self._fired = True
            self._armed = False

        if self._cancel.is_set():
            return   # cancel() beat us — don't double-release

        try:
            self._on_release()
        except Exception as exc:
            print(f"[ShotMeter] release error: {exc}")
