"""
NBA 2K26 Green Light Shot Suite — Launcher
==========================================
Wires all modules together:

  XInputReader  →  passthrough → VirtualController  (all buttons)
                →  ShotTimingEngine               (RT intercept)
                →  ControllerOverlay              (UI)

  ShotTimingEngine  →  HumanButtonResponder  →  VirtualController.hold/release_rt

Usage:
  python main.py [--profile default|quick|slow|midrange]
                 [--controller 0]
                 [--poll-hz 125]
                 [--alpha 0.90]

Controls (overlay window):
  Drag     — move the window
  Escape   — quit
  ✕ button — quit

  The overlay also has a profile selector (modify PROFILES or add --profile).

Game setup:
  1. Install ViGEmBus driver: https://github.com/nefarius/ViGEmBus/releases
  2. pip install vgamepad
  3. Run this script BEFORE launching 2K26
  4. In 2K26 controller settings, select controller slot 2 (the virtual one)
     — or whatever slot ViGEmBus assigns.
  5. Play normally; the suite intercepts RT timing automatically.

Shot calibration:
  Edit PROFILES in src/shot_timer.py to match your player's jump-shot
  animation length.  Use Practice mode to find the right timing window.
"""
from __future__ import annotations

import argparse
import sys
import threading
import tkinter as tk

from src.xinput_reader import XInputReader, ControllerSnapshot
from src.hbr import HumanButtonResponder, HBRProfile
from src.shot_timer import ShotTimingEngine, PROFILES, JumpShotProfile
from src.overlay import ControllerOverlay
from src.vcontroller import VirtualController


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NBA 2K26 Green Light Shot Suite")
    p.add_argument("--profile",    default="default", choices=list(PROFILES))
    p.add_argument("--controller", type=int, default=0, choices=[0, 1, 2, 3])
    p.add_argument("--poll-hz",    type=int, default=125)
    p.add_argument("--alpha",      type=float, default=0.90)
    p.add_argument("--display-only", action="store_true",
                   help="Run overlay + map only (no virtual controller output)")
    return p.parse_args()


class ShotSuite:
    """
    Top-level coordinator.  Owns all components and manages their lifecycle.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        profile = PROFILES[args.profile]

        # Components
        self._vpad   = VirtualController()
        self._hbr    = HumanButtonResponder(HBRProfile())

        self._engine = ShotTimingEngine(
            profile    = profile,
            hbr        = self._hbr,
            on_hold    = self._on_shot_hold,
            on_release = self._on_shot_release,
            on_event   = self._on_shot_event,
        )

        self._reader = XInputReader(
            controller_index = args.controller,
            poll_hz          = args.poll_hz,
            on_state_change  = self._on_state_change,
        )

        # UI (built later in run() on the main thread)
        self._overlay: ControllerOverlay | None = None
        self._root: tk.Tk | None = None

    # ── Callbacks (called from xinput poll thread) ────────────────────────────

    def _on_state_change(self, snap: ControllerSnapshot) -> None:
        """
        Called at every XInput packet change.  Runs in the poll thread.

        Order matters:
          1. Feed shot engine (may arm/cancel timer thread)
          2. Passthrough non-RT inputs to virtual controller
          3. Update overlay (posts to main thread via after(0))
        """
        shot_active = snap.rt >= ShotTimingEngine.SHOOT_THRESHOLD

        # 1 — shot engine
        self._engine.on_snapshot(snap.rt)

        # 2 — passthrough (RT overridden when shot is being timed)
        if not self._args.display_only:
            self._vpad.passthrough(
                snap,
                override_rt    = shot_active,
                stick_noise_fn = self._hbr.stick_drift,
            )

        # 3 — overlay
        if self._overlay is not None:
            self._overlay.update_snapshot(snap)

    def _on_shot_hold(self) -> None:
        """Shot engine detected RT press — hold RT on virtual controller."""
        if not self._args.display_only:
            self._vpad.hold_rt(1.0)

    def _on_shot_release(self) -> None:
        """Shot engine fires release at green window."""
        if not self._args.display_only:
            self._vpad.release_rt()

    def _on_shot_event(self, label: str) -> None:
        """Display shot timing event in overlay."""
        if self._overlay is not None:
            self._overlay.flash_event(label)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Build UI on main thread, start polling, enter tkinter main loop."""
        self._root = tk.Tk()
        self._root.wm_attributes("-alpha", self._args.alpha)

        self._overlay = ControllerOverlay(self._root)
        self._overlay.set_profile_label(self._args.profile)

        # Position overlay in top-right corner
        sw = self._root.winfo_screenwidth()
        self._root.geometry(f"+{sw - ControllerOverlay.W - 20}+30")

        # Wire profile selector (simple key bindings: 1–4)
        profile_keys = list(PROFILES.keys())
        for i, name in enumerate(profile_keys):
            key = str(i + 1)
            self._root.bind(key, lambda _e, n=name: self._switch_profile(n))

        # Start polling AFTER UI is ready
        self._reader.start()

        self._print_banner()

        try:
            self._root.mainloop()
        finally:
            self._cleanup()

    def _switch_profile(self, name: str) -> None:
        profile = PROFILES[name]
        self._engine.set_profile(profile)
        if self._overlay:
            self._overlay.set_profile_label(name)
        print(f"[Suite] Profile → {name}  (release @ {profile.release_ms:.0f} ms)")

    def _cleanup(self) -> None:
        self._reader.stop()
        self._vpad.reset()

    def _print_banner(self) -> None:
        profile = PROFILES[self._args.profile]
        mode = "DISPLAY ONLY" if self._args.display_only else "ACTIVE"
        print(
            f"\n{'═'*52}\n"
            f"  NBA 2K26 Green Light Shot Suite  [{mode}]\n"
            f"{'─'*52}\n"
            f"  Controller slot : {self._args.controller}\n"
            f"  Poll rate       : {self._args.poll_hz} Hz\n"
            f"  Profile         : {profile.name}\n"
            f"  Animation       : {profile.animation_ms:.0f} ms\n"
            f"  Release target  : {profile.release_ms:.0f} ms after press\n"
            f"  Green window    : {profile.green_window_ms:.0f} ms wide\n"
            f"  Virtual pad     : {'connected' if self._vpad.available else 'NOT AVAILABLE'}\n"
            f"{'─'*52}\n"
            f"  Keys 1–{len(PROFILES)}: switch profile   Escape/✕: quit\n"
            f"{'═'*52}\n"
        )


def main() -> None:
    args = parse_args()
    suite = ShotSuite(args)
    suite.run()


if __name__ == "__main__":
    main()
