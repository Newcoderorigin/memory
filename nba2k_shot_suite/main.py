"""
NBA 2K26 Green Light Shot Suite — Launcher
==========================================
Wires all modules together and serves the web dashboard.

  XInputReader  →  passthrough → VirtualController  (all buttons)
                →  ShotTimingEngine               (X button intercept)
                →  web_server.push_state()        (live dashboard)

  ShotTimingEngine  →  HBR.jitter_ms()  →  VirtualController.press_x/release_x

  ScreenCapture  →  MeterDetector  →  GameOverlay  (live detection feedback)
  ShotTimingEngine + MeterDetector  →  AdaptiveTimingLearner  (self-tuning)

Usage
─────
  pip install -r requirements.txt
  python main.py [--profile default|quick|slow|midrange]
                 [--controller 0]
                 [--poll-hz 125]
                 [--port 8420]
                 [--display-only]
                 [--no-overlay]
                 [--debug-cv]

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
  The learner adapts aim_percentile automatically from screen-detected outcomes.
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from src.xinput_reader import XInputReader, ControllerSnapshot, BTN_X
from src.hbr          import HumanButtonResponder, HBRProfile
from src.shot_timer   import ShotTimingEngine, PROFILES, JumpShotProfile
from src.vcontroller  import VirtualController
from src.config_manager import ConfigManager
from src.web_server   import push_state, start_web_server
from src.screen_capture import ScreenCapture
from src.meter_detector import MeterDetector, DetectionResult
from src.shot_learner   import AdaptiveTimingLearner

_CONFIG_PATH  = Path(__file__).parent / "config.json"
_LEARNER_PATH = Path(__file__).parent / "learner.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NBA 2K26 Green Light Shot Suite")
    p.add_argument("--profile",      default="default", choices=list(PROFILES))
    p.add_argument("--controller",   type=int, default=0, choices=[0, 1, 2, 3])
    p.add_argument("--poll-hz",      type=int, default=125)
    p.add_argument("--port",         type=int, default=8420)
    p.add_argument("--display-only", action="store_true",
                   help="Overlay + dashboard only — no virtual controller output")
    p.add_argument("--no-overlay",   action="store_true",
                   help="Disable the in-game GameOverlay window")
    p.add_argument("--debug-cv",     action="store_true",
                   help="Show OpenCV debug visualisation in console (dev only)")
    return p.parse_args()


class ShotSuite:
    """Top-level coordinator — owns all components and their lifecycle."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        self._stop = threading.Event()
        self._last_event: str = ""

        # ── Config ────────────────────────────────────────────────────────────
        self._config = ConfigManager(_CONFIG_PATH)
        initial_profile = PROFILES[args.profile]

        # ── Core shot components ──────────────────────────────────────────────
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

        # ── Vision + learning ─────────────────────────────────────────────────
        self._capture  = ScreenCapture()
        self._detector = MeterDetector(
            capture    = self._capture,
            debug      = args.debug_cv,
            on_result  = self._on_detection,
        )
        self._learner  = AdaptiveTimingLearner(save_path=_LEARNER_PATH)

        # Shot tracking — records aim_pct used per shot for learner feedback
        self._shot_aim_pct: float = initial_profile.aim_percentile
        self._shot_fired   = threading.Event()

        # ── Config wiring ─────────────────────────────────────────────────────
        self._config.register(self._hbr, self._engine)
        self._config.apply_dict({
            "active_profile":  args.profile,
            "animation_ms":    initial_profile.animation_ms,
            "green_start_pct": initial_profile.green_start_pct,
            "green_end_pct":   initial_profile.green_end_pct,
            "aim_percentile":  initial_profile.aim_percentile,
        })

        # ── Overlay (optional) ────────────────────────────────────────────────
        self._overlay: Optional[Any] = None

    # ── State dict for dashboard ──────────────────────────────────────────────

    def current_state_dict(self) -> dict[str, Any]:
        snap = self._reader.snapshot
        cfg  = self._config.get()
        det  = self._detector.latest
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
            # Vision
            "meter_found":          det.meter_found,
            "green_window_visible": det.green_window_visible,
            "fill_pct":             round(det.fill_pct, 3),
            "green_window_pct":     round(det.green_window_pct, 3),
            "outcome_detected":     det.outcome_detected,
            "cv_confidence":        round(det.confidence, 3),
            # Learner
            "learner_mu":        round(self._learner.mu, 4),
            "learner_sigma":     round(self._learner.sigma, 4),
            "learner_shots":     self._learner.n_shots,
            "learner_green_pct": round(self._learner.green_rate, 4),
        }

    # ── XInput callbacks (poll thread) ────────────────────────────────────────

    def _on_state_change(self, snap: ControllerSnapshot) -> None:
        shot_active = bool(snap.buttons & ShotTimingEngine.SHOOT_BUTTON)

        # 1 — shot engine
        self._engine.on_snapshot(snap.buttons)

        # 2 — passthrough (X excluded when shot timer owns it)
        if not self._args.display_only:
            self._vpad.passthrough(
                snap,
                override_x     = shot_active,
                stick_noise_fn = self._hbr.stick_drift,
            )

        # 3 — dashboard push (non-blocking cache update)
        event, self._last_event = self._last_event, ""
        state = self.current_state_dict()
        state["event"] = event
        push_state(state)

    # ── Shot engine callbacks ─────────────────────────────────────────────────

    def _on_shot_hold(self) -> None:
        # Snapshot the learner's current recommendation so we can log it later
        self._shot_aim_pct = self._learner.aim_percentile
        if not self._args.display_only:
            self._vpad.press_x()
        if self._overlay:
            try:
                self._overlay.set_armed()
            except Exception:
                pass

    def _on_shot_release(self) -> None:
        if not self._args.display_only:
            self._vpad.release_x()
        # Signal detection loop that a shot just fired
        self._shot_fired.set()
        if self._overlay:
            try:
                self._overlay.flash_green()
            except Exception:
                pass

    def _on_shot_event(self, label: str) -> None:
        self._last_event = label

    # ── Detection callback (detector thread) ─────────────────────────────────

    def _on_detection(self, result: DetectionResult) -> None:
        # Update overlay
        if self._overlay:
            try:
                self._overlay.update_detection(result)
                self._overlay.update_learner(self._learner)
            except Exception:
                pass

        # If a shot was fired recently, use the detected outcome for learning
        if self._shot_fired.is_set():
            outcome = "unknown"
            if result.outcome_detected:
                outcome = "green"
            elif result.fill_pct < 0.4:
                outcome = "early"
            elif result.fill_pct > 0.85:
                outcome = "late"

            if outcome != "unknown":
                self._learner.record(
                    outcome     = outcome,
                    release_pct = self._shot_aim_pct,
                    aim_pct     = self._shot_aim_pct,
                )
                self._shot_fired.clear()

                # Push learner's updated aim back into the engine profile
                self._sync_learner_to_engine()

    def _sync_learner_to_engine(self) -> None:
        """Apply the learner's current μ as the engine aim_percentile."""
        cfg = self._config.get()
        try:
            profile = JumpShotProfile(
                name             = cfg.active_profile,
                animation_ms     = cfg.animation_ms,
                green_start_pct  = cfg.green_start_pct,
                green_end_pct    = cfg.green_end_pct,
                aim_percentile   = self._learner.aim_percentile,
            )
            self._engine.set_profile(profile)
        except Exception as exc:
            print(f"[Suite] learner sync error: {exc}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        # Try to auto-locate game window for precise capture region
        if self._capture.available:
            if not self._capture.auto_locate():
                print("[Suite] Game window not found — using default capture region. "
                      "Launch NBA 2K26 and restart, or drag the overlay to the meter.")

        # Start web dashboard
        start_web_server(config_mgr=self._config, suite=self,
                         host="127.0.0.1", port=self._args.port)

        # Start detection loop (120 FPS)
        if self._capture.available:
            self._detector.start(fps=120)
        else:
            print("[Suite] Screen capture unavailable — vision features disabled.")

        # Start XInput polling
        self._reader.start()

        # Launch overlay in daemon thread (tkinter mainloop)
        if not self._args.no_overlay:
            self._start_overlay()

        self._print_banner()
        signal.signal(signal.SIGINT, self._handle_sigint)

        try:
            self._stop.wait()
        finally:
            self._cleanup()

    def _start_overlay(self) -> None:
        try:
            import tkinter as tk
            from src.game_overlay import GameOverlay
        except ImportError as exc:
            print(
                f"[Suite] Overlay disabled — tkinter unavailable ({exc}).\n"
                "  Python 3.13 on Windows often ships with a broken _tkinter.dll.\n"
                "  Fix: reinstall Python from python.org and tick 'tcl/tk and IDLE',\n"
                "  or run with --no-overlay to suppress this message."
            )
            return

        _ready = threading.Event()

        def _run() -> None:
            try:
                root = tk.Tk()
                self._overlay = GameOverlay(root)
                root.geometry("+100+100")
                _ready.set()
                root.mainloop()
            except Exception as exc2:
                print(f"[Suite] Overlay crashed: {exc2}")
                _ready.set()

        t = threading.Thread(target=_run, daemon=True, name="overlay")
        t.start()
        _ready.wait(timeout=3.0)

    def _handle_sigint(self, *_: Any) -> None:
        print("\n[Suite] Stopping…")
        self._stop.set()

    def _cleanup(self) -> None:
        self._detector.stop()
        self._reader.stop()
        self._vpad.reset()
        print(f"[Suite] Stopped. Learner: {self._learner.summary()}")

    def _print_banner(self) -> None:
        cfg  = self._config.get()
        mode = "DISPLAY ONLY" if self._args.display_only else "ACTIVE"
        p    = PROFILES.get(cfg.active_profile, list(PROFILES.values())[0])
        cv   = "ON" if self._capture.available else "OFF (pip install mss opencv-python)"
        print(
            f"\n{'═'*58}\n"
            f"  NBA 2K26 Green Light Shot Suite  [{mode}]\n"
            f"{'─'*58}\n"
            f"  Controller slot  : {self._args.controller}\n"
            f"  Poll rate        : {self._args.poll_hz} Hz\n"
            f"  Shot button      : X (digital)\n"
            f"  Profile          : {cfg.active_profile}\n"
            f"  Animation        : {cfg.animation_ms:.0f} ms\n"
            f"  Release target   : {p.release_ms:.0f} ms after X press\n"
            f"  Green window     : {p.green_window_ms:.0f} ms wide\n"
            f"  Virtual pad      : {'connected' if self._vpad.available else 'NOT AVAILABLE'}\n"
            f"  Vision (CV)      : {cv}\n"
            f"  Learner          : {self._learner.summary()}\n"
            f"  Dashboard        : http://127.0.0.1:{self._args.port}\n"
            f"{'─'*58}\n"
            f"  Ctrl+C to quit\n"
            f"{'═'*58}\n"
        )


def main() -> None:
    args = parse_args()
    ShotSuite(args).run()


if __name__ == "__main__":
    main()
