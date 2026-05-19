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
        self._lock = threading.Lock()    # guards all vpad calls
        self._pad: Optional[object] = None
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

    # ── Shot button interface (X button) ─────────────────────────────────────

    def press_x(self) -> None:
        """Press X on the virtual controller (shot button)."""
        with self._lock:
            if self._pad and _VGAMEPAD_OK:
                try:
                    import vgamepad as vg
                    self._pad.press_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_X)
                    self._pad.update()
                except Exception as exc:
                    print(f"[VController] press_x error: {exc}")

    def release_x(self) -> None:
        """Release X on the virtual controller."""
        with self._lock:
            if self._pad and _VGAMEPAD_OK:
                try:
                    import vgamepad as vg
                    self._pad.release_button(button=vg.XUSB_BUTTON.XUSB_GAMEPAD_X)
                    self._pad.update()
                except Exception as exc:
                    print(f"[VController] release_x error: {exc}")

    # ── Passthrough ───────────────────────────────────────────────────────────

    def passthrough(
        self,
        snap: object,    # ControllerSnapshot (typed here to avoid circular import)
        override_x: bool = False,
        stick_noise_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        """
        Mirror physical controller state to virtual controller.

        override_x=True means the shot timer owns the X button — skip
        forwarding X from physical so early physical release can't interfere
        with the timed release.
        """
        if not self._pad:
            return

        def _n(v: float) -> float:
            if stick_noise_fn:
                return max(-1.0, min(1.0, v + stick_noise_fn()))
            return v

        with self._lock:
            try:
                # Incremental set/clear — no reset() to avoid 1-frame blank inputs.
                # Skip X button (0x4000) when shot timer owns it (override_x).
                for mask, btn in _BUTTON_MAP.items():
                    if override_x and mask == 0x4000:
                        continue   # shot timer controls X release timing
                    if snap.buttons & mask:  # type: ignore[attr-defined]
                        self._pad.press_button(button=btn)
                    else:
                        self._pad.release_button(button=btn)

                # ── Triggers (always forwarded — RT no longer the shot button)
                self._pad.left_trigger(value=int(snap.lt * 255))   # type: ignore
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
