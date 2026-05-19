"""
NBA 2K26 Green Light Shot Suite — Launcher
==========================================
Wires all modules together and serves the web dashboard.

  XInputReader  →  passthrough → VirtualController  (all buttons)
                →  ShotTimingEngine               (X button intercept)
                →  web_server.push_state()        (live dashboard)

  Timing mode:
    ShotTimingEngine  →  HBR.jitter_ms()  →  VirtualController.press_x/release_x

  Vision mode:
    ShotTimingEngine (hold)  →  ShotMeterController  →  VirtualController.release_x
    MeterDetector (240 Hz)   → Kalman filter → predictive release timing

Usage
─────
  pip install fastapi "uvicorn[standard]" vgamepad pywin32
  pip install dxcam opencv-python numpy mss      # vision mode

  python main.py [--profile default|quick|slow|midrange]
                 [--controller 0]
                 [--poll-hz 125]
                 [--port 8420]
                 [--display-only]
                 [--vision]

  Then open http://localhost:8420 in your browser.

Game setup
──────────
  1. Install ViGEmBus: https://github.com/nefarius/ViGEmBus/releases
  2. Run this script BEFORE launching NBA 2K26.
  3. In 2K26 controller settings, select the ViGEmBus virtual controller slot.
  4. Press X to shoot — the suite intercepts and optimises the release timing.

Shot calibration
────────────────
  Timing mode : Open dashboard → Settings → adjust Animation ms, Green Start/End %.
  Vision mode : Open dashboard → Shot Meter → Run Calibration → select meter ROI.
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
from src.meter_detector import MeterDetector, MeterConfig
from src.shot_meter import ShotMeterController
from src.calibrator import load_meter_config

_CONFIG_PATH = Path(__file__).parent / "config.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="NBA 2K26 Green Light Shot Suite")
    p.add_argument("--profile",      default="default", choices=list(PROFILES))
    p.add_argument("--controller",   type=int, default=0, choices=[0, 1, 2, 3])
    p.add_argument("--poll-hz",      type=int, default=125)
    p.add_argument("--port",         type=int, default=8420)
    p.add_argument("--display-only", action="store_true",
                   help="Overlay + dashboard only — no virtual controller output")
    p.add_argument("--vision",       action="store_true",
                   help="Start with vision mode enabled (requires cv2 + capture backend)")
    return p.parse_args()


class ShotSuite:
    """Top-level coordinator — owns all components and their lifecycle."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        self._stop = threading.Event()
        self._last_event: str = ""

        # Config
        self._config = ConfigManager(_CONFIG_PATH)

        # Apply startup profile from CLI arg
        initial_profile = PROFILES[args.profile]

        # Components
        self._vpad   = VirtualController()
        self._hbr    = HumanButtonResponder(HBRProfile())
        self._engine = ShotTimingEngine(
            profile    = initial_profile,
            hbr        = self._hbr,
            on_hold    = self._on_shot_hold,
            on_release = self._on_shot_release_timing,
            on_event   = self._on_shot_event,
        )
        self._reader = XInputReader(
            controller_index = args.controller,
            poll_hz          = args.poll_hz,
            on_state_change  = self._on_state_change,
        )

        # Vision components
        self._meter_cfg  = load_meter_config()
        self._detector   = MeterDetector(self._meter_cfg)
        self._meter_ctrl = ShotMeterController(
            detector      = self._detector,
            on_release_fn = self._vpad.release_x,
            hbr           = self._hbr,
        )
        self._vision_mode = args.vision
        self._vision_lock = threading.Lock()

        # Start vision detector (it starts its poll thread regardless of mode)
        self._vision_available = self._detector.start()
        if not self._vision_available and args.vision:
            print("[Suite] Vision mode requested but capture backend unavailable — falling back to timing mode.")
            self._vision_mode = False

        # Sync config
        self._config.register(self._hbr, self._engine)
        self._config.apply_dict({
            "active_profile":   args.profile,
            "animation_ms":     initial_profile.animation_ms,
            "green_start_pct":  initial_profile.green_start_pct,
            "green_end_pct":    initial_profile.green_end_pct,
            "aim_percentile":   initial_profile.aim_percentile,
            "vision_mode":      self._vision_mode,
            "vision_latency_ms": self._meter_cfg.latency_ms,
        })

    # ── Vision mode control ───────────────────────────────────────────────────

    def set_vision_mode(self, on: bool) -> None:
        with self._vision_lock:
            if on and not self._vision_available:
                print("[Suite] Vision capture backend unavailable — ignoring vision mode request.")
                return
            self._vision_mode = on
            print(f"[Suite] Vision mode {'ON' if on else 'OFF'}")

    def set_vision_latency(self, ms: float) -> None:
        with self._vision_lock:
            cfg = self._meter_cfg
            new_cfg = MeterConfig(
                roi=cfg.roi,
                fill_v_threshold=cfg.fill_v_threshold,
                min_col_fraction=cfg.min_col_fraction,
                green_h_lo=cfg.green_h_lo,
                green_h_hi=cfg.green_h_hi,
                green_s_lo=cfg.green_s_lo,
                green_v_lo=cfg.green_v_lo,
                latency_ms=ms,
                kalman_Q=cfg.kalman_Q,
                kalman_R=cfg.kalman_R,
                target_hz=cfg.target_hz,
            )
            self._meter_cfg = new_cfg
            self._detector.update_config(new_cfg)

    def get_meter_config(self) -> MeterConfig:
        with self._vision_lock:
            return self._meter_cfg

    def update_meter_config(self, cfg: MeterConfig) -> None:
        with self._vision_lock:
            self._meter_cfg = cfg
            self._detector.update_config(cfg)

    @property
    def vision_backend(self) -> str:
        return self._detector.backend

    # ── State dict for dashboard ──────────────────────────────────────────────

    def current_state_dict(self) -> dict[str, Any]:
        snap     = self._reader.snapshot
        cfg      = self._config.get()
        meter    = self._detector.get_snapshot()
        vm       = self._vision_mode
        m_cfg    = self._meter_cfg

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
            "vision_mode":     vm,
            "vision_backend":  self._detector.backend,
            "event":           "",
            "meter": {
                "fill_pct":            round(meter.fill_pct, 3),
                "velocity_pct_per_ms": round(meter.velocity_pct_per_ms, 4),
                "fill_detected":       meter.fill_detected,
                "green_detected":      meter.green_detected,
                "latency_ms":          m_cfg.latency_ms,
            },
            "meter_cfg": {
                "green_start_pct": cfg.green_start_pct,
                "green_end_pct":   cfg.green_end_pct,
            },
        }

    # ── XInput callbacks (poll thread) ────────────────────────────────────────

    def _on_state_change(self, snap: ControllerSnapshot) -> None:
        shot_active = bool(snap.buttons & ShotTimingEngine.SHOOT_BUTTON)

        self._engine.on_snapshot(snap.buttons)

        if not self._args.display_only:
            self._vpad.passthrough(
                snap,
                override_x     = shot_active,
                stick_noise_fn = self._hbr.stick_drift,
            )

        event, self._last_event = self._last_event, ""
        state = self.current_state_dict()
        state["event"] = event
        push_state(state)

    # ── Shot engine callbacks ─────────────────────────────────────────────────

    def _on_shot_hold(self) -> None:
        """X pressed — hold X on vpad, arm vision controller if vision mode on."""
        if not self._args.display_only:
            self._vpad.press_x()

        if self._vision_mode and self._vision_available:
            cfg = self._config.get()
            self._meter_ctrl.arm(
                shot_start       = time.perf_counter(),
                green_start_pct  = cfg.green_start_pct,
                green_end_pct    = cfg.green_end_pct,
                animation_ms     = cfg.animation_ms,
                aim_percentile   = cfg.aim_percentile,
            )

    def _on_shot_release_timing(self) -> None:
        """
        Called by ShotTimingEngine's fixed timer.
        In vision mode: only fires if ShotMeterController hasn't already fired.
        In timing mode: fires unconditionally.
        """
        if self._vision_mode and self._vision_available:
            # Vision mode — ShotMeterController owns the release.
            # ShotTimingEngine fires as a fallback if vision fires first,
            # but meter_ctrl already has single-fire guard.
            self._meter_ctrl.cancel()   # harmless if already fired
            return

        if not self._args.display_only:
            self._vpad.release_x()

    def _on_shot_event(self, label: str) -> None:
        self._last_event = label

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        start_web_server(
            config_mgr = self._config,
            suite      = self,
            host       = "127.0.0.1",
            port       = self._args.port,
        )

        self._reader.start()
        self._print_banner()

        signal.signal(signal.SIGINT, self._handle_sigint)

        try:
            self._stop.wait()
        finally:
            self._cleanup()

    def _handle_sigint(self, *_: Any) -> None:
        print("\n[Suite] Stopping…")
        self._stop.set()

    def _cleanup(self) -> None:
        self._reader.stop()
        self._detector.stop()
        self._vpad.reset()
        print("[Suite] Stopped.")

    def _print_banner(self) -> None:
        cfg  = self._config.get()
        mode = "DISPLAY ONLY" if self._args.display_only else "ACTIVE"
        vm   = "ON (" + self._detector.backend + ")" if self._vision_mode else "off"
        p    = PROFILES.get(cfg.active_profile, list(PROFILES.values())[0])
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
            f"  Vision mode      : {vm}\n"
            f"  Virtual pad      : {'connected' if self._vpad.available else 'NOT AVAILABLE'}\n"
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
