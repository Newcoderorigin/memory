"""
Persistent configuration manager.

Loads/saves config.json; exposes live-update hooks so the web dashboard
can apply changes to running HBR and ShotTimingEngine instances.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .hbr import HumanButtonResponder
    from .shot_timer import ShotTimingEngine


@dataclass
class Config:
    active_profile:  str   = "default"
    animation_ms:    float = 800.0
    green_start_pct: float = 0.55
    green_end_pct:   float = 0.65
    aim_percentile:  float = 0.50


class ConfigManager:
    def __init__(self, path: Path) -> None:
        self._path  = path
        self._lock  = threading.Lock()
        self._cfg   = Config()
        self._hbr:    Optional[Any] = None
        self._engine: Optional[Any] = None
        self._load()

    # ── Registration ──────────────────────────────────────────────────────────

    def register(self, hbr: Any, engine: Any) -> None:
        self._hbr    = hbr
        self._engine = engine

    # ── Read / write ──────────────────────────────────────────────────────────

    def get(self) -> Config:
        with self._lock:
            return self._cfg

    def apply_dict(self, data: dict[str, Any]) -> None:
        with self._lock:
            for k, v in data.items():
                if hasattr(self._cfg, k):
                    setattr(self._cfg, k, v)
            self._persist()
            self._push_to_components()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(self._cfg, k):
                    setattr(self._cfg, k, v)
        except Exception as exc:
            print(f"[Config] load error (using defaults): {exc}")

    def _persist(self) -> None:
        try:
            self._path.write_text(
                json.dumps(asdict(self._cfg), indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"[Config] save error: {exc}")

    def _push_to_components(self) -> None:
        """Apply current config to live engine/hbr if registered."""
        cfg = self._cfg
        if self._engine is not None:
            from .shot_timer import JumpShotProfile
            try:
                self._engine.set_profile(JumpShotProfile(
                    name            = cfg.active_profile,
                    animation_ms    = cfg.animation_ms,
                    green_start_pct = cfg.green_start_pct,
                    green_end_pct   = cfg.green_end_pct,
                    aim_percentile  = cfg.aim_percentile,
                ))
            except Exception as exc:
                print(f"[Config] engine update error: {exc}")
