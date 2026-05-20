# src/config_manager.py
"""
Configuration manager with persistent storage and live object updates.
Tracks shot profiles, learning data, and HBR parameters.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

@dataclass
class ShotLearningProfile:
    """Per-player timing learning data."""
    player_name: str
    animation_ms: float
    
    # Adaptive offset (learned timing correction in ms)
    offset_ms: float = 0.0
    offset_sigma: float = 8.0  # uncertainty in offset estimate
    
    # Timing history (rolling window of last N shots)
    release_errors: list[float] = None  # actual_time - optimal_time
    green_rate: float = 0.0  # greens / total shots
    total_shots: int = 0
    
    def __post_init__(self):
        if self.release_errors is None:
            self.release_errors = []
    
    def record_shot(self, error_ms: float, was_green: bool) -> None:
        """Record actual vs. expected release timing."""
        self.release_errors.append(error_ms)
        if len(self.release_errors) > 100:  # keep last 100 shots
            self.release_errors.pop(0)
        
        self.total_shots += 1
        if was_green:
            self.green_rate = (self.green_rate * (self.total_shots - 1) + 1.0) / self.total_shots
        else:
            self.green_rate = (self.green_rate * (self.total_shots - 1)) / self.total_shots


class ConfigManager:
    """
    Manages configuration state and auto-saves to disk.
    Supports live updates for registered objects (HBR, ShotTimingEngine, learning profiles).
    """
    
    def __init__(self, config_path: Path) -> None:
        self._path = config_path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._learning_profiles: dict[str, ShotLearningProfile] = {}
        self._observers: list[Any] = []  # objects to update on config change
        
        self._load()
    
    def _load(self) -> None:
        """Load config from disk, or create defaults."""
        if self._path.exists():
            try:
                with open(self._path, 'r') as f:
                    saved = json.load(f)
                    self._data = saved.get('config', {})
                    
                    # Restore learning profiles
                    for name, profile_data in saved.get('learning_profiles', {}).items():
                        profile = ShotLearningProfile(**profile_data)
                        self._learning_profiles[name] = profile
            except Exception as e:
                print(f"[ConfigManager] Load failed: {e} — using defaults")
        
        # Ensure essential keys exist
        self._data.setdefault('active_profile', 'default')
        self._data.setdefault('animation_ms', 800.0)
        self._data.setdefault('green_start_pct', 0.55)
        self._data.setdefault('green_end_pct', 0.65)
        self._data.setdefault('aim_percentile', 0.50)
        self._data.setdefault('auto_shoot_mode', False)
        self._data.setdefault('learning_enabled', True)
        self._save()
    
    def register(self, *objects: Any) -> None:
        """Register live objects to receive config updates."""
        with self._lock:
            self._observers.extend(objects)
    
    def get(self) -> dict[str, Any]:
        """Get current config dict (thread-safe snapshot)."""
        with self._lock:
            return dict(self._data)
    
    def apply_dict(self, updates: dict[str, Any]) -> None:
        """Apply updates and notify observers."""
        with self._lock:
            self._data.update(updates)
            
            # Notify registered objects of changes
            for obs in self._observers:
                if hasattr(obs, 'update_profile'):
                    obs.update_profile(updates)
        
        self._save()
    
    def get_learning_profile(self, player_name: str) -> ShotLearningProfile:
        """Get or create learning profile for a player."""
        with self._lock:
            if player_name not in self._learning_profiles:
                self._learning_profiles[player_name] = ShotLearningProfile(
                    player_name=player_name,
                    animation_ms=self._data.get('animation_ms', 800.0),
                )
            return self._learning_profiles[player_name]
    
    def record_shot(self, player_name: str, error_ms: float, was_green: bool) -> None:
        """Record a shot attempt for learning."""
        profile = self.get_learning_profile(player_name)
        profile.record_shot(error_ms, was_green)
        self._save()
    
    def _save(self) -> None:
        """Persist config to disk."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                'config': self._data,
                'learning_profiles': {
                    name: asdict(profile)
                    for name, profile in self._learning_profiles.items()
                }
            }
            with open(self._path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[ConfigManager] Save failed: {e}")