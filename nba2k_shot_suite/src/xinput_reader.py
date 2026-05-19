"""
XInput state reader — raw ctypes binding to xinput1_4.dll with fallback chain.

Runs a dedicated polling thread at a configurable rate using time.perf_counter
(QPC-backed, ~100 ns resolution on Windows).  All state is published via a
thread-safe snapshot protected by RLock.  Callbacks fire only on packet change.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# ── XInput button bitmask constants ──────────────────────────────────────────
DPAD_UP   = 0x0001
DPAD_DOWN = 0x0002
DPAD_LEFT = 0x0004
DPAD_RIGHT = 0x0008
BTN_START = 0x0010
BTN_BACK  = 0x0020
BTN_LS    = 0x0040
BTN_RS    = 0x0080
BTN_LB    = 0x0100
BTN_RB    = 0x0200
BTN_A     = 0x1000
BTN_B     = 0x2000
BTN_X     = 0x4000
BTN_Y     = 0x8000

BUTTON_NAMES: dict[int, str] = {
    DPAD_UP:    "DPAD_UP",
    DPAD_DOWN:  "DPAD_DOWN",
    DPAD_LEFT:  "DPAD_LEFT",
    DPAD_RIGHT: "DPAD_RIGHT",
    BTN_START:  "START",
    BTN_BACK:   "BACK",
    BTN_LS:     "LS",
    BTN_RS:     "RS",
    BTN_LB:     "LB",
    BTN_RB:     "RB",
    BTN_A:      "A",
    BTN_B:      "B",
    BTN_X:      "X",
    BTN_Y:      "Y",
}

# XInput error code returned when no controller on that slot
_ERROR_DEVICE_NOT_CONNECTED = 0x48F


class _XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ("wButtons",      ctypes.wintypes.WORD),
        ("bLeftTrigger",  ctypes.wintypes.BYTE),
        ("bRightTrigger", ctypes.wintypes.BYTE),
        ("sThumbLX",      ctypes.c_short),
        ("sThumbLY",      ctypes.c_short),
        ("sThumbRX",      ctypes.c_short),
        ("sThumbRY",      ctypes.c_short),
    ]


class _XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPacketNumber", ctypes.wintypes.DWORD),
        ("Gamepad",        _XINPUT_GAMEPAD),
    ]


@dataclass(frozen=True)
class ControllerSnapshot:
    """Immutable, shareable controller state snapshot."""
    connected: bool
    packet: int
    buttons: int
    lt: float      # Left trigger  [0.0, 1.0]
    rt: float      # Right trigger [0.0, 1.0]
    lx: float      # Left stick X  [-1.0, 1.0]
    ly: float      # Left stick Y  [-1.0, 1.0]
    rx: float      # Right stick X [-1.0, 1.0]
    ry: float      # Right stick Y [-1.0, 1.0]

    def button_pressed(self, mask: int) -> bool:
        return bool(self.buttons & mask)

    def active_button_names(self) -> list[str]:
        return [name for mask, name in BUTTON_NAMES.items() if self.buttons & mask]


_DISCONNECTED_SNAP = ControllerSnapshot(
    connected=False, packet=0, buttons=0,
    lt=0.0, rt=0.0, lx=0.0, ly=0.0, rx=0.0, ry=0.0,
)


def _load_xinput() -> ctypes.WinDLL:
    """Load the best available XInput DLL with version fallback."""
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            dll = ctypes.WinDLL(name)
            # Verify XInputGetState is present
            _ = dll.XInputGetState
            return dll
        except (OSError, AttributeError):
            continue
    raise RuntimeError(
        "No XInput DLL found. Install DirectX Runtime or Visual C++ Redistributable."
    )


class XInputReader:
    """
    Polls a single XInput controller slot at a fixed rate using QPC-backed
    perf_counter for drift-free interval scheduling.

    The poll loop computes a fixed deadline and adjusts each iteration so
    scheduling jitter does not accumulate over time.
    """

    def __init__(
        self,
        controller_index: int = 0,
        poll_hz: int = 125,
        on_state_change: Optional[Callable[[ControllerSnapshot], None]] = None,
    ) -> None:
        if not (0 <= controller_index <= 3):
            raise ValueError("controller_index must be 0–3")
        if poll_hz < 1 or poll_hz > 1000:
            raise ValueError("poll_hz must be 1–1000")

        self._index = ctypes.wintypes.DWORD(controller_index)
        self._interval = 1.0 / poll_hz
        self._on_change = on_state_change

        self._xinput = _load_xinput()
        self._xinput.XInputGetState.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.POINTER(_XINPUT_STATE),
        ]
        self._xinput.XInputGetState.restype = ctypes.wintypes.DWORD

        self._lock = threading.RLock()
        self._snapshot: ControllerSnapshot = _DISCONNECTED_SNAP
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @property
    def snapshot(self) -> ControllerSnapshot:
        """Thread-safe current state."""
        with self._lock:
            return self._snapshot

    def start(self) -> None:
        """Begin polling in a daemon thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._poll_loop, name="xinput-poller", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 1.0) -> None:
        """Signal poll thread to stop and wait for it."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def _poll_loop(self) -> None:
        raw = _XINPUT_STATE()
        prev_packet: int = -1
        prev_connected: bool = False
        deadline = time.perf_counter()

        while self._running:
            deadline += self._interval
            sleep_for = deadline - time.perf_counter()
            if sleep_for > 0:
                time.sleep(sleep_for)

            result = self._xinput.XInputGetState(self._index, ctypes.byref(raw))
            connected = result != _ERROR_DEVICE_NOT_CONNECTED

            if not connected:
                snap = _DISCONNECTED_SNAP
                changed = prev_connected  # just disconnected
                prev_connected = False
            else:
                gp = raw.Gamepad
                snap = ControllerSnapshot(
                    connected=True,
                    packet=raw.dwPacketNumber,
                    buttons=gp.wButtons,
                    lt=gp.bLeftTrigger / 255.0,
                    rt=gp.bRightTrigger / 255.0,
                    lx=max(-1.0, gp.sThumbLX / 32767.0),
                    ly=max(-1.0, gp.sThumbLY / 32767.0),
                    rx=max(-1.0, gp.sThumbRX / 32767.0),
                    ry=max(-1.0, gp.sThumbRY / 32767.0),
                )
                changed = snap.packet != prev_packet or not prev_connected
                prev_packet = snap.packet
                prev_connected = True

            with self._lock:
                self._snapshot = snap

            if changed and self._on_change is not None:
                try:
                    self._on_change(snap)
                except Exception:
                    pass  # callback errors must not kill poll thread


# ── Self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    def _print_snap(s: ControllerSnapshot) -> None:
        if s.connected:
            print(f"[{s.packet:08d}] BTN={s.active_button_names()} "
                  f"LT={s.lt:.2f} RT={s.rt:.2f} "
                  f"LS=({s.lx:+.2f},{s.ly:+.2f}) RS=({s.rx:+.2f},{s.ry:+.2f})")
        else:
            print("Controller disconnected")

    reader = XInputReader(on_state_change=_print_snap)
    reader.start()
    print("Polling — press Ctrl+C to stop")
    try:
        while True:
            time.sleep(0.1)
    except KeyboardInterrupt:
        reader.stop()
        sys.exit(0)
