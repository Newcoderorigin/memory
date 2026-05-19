"""
Config manager — loads/saves config.json and applies settings to live objects.

Atomic writes use a temp-file + rename pattern so a crash mid-write never
corrupts the config file.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .hbr import HBRProfile, HumanButtonResponder
    from .shot_timer import JumpShotProfile, ShotTimingEngine


@dataclass
class LiveConfig:
    """Flat config snapshot — everything the dashboard can read/write."""
    active_profile: str = "default"
    animation_ms: float = 800.0
    green_start_pct: float = 0.55
    green_end_pct: float = 0.65
    aim_percentile: float = 0.50
    hbr_sigma_ms: float = 8.0
    hbr_tau_ms: float = 4.0
    hold_base_ms: float = 52.0
    ramp_steps: int = 5
    ramp_exponent: float = 2.4
    stick_noise_sigma: float = 0.011
    # Vision mode
    vision_mode: bool = False
    vision_latency_ms: float = 8.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LiveConfig":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


class ConfigManager:
    """
    Thread-safe config store.  Holds a LiveConfig, persists to JSON,
    and can apply changes to live HBR / ShotTimingEngine instances.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._cfg = LiveConfig()

        # Weak refs to live objects (set after suite is constructed)
        self._hbr: Optional["HumanButtonResponder"] = None
        self._engine: Optional["ShotTimingEngine"] = None

        if path.exists():
            self._load()

    # ── Registration ──────────────────────────────────────────────────────────

    def register(
        self,
        hbr: "HumanButtonResponder",
        engine: "ShotTimingEngine",
    ) -> None:
        with self._lock:
            self._hbr = hbr
            self._engine = engine

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(self) -> LiveConfig:
        with self._lock:
            return LiveConfig(**self._cfg.to_dict())

    def get_dict(self) -> dict[str, Any]:
        with self._lock:
            return self._cfg.to_dict()

    # ── Write + Apply ─────────────────────────────────────────────────────────

    def apply_dict(self, updates: dict[str, Any]) -> None:
        """Merge updates into current config, apply to live objects, save."""
        from .hbr import HBRProfile
        from .shot_timer import JumpShotProfile

        with self._lock:
            current = self._cfg.to_dict()
            current.update(updates)
            self._cfg = LiveConfig.from_dict(current)
            cfg = self._cfg

            if self._hbr is not None:
                self._hbr.update_profile(HBRProfile(
                    press_sigma_ms=cfg.hbr_sigma_ms,
                    press_tau_ms=cfg.hbr_tau_ms,
                    hold_base_ms=cfg.hold_base_ms,
                    ramp_steps=cfg.ramp_steps,
                    ramp_exponent=cfg.ramp_exponent,
                    stick_noise_sigma=cfg.stick_noise_sigma,
                ))

            if self._engine is not None:
                try:
                    profile = JumpShotProfile(
                        name=cfg.active_profile,
                        animation_ms=cfg.animation_ms,
                        green_start_pct=cfg.green_start_pct,
                        green_end_pct=cfg.green_end_pct,
                        aim_percentile=cfg.aim_percentile,
                    )
                    self._engine.set_profile(profile)
                except ValueError as exc:
                    raise ValueError(f"Invalid shot profile: {exc}") from exc

        self._save()

    def switch_profile(self, name: str) -> None:
        """Switch to a named built-in profile and apply immediately."""
        from .shot_timer import PROFILES

        if name not in PROFILES:
            raise KeyError(f"Unknown profile: {name!r}")

        p = PROFILES[name]
        self.apply_dict(
            {
                "active_profile": name,
                "animation_ms": p.animation_ms,
                "green_start_pct": p.green_start_pct,
                "green_end_pct": p.green_end_pct,
                "aim_percentile": p.aim_percentile,
            }
        )

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            self._cfg = LiveConfig.from_dict(data)
        except Exception as exc:
            print(f"[Config] Failed to load {self._path}: {exc} — using defaults")

    def _save(self) -> None:
        """Atomic write: temp file → rename."""
        tmp = self._path.with_suffix(".json.tmp")
        try:
            with self._lock:
                data = self._cfg.to_dict()
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception as exc:
            print(f"[Config] Save failed: {exc}")
            tmp.unlink(missing_ok=True)
