"""
Human Button Responder (HBR) — ex-Gaussian motor-variance timing engine.

Injects statistically calibrated noise into all virtual controller outputs so
that the timing distribution is indistinguishable from a practiced human player
at the driver level.  Based on the Wing-Kristofferson two-stage model and the
ex-Gaussian reaction-time distribution (Normal + Exponential tail).

Reference: Lindeløe (2019) "Reaction Time Distributions" — σ≈8 ms, τ≈4 ms
for anticipatory single-button press by a practiced performer.
"""
from __future__ import annotations

import random
import time
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class HBRProfile:
    """
    Motor-variance parameters for a single-button anticipatory press.
    All time values are in milliseconds unless noted.
    """
    # Ex-Gaussian jitter applied to release timing
    press_sigma_ms: float = 8.0    # Gaussian SD component (practiced player)
    press_tau_ms: float   = 4.0    # Exponential tail (rare late errors)

    # Trigger pressure ramp (physical triggers don't snap 0→1 instantly)
    ramp_steps: int       = 5      # number of intermediate values
    ramp_exponent: float  = 2.4    # power-function curve shape (>1 = ease-in)

    # Deadzone drift (sticks have subtle noise at rest from motor tremor)
    stick_noise_sigma: float = 0.011   # ~1.1% of full axis range

    # Tap hold duration
    hold_base_ms: float  = 52.0
    hold_sigma_ms: float = 7.0


def _ex_gaussian(sigma: float, tau: float) -> float:
    """
    Draw one sample from Ex-Gaussian(0, sigma, tau).
    Mean is 0 (caller adds their own offset); distribution has a positive tail.
    """
    gauss = random.gauss(0.0, sigma)
    expo  = random.expovariate(1.0 / tau) if tau > 0.0 else 0.0
    return gauss + expo


def _power_ramp(step: int, total: int, exp: float) -> float:
    """Ease-in power ramp: f(t) = t^exp, t in (0,1]."""
    t = (step + 1) / total
    return t ** exp


def precise_sleep(seconds: float) -> None:
    """
    High-resolution sleep: coarse OS sleep for most of the interval, then
    busy-spin for only the final 0.8 ms (fix #4: was 1.5 ms unconditionally;
    now only spins when remaining gap justifies it, reducing CPU waste).
    """
    if seconds <= 0.0002:   # sub-0.2ms — not worth sleeping at all
        return
    deadline = time.perf_counter() + seconds
    coarse = seconds - 0.0008   # leave 0.8ms for busy-spin
    if coarse > 0.0:
        time.sleep(coarse)
    while time.perf_counter() < deadline:
        pass


class HumanButtonResponder:
    """
    Wraps a virtual-controller dispatch function and adds ex-Gaussian jitter,
    trigger ramps, and deadzone drift to every output.

    dispatch_fn(action: str, value: float) — caller-supplied;
    action is a free-form string e.g. "RT", "A_press", "LX".
    """

    def __init__(
        self,
        profile: Optional[HBRProfile] = None,
        dispatch_fn: Optional[Callable[[str, float], None]] = None,
    ) -> None:
        self._profile = profile or HBRProfile()
        self._dispatch_fn = dispatch_fn
        self._lock = threading.Lock()

    def set_dispatch(self, fn: Callable[[str, float], None]) -> None:
        with self._lock:
            self._dispatch_fn = fn

    # ── Public timing helpers ─────────────────────────────────────────────────

    def jitter_ms(self) -> float:
        """One ex-Gaussian jitter sample in milliseconds (always >= 0)."""
        p = self._profile
        return max(0.0, _ex_gaussian(p.press_sigma_ms, p.press_tau_ms))

    def hold_ms(self) -> float:
        """Randomised tap hold duration in milliseconds."""
        p = self._profile
        return max(15.0, random.gauss(p.hold_base_ms, p.hold_sigma_ms))

    def stick_drift(self) -> float:
        """Tiny Gaussian noise for an analog axis at rest."""
        return random.gauss(0.0, self._profile.stick_noise_sigma)

    def trigger_ramp(self, target: float) -> list[float]:
        """
        Non-linear pressure ramp from 0.0 → target over ramp_steps values.
        Simulates a human's thumb depressing the trigger with a natural curve.
        """
        p = self._profile
        return [_power_ramp(i, p.ramp_steps, p.ramp_exponent) * target
                for i in range(p.ramp_steps)]

    # ── Dispatch helpers ──────────────────────────────────────────────────────

    def _dispatch(self, action: str, value: float) -> None:
        with self._lock:
            fn = self._dispatch_fn
        if fn is not None:
            fn(action, value)

    def tap(
        self,
        action: str,
        press_value: float = 1.0,
        release_value: float = 0.0,
        pre_delay_ms: float = 0.0,
    ) -> None:
        """
        Press then release `action` with HBR timing.  Blocks until release.
        Run in a thread if non-blocking behaviour is needed.
        """
        jitter = self.jitter_ms()
        total_pre = (pre_delay_ms + jitter) / 1000.0
        hold     = self.hold_ms() / 1000.0

        if total_pre > 0.0:
            precise_sleep(total_pre)

        self._dispatch(action, press_value)
        precise_sleep(hold)
        self._dispatch(action, release_value)

    def ramp_trigger_action(
        self,
        action: str,
        target: float,
        step_delay_ms: float = 3.5,
    ) -> None:
        """
        Send a non-linear trigger ramp for `action`.  Blocks until complete.
        """
        for value in self.trigger_ramp(target):
            self._dispatch(action, value)
            precise_sleep(step_delay_ms / 1000.0)


# ── Tests ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import statistics

    profile = HBRProfile()
    hbr = HumanButtonResponder(profile)

    samples = [hbr.jitter_ms() for _ in range(10_000)]
    print(f"Ex-Gaussian jitter — n=10000")
    print(f"  mean : {statistics.mean(samples):.2f} ms")
    print(f"  stdev: {statistics.stdev(samples):.2f} ms")
    print(f"  min  : {min(samples):.2f} ms")
    print(f"  max  : {max(samples):.2f} ms")

    ramp = hbr.trigger_ramp(1.0)
    print(f"\nTrigger ramp ({profile.ramp_steps} steps): {[f'{v:.3f}' for v in ramp]}")

    # Timing precision test
    log: list[float] = []
    hbr.set_dispatch(lambda a, v: log.append(time.perf_counter()))

    target_ms = 50.0
    t0 = time.perf_counter()
    hbr.tap("TEST", pre_delay_ms=target_ms)
    elapsed = (log[0] - t0) * 1000 if log else float("nan")
    print(f"\nTap dispatch latency (target={target_ms} ms): {elapsed:.2f} ms")
    assert abs(elapsed - target_ms) < 30, "Timing error > 30 ms — check perf_counter"
    print("All tests passed.")
