"""
Green-Light Shot Timer for NBA 2K26 (offline, no EAC).

Intercepts the physical X button press, holds it on the virtual controller,
then schedules release at the statistically optimal green window using
QPC-precision timing + HBR ex-Gaussian jitter.

Flow:
  1. X button pressed (digital 0→1) → immediately hold X on vpad
  2. Timer thread counts down to release_ms (from shot profile)
  3. HBR jitter applied to release instant (ex-Gaussian, σ≈8 ms)
  4. If X released early → emergency release fires immediately
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .hbr import HumanButtonResponder, precise_sleep


@dataclass
class JumpShotProfile:
    """
    Per-player / per-animation shot timing configuration.

    animation_ms:    Total jump-shot animation length in milliseconds.
    green_start_pct: Fraction of animation where green window opens  (0.0–1.0).
    green_end_pct:   Fraction of animation where green window closes (0.0–1.0).
    aim_percentile:  Target point within [green_start, green_end] (0.0=start, 0.5=mid, 1.0=end).
    name:            Display label for the profile selector.
    """
    name: str
    animation_ms: float
    green_start_pct: float
    green_end_pct: float
    aim_percentile: float = 0.50

    def __post_init__(self) -> None:
        if not (0.0 < self.green_start_pct < self.green_end_pct <= 1.0):
            raise ValueError("green_start_pct must be < green_end_pct, both in (0,1]")
        if not (0.0 <= self.aim_percentile <= 1.0):
            raise ValueError("aim_percentile must be in [0.0, 1.0]")

    @property
    def release_ms(self) -> float:
        """Calculated optimal RT release time after press (ms)."""
        start = self.animation_ms * self.green_start_pct
        end   = self.animation_ms * self.green_end_pct
        return start + (end - start) * self.aim_percentile

    @property
    def green_window_ms(self) -> float:
        """Width of the green window in milliseconds."""
        return self.animation_ms * (self.green_end_pct - self.green_start_pct)


# ── Built-in profiles — calibrate via practice mode ──────────────────────────
PROFILES: dict[str, JumpShotProfile] = {
    "default": JumpShotProfile(
        name="default",
        animation_ms=800.0,
        green_start_pct=0.55,
        green_end_pct=0.65,
    ),
    "quick": JumpShotProfile(
        name="quick",
        animation_ms=640.0,
        green_start_pct=0.50,
        green_end_pct=0.60,
    ),
    "slow": JumpShotProfile(
        name="slow",
        animation_ms=960.0,
        green_start_pct=0.58,
        green_end_pct=0.68,
    ),
    "midrange": JumpShotProfile(
        name="midrange",
        animation_ms=720.0,
        green_start_pct=0.53,
        green_end_pct=0.63,
    ),
}


class ShotTimingEngine:
    """
    Monitors the Xbox X button (digital) and fires a timed release at the
    green window, using QPC busy-wait precision and HBR jitter.

    on_hold()    — called when X is pressed; caller presses X on virtual pad.
    on_release() — called at the computed green window; caller releases X.
    on_event(label) — optional notification callback (e.g. for web dashboard).
    """

    SHOOT_BUTTON: int = 0x4000   # BTN_X — digital, no threshold needed

    def __init__(
        self,
        profile: JumpShotProfile,
        hbr: HumanButtonResponder,
        on_hold: Callable[[], None],
        on_release: Callable[[], None],
        on_event: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._profile = profile
        self._hbr = hbr
        self._on_hold = on_hold
        self._on_release = on_release
        self._on_event = on_event

        self._lock = threading.Lock()
        self._shot_active = False
        self._shot_start: float = 0.0
        self._cancel_event = threading.Event()
        self._timer_thread: Optional[threading.Thread] = None

    def set_profile(self, profile: JumpShotProfile) -> None:
        with self._lock:
            self._profile = profile

    @property
    def shot_active(self) -> bool:
        with self._lock:
            return self._shot_active

    def on_snapshot(self, buttons: int) -> None:
        """
        Feed the current XInput button bitmask on each poll.
        Detects X-button rising and falling edges (digital — no threshold).
        Callbacks are invoked AFTER releasing self._lock to avoid lock-ordering
        issues with the HBR lock and the VirtualController lock.
        """
        shooting = bool(buttons & self.SHOOT_BUTTON)
        post_hold    = False
        post_label   = ""

        with self._lock:
            was_active = self._shot_active
            profile = self._profile

            if shooting and not was_active:
                # Rising edge — shot starts
                self._shot_active = True
                self._shot_start = time.perf_counter()
                self._cancel_event.clear()
                self._launch_timer(profile)
                post_hold  = True
                post_label = "SHOT ARMED"

            elif not shooting and was_active:
                # Falling edge before timer fired — emergency release
                self._shot_active = False
                self._cancel_event.set()
                post_label = "EARLY RELEASE"

        # Invoke callbacks outside the lock (VirtualController has its own lock)
        if post_hold:
            try:
                self._on_hold()
            except Exception as exc:
                print(f"[ShotTimer] hold callback error: {exc}")
        if post_label:
            self._notify(post_label)

    def _launch_timer(self, profile: JumpShotProfile) -> None:
        """Spawn a fire-and-forget thread targeting the green window."""
        jitter_ms = self._hbr.jitter_ms()
        shot_start = self._shot_start
        release_delay = (profile.release_ms + jitter_ms) / 1000.0

        t = threading.Thread(
            target=self._fire_at,
            args=(shot_start + release_delay,),
            daemon=True,
        )
        self._timer_thread = t
        t.start()

    def _fire_at(self, target_time: float) -> None:
        """
        Busy-sleep to target_time (QPC) then fire the release.
        Respects cancel_event for early-physical-release bail-out.
        """
        remaining = target_time - time.perf_counter()

        # Coarse OS sleep (leave 0.8 ms for busy-spin — fix #2: was 3 ms)
        coarse = remaining - 0.0008
        if coarse > 0.0:
            cancelled = self._cancel_event.wait(timeout=coarse)
            if cancelled:
                # Physical RT was released early — release virtual RT immediately
                try:
                    self._on_release()
                except Exception as exc:
                    print(f"[ShotTimer] early-release callback error: {exc}")
                self._notify("RELEASED (early)")
                return

        # Precision busy-spin for final milliseconds
        while time.perf_counter() < target_time:
            if self._cancel_event.is_set():
                self._on_release()
                self._notify("RELEASED (early)")
                return

        with self._lock:
            if not self._shot_active:
                return  # already handled by cancel path
            self._shot_active = False

        try:
            self._on_release()
        except Exception as exc:
            # Fix #5: callback errors must not kill the timer thread silently
            print(f"[ShotTimer] release callback error: {exc}")
        self._notify("GREEN RELEASE ✓")

    def _notify(self, label: str) -> None:
        if self._on_event is not None:
            try:
                self._on_event(label)
            except Exception:
                pass


# ── Tests ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    profile = PROFILES["default"]
    print(f"Profile: {profile.name}")
    print(f"  animation : {profile.animation_ms:.0f} ms")
    print(f"  green win : {profile.green_window_ms:.1f} ms wide")
    print(f"  release_ms: {profile.release_ms:.1f} ms after press")

    releases: list[float] = []

    from .hbr import HBRProfile, HumanButtonResponder
    hbr = HumanButtonResponder(HBRProfile())

    def _hold():  pass
    def _rel():   releases.append(time.perf_counter())

    engine = ShotTimingEngine(profile, hbr, _hold, _rel)

    N = 20
    for _ in range(N):
        press_t = time.perf_counter()
        engine.on_snapshot(ShotTimingEngine.SHOOT_BUTTON)  # simulate X pressed
        time.sleep(profile.animation_ms / 1000.0 * 1.2)
        if releases:
            elapsed = (releases[-1] - press_t) * 1000
            error   = elapsed - profile.release_ms
            print(f"  release at {elapsed:.1f} ms  (error {error:+.1f} ms)")
        releases.clear()
        time.sleep(0.1)

    print("Shot timer test complete.")
