"""
NBA 2K26 Green Light Shot Suite — Launcher
==========================================
Wires all modules together and serves the web dashboard.

  XInputReader  →  passthrough → VirtualController  (all buttons)
                →  ShotTimingEngine               (X button intercept)
                →  web_server.push_state()        (live dashboard)

  ShotTimingEngine  →  HBR.jitter_ms()  →  VirtualController.press_x/release_x

Usage
─────
  pip install fastapi "uvicorn[standard]" vgamepad pywin32
  python main.py [--profile default|quick|slow|midrange]
                 [--controller 0]
                 [--poll-hz 125]
                 [--port 8420]
                 [--display-only]

  Then open http://localhost:8420 in your browser.

Game setup
──────────
  1. Install ViGEmBus: https://github.com/nefarius/ViGEmBus/releases
  2. Run this script BEFORE launching NBA 2K26.
  3. In 2K26 controller settings, select the ViGEmBus virtual controller slot.
  4. Press X to shoot — the suite intercepts and optimises the release timing.

Shot calibration
────────────────
  Open the dashboard → Settings panel → adjust Animation ms, Green Start/End %.
  Changes apply live and persist to config.json.
  Use Practice mode to find the correct animation length for your player.
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from src.xinput_reader import XInputReader, ControllerSnapshot, BTN_X
from src.hbr import HumanButtonResponder, HBRProfile
from src.shot_timer import ShotTimingEngine, PROFILES, JumpShotProfile
from src.vcontroller import VirtualController
from src.config_manager import ConfigManager
from src.web_server import push_state, start_web_server

_CONFIG_PATH = Path(__file__).parent / "config.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NBA 2K26 Green Light Shot Suite")
    p.add_argument("--profile",      default="default", choices=list(PROFILES))
    p.add_argument("--controller",   type=int, default=0, choices=[0, 1, 2, 3])
    p.add_argument("--poll-hz",      type=int, default=125)
    p.add_argument("--port",         type=int, default=8420)
    p.add_argument("--display-only", action="store_true",
                   help="Overlay + dashboard only — no virtual controller output")
    return p.parse_args()


class ShotSuite:
    """Top-level coordinator — owns all components and their lifecycle."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        self._stop = threading.Event()
        self._last_event: str = ""

        # Config (loads config.json if present, else uses defaults)
        self._config = ConfigManager(_CONFIG_PATH)

        # Apply startup profile from CLI arg (overrides config.json active_profile)
        initial_profile = PROFILES[args.profile]

        # Components
        self._vpad   = VirtualController()
        self._hbr    = HumanButtonResponder(HBRProfile())
        self._engine = ShotTimingEngine(
            profile    = initial_profile,
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

        # Register live objects with config manager so dashboard can apply changes
        self._config.register(self._hbr, self._engine)

        # Sync config manager state with initial profile
        self._config.apply_dict({
            "active_profile":   args.profile,
            "animation_ms":     initial_profile.animation_ms,
            "green_start_pct":  initial_profile.green_start_pct,
            "green_end_pct":    initial_profile.green_end_pct,
            "aim_percentile":   initial_profile.aim_percentile,
        })

    # ── State dict for dashboard ──────────────────────────────────────────────

    def current_state_dict(self) -> dict[str, Any]:
        snap = self._reader.snapshot
        cfg  = self._config.get()
        return {
            "connected":       snap.connected,
            "buttons":         snap.buttons,
            "lt":              round(snap.lt, 3),
            "rt":              round(snap.rt, 3),
            "lx":              round(snap.lx, 3),
            "ly":              round(snap.ly, 3),
            "rx":              round(snap.rx, 3),
            "ry":              round(snap.ry, 3),
            "shot_active":     self._engine.shot_active,
            "current_profile": cfg.active_profile,
            "vpad_available":  self._vpad.available,
            "event":           "",
        }

    # ── XInput callbacks (poll thread) ────────────────────────────────────────

    def _on_state_change(self, snap: ControllerSnapshot) -> None:
        """
        Called on every XInput packet change from the poll thread.
        Order: shot engine → passthrough → web push.
        """
        shot_active = bool(snap.buttons & ShotTimingEngine.SHOOT_BUTTON)

        # 1 — shot engine (X button digital)
        self._engine.on_snapshot(snap.buttons)

        # 2 — passthrough (X button excluded when shot timer owns it)
        if not self._args.display_only:
            self._vpad.passthrough(
                snap,
                override_x     = shot_active,
                stick_noise_fn = self._hbr.stick_drift,
            )

        # 3 — web dashboard push (event field populated by _on_shot_event)
        event, self._last_event = self._last_event, ""
        state = self.current_state_dict()
        state["event"] = event
        push_state(state)

    # ── Shot engine callbacks ─────────────────────────────────────────────────

    def _on_shot_hold(self) -> None:
        if not self._args.display_only:
            self._vpad.press_x()

    def _on_shot_release(self) -> None:
        if not self._args.display_only:
            self._vpad.release_x()

    def _on_shot_event(self, label: str) -> None:
        self._last_event = label

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        # Start web dashboard (blocks until uvicorn is bound)
        start_web_server(
            config_mgr = self._config,
            suite      = self,
            host       = "127.0.0.1",
            port       = self._args.port,
        )

        # Start XInput polling
        self._reader.start()
        self._print_banner()

        # Handle Ctrl+C gracefully
        signal.signal(signal.SIGINT, self._handle_sigint)

        try:
            self._stop.wait()   # block main thread until stop is signalled
        finally:
            self._cleanup()

    def _handle_sigint(self, *_: Any) -> None:
        print("\n[Suite] Stopping…")
        self._stop.set()

    def _cleanup(self) -> None:
        self._reader.stop()
        self._vpad.reset()
        print("[Suite] Stopped.")

    def _print_banner(self) -> None:
        cfg  = self._config.get()
        mode = "DISPLAY ONLY" if self._args.display_only else "ACTIVE"
        p    = PROFILES.get(cfg.active_profile, list(PROFILES.values())[0])
        print(
            f"\n{'═'*54}\n"
            f"  NBA 2K26 Green Light Shot Suite  [{mode}]\n"
            f"{'─'*54}\n"
            f"  Controller slot  : {self._args.controller}\n"
            f"  Poll rate        : {self._args.poll_hz} Hz\n"
            f"  Shot button      : X (digital)\n"
            f"  Profile          : {cfg.active_profile}\n"
            f"  Animation        : {cfg.animation_ms:.0f} ms\n"
            f"  Release target   : {p.release_ms:.0f} ms after X press\n"
            f"  Green window     : {p.green_window_ms:.0f} ms wide\n"
            f"  Virtual pad      : {'connected' if self._vpad.available else 'NOT AVAILABLE'}\n"
            f"  Dashboard        : http://127.0.0.1:{self._args.port}\n"
            f"{'─'*54}\n"
            f"  Ctrl+C to quit\n"
            f"{'═'*54}\n"
        )


def main() -> None:
    args = parse_args()
    ShotSuite(args).run()


if __name__ == "__main__":
    main()
