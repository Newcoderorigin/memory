"""
Virtual Xbox 360 controller via vgamepad (ViGEmBus kernel driver).

Provides a thread-safe wrapper around VX360Gamepad that:
  - Forwards all physical controller state (passthrough mode)
  - Allows individual axis/button overrides for shot timing
  - Gracefully degrades to a display-only no-op if vgamepad is unavailable

Install: pip install vgamepad
ViGEmBus driver: https://github.com/nefarius/ViGEmBus/releases
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

try:
    import vgamepad as vg

    # Map XInput bitmasks → XUSB_BUTTON enum values
    _BUTTON_MAP: dict[int, "vg.XUSB_BUTTON"] = {
        0x0001: vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_UP,
        0x0002: vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_DOWN,
        0x0004: vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_LEFT,
        0x0008: vg.XUSB_BUTTON.XUSB_GAMEPAD_DPAD_RIGHT,
        0x0010: vg.XUSB_BUTTON.XUSB_GAMEPAD_START,
        0x0020: vg.XUSB_BUTTON.XUSB_GAMEPAD_BACK,
        0x0040: vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_THUMB,
        0x0080: vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_THUMB,
        0x0100: vg.XUSB_BUTTON.XUSB_GAMEPAD_LEFT_SHOULDER,
        0x0200: vg.XUSB_BUTTON.XUSB_GAMEPAD_RIGHT_SHOULDER,
        0x1000: vg.XUSB_BUTTON.XUSB_GAMEPAD_A,
        0x2000: vg.XUSB_BUTTON.XUSB_GAMEPAD_B,
        0x4000: vg.XUSB_BUTTON.XUSB_GAMEPAD_X,
        0x8000: vg.XUSB_BUTTON.XUSB_GAMEPAD_Y,
    }
    _VGAMEPAD_OK = True
except ImportError:
    _VGAMEPAD_OK = False
    _BUTTON_MAP = {}

# RT bitmask — we intercept this for shot timing
_RT_THRESHOLD_BYTE = 217  # ≈ 0.85 × 255


class VirtualController:
    """
    Thread-safe virtual Xbox 360 controller.

    Uses two separate locks so the shot timer can release RT without
    contending with the full passthrough lock (fix #7: avoids up to 8 ms
    of release latency caused by lock contention).

    The shot timing engine controls RT independently via hold_rt() / release_rt().
    All other inputs are mirrored from the physical controller via passthrough().
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()      # guards buttons + sticks + LT
        self._rt_lock = threading.Lock()   # guards RT only (held briefly)
        self._pad: Optional[object] = None
        self._rt_held: bool = False
        self._error_count: int = 0

        if not _VGAMEPAD_OK:
            print(
                "[VController] vgamepad not installed — running in display-only mode.\n"
                "  Install: pip install vgamepad\n"
                "  Driver : https://github.com/nefarius/ViGEmBus/releases"
            )
            return

        try:
            import vgamepad as vg
            self._pad = vg.VX360Gamepad()
            self._pad.reset()
            self._pad.update()
            print("[VController] ViGEmBus virtual controller connected.")
        except Exception as exc:
            print(f"[VController] ViGEmBus init failed: {exc}")
            self._pad = None

    @property
    def available(self) -> bool:
        return self._pad is not None

    # ── Shot timing interface ─────────────────────────────────────────────────

    def hold_rt(self, value: float = 1.0) -> None:
        """Press RT on the virtual controller (0.0–1.0)."""
        byte_val = min(255, int(value * 255))
        with self._rt_lock:   # RT-only lock — does not block passthrough
            if self._pad:
                self._pad.right_trigger(value=byte_val)
                self._pad.update()
                self._rt_held = True

    def release_rt(self) -> None:
        """Release RT on the virtual controller (fix #7: RT-only lock)."""
        with self._rt_lock:   # RT-only lock — minimal contention with passthrough
            if self._pad:
                self._pad.right_trigger(value=0)
                self._pad.update()
                self._rt_held = False

    # ── Passthrough ───────────────────────────────────────────────────────────

    def passthrough(
        self,
        snap: object,    # ControllerSnapshot (typed here to avoid circular import)
        override_rt: bool = False,
        stick_noise_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        """
        Mirror physical controller state to virtual controller.

        override_rt=True means the shot timer owns RT — do NOT forward RT
        from the physical controller (prevents early release from interfering).
        """
        if not self._pad:
            return

        def _n(v: float) -> float:
            if stick_noise_fn:
                return max(-1.0, min(1.0, v + stick_noise_fn()))
            return v

        with self._lock:
            try:
                # Fix #1: incremental set/clear instead of reset() to avoid
                # the 1-frame blank input that reset() causes each call.
                for mask, btn in _BUTTON_MAP.items():
                    if snap.buttons & mask:  # type: ignore[attr-defined]
                        self._pad.press_button(button=btn)
                    else:
                        self._pad.release_button(button=btn)

                # ── Triggers ─────────────────────────────────────────────────
                self._pad.left_trigger(value=int(snap.lt * 255))  # type: ignore
                if not override_rt:
                    # RT-only lock is separate; acquire briefly to set value
                    with self._rt_lock:
                        self._pad.right_trigger(value=int(snap.rt * 255))  # type: ignore

                # ── Sticks with optional deadzone noise ───────────────────────
                self._pad.left_joystick(
                    x_value=int(_n(snap.lx) * 32767),  # type: ignore
                    y_value=int(_n(snap.ly) * 32767),  # type: ignore
                )
                self._pad.right_joystick(
                    x_value=int(_n(snap.rx) * 32767),  # type: ignore
                    y_value=int(_n(snap.ry) * 32767),  # type: ignore
                )
                self._pad.update()
                self._error_count = 0
            except Exception as exc:
                # Fix #6: surface persistent ViGEmBus errors every 10 failures
                self._error_count += 1
                if self._error_count % 10 == 1:
                    print(f"[VController] passthrough error (×{self._error_count}): {exc}")

    def reset(self) -> None:
        with self._lock:
            if self._pad:
                try:
                    self._pad.reset()
                    self._pad.update()
                except Exception:
                    pass
