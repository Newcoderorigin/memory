"""
In-game shot feedback overlay.

A borderless, always-on-top, semi-transparent tkinter window that renders
over the game and shows:
  - Current detection state  (WAITING / METER ACTIVE / TARGET LOCKED / GREEN ✓)
  - Shot meter fill bar       (mirrors the arc fill level from MeterDetector)
  - Green window marker       (a green band on the bar showing the window)
  - Aim marker                (vertical line showing where learner targets)
  - Live stats                (shots taken, green rate, current μ/σ)

All public methods are safe to call from any thread — they schedule
updates through root.after(0, ...) which is the only safe tkinter
cross-thread mechanism.

Positioning
───────────
  Default: upper-left corner (100, 100).  User can drag it.
  For best results, drag it near the shot meter arc in-game.
"""
from __future__ import annotations

import threading
import tkinter as tk
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .meter_detector import DetectionResult
    from .shot_learner   import AdaptiveTimingLearner

# ── Theme ─────────────────────────────────────────────────────────────────────
_BG       = "#08080f"
_ACTIVE   = "#00ff88"
_WARNING  = "#ffaa00"
_DANGER   = "#ff4444"
_DIM      = "#2a2a3a"
_TEXT     = "#ccccee"
_BAR_BG   = "#151525"
_BAR_FILL = "#3377ff"
_GREEN_W  = "#00ff55"
_CLOSE    = "#441111"

_W, _H = 290, 165


class GameOverlay:
    """
    In-game shot meter feedback overlay.  Run in a daemon thread via launch().
    """

    def __init__(self, root: tk.Tk) -> None:
        self._root   = root
        self._canvas: Optional[tk.Canvas] = None

        # Canvas item IDs (populated during _build)
        self._state_id:      Optional[int] = None
        self._fill_rect_id:  Optional[int] = None
        self._green_band_id: Optional[int] = None
        self._aim_line_id:   Optional[int] = None
        self._fill_label_id: Optional[int] = None
        self._stats_id:      Optional[int] = None
        self._mu_id:         Optional[int] = None

        self._drag_ox = self._drag_oy = 0
        self._build()

    # ── Construction ──────────────────────────────────────────────────────────

    def _build(self) -> None:
        r = self._root
        r.title("Shot Suite Overlay")
        r.wm_attributes("-topmost", True)
        r.wm_attributes("-alpha",   0.87)
        r.configure(bg=_BG)
        r.resizable(False, False)
        r.overrideredirect(True)

        r.bind("<ButtonPress-1>", self._drag_start)
        r.bind("<B1-Motion>",     self._drag_move)
        r.bind("<Escape>",        lambda _: r.destroy())

        c = tk.Canvas(r, width=_W, height=_H, bg=_BG, highlightthickness=0)
        c.pack()
        self._canvas = c

        # ── Title bar ─────────────────────────────────────────────────────────
        c.create_rectangle(0, 0, _W, 22, fill="#05050c", outline="")
        c.create_text(10, 11, text="⚡ 2K26 Shot Suite", fill=_TEXT,
                      font=("Segoe UI", 8, "bold"), anchor="w")
        c.create_rectangle(_W-24, 2, _W-2, 20, fill=_CLOSE, outline="", tags="cls")
        c.create_text(_W-13, 11, text="✕", fill="#ff6666",
                      font=("Segoe UI", 8), tags="cls")
        c.tag_bind("cls", "<Button-1>", lambda _: r.destroy())

        # ── State banner ──────────────────────────────────────────────────────
        self._state_id = c.create_text(
            _W // 2, 46, text="WAITING",
            fill=_DIM, font=("Segoe UI", 15, "bold"),
        )

        # ── Meter bar ─────────────────────────────────────────────────────────
        # Bar background
        c.create_rectangle(18, 68, _W-18, 86, fill=_BAR_BG, outline=_DIM, width=1)
        # Fill rect (dynamically resized)
        self._fill_rect_id = c.create_rectangle(18, 68, 18, 86, fill=_BAR_FILL, outline="")
        # Green window band (dynamically positioned)
        self._green_band_id = c.create_rectangle(0, 66, 0, 88,
                                                  fill=_GREEN_W, outline="",
                                                  stipple="gray50")
        # Aim marker line
        self._aim_line_id = c.create_line(18, 63, 18, 91, fill=_ACTIVE, width=2)

        # ── Labels ────────────────────────────────────────────────────────────
        self._fill_label_id = c.create_text(
            _W // 2, 98, text="fill: 0%   conf: 0.00",
            fill=_TEXT, font=("Segoe UI", 8),
        )
        self._stats_id = c.create_text(
            _W // 2, 118, text="shots: 0  green: 0.0%  aim: 50.0%",
            fill=_DIM, font=("Segoe UI", 8),
        )
        c.create_text(14, 142, text="learner:", fill=_DIM,
                      font=("Segoe UI", 8), anchor="w")
        self._mu_id = c.create_text(
            70, 142, text="μ=0.500  σ=0.080",
            fill=_TEXT, font=("Segoe UI", 8), anchor="w",
        )

    # ── Public thread-safe API ────────────────────────────────────────────────

    def update_detection(self, result: "DetectionResult") -> None:
        """Refresh meter bar and state banner from a DetectionResult."""
        self._root.after(0, self._apply_detection, result)

    def update_learner(self, learner: "AdaptiveTimingLearner") -> None:
        """Refresh aim marker and stats from the learner."""
        self._root.after(0, self._apply_learner, learner)

    def flash_green(self) -> None:
        """Flash the GREEN RELEASE ✓ banner for 800 ms."""
        self._root.after(0, self._do_flash_green)

    def set_armed(self) -> None:
        self._root.after(0, self._set_state, "SHOT ARMED", _WARNING)

    # ── Internal UI updaters (main thread) ───────────────────────────────────

    def _apply_detection(self, result: "DetectionResult") -> None:
        c = self._canvas
        if not c:
            return

        bar_l, bar_r = 18, _W - 18
        bar_w = bar_r - bar_l

        # Fill bar
        fill_x = bar_l + int(bar_w * max(0.0, min(1.0, result.fill_pct)))
        c.coords(self._fill_rect_id, bar_l, 68, max(bar_l, fill_x), 86)

        # Green window band
        if result.green_window_visible:
            gx      = bar_l + int(bar_w * result.green_window_pct)
            half    = max(4, int(bar_w * 0.055))
            c.coords(self._green_band_id, gx - half, 66, gx + half, 88)
            c.itemconfig(self._green_band_id, fill=_GREEN_W)
        else:
            c.coords(self._green_band_id, 0, 0, 0, 0)

        # Fill label
        c.itemconfig(
            self._fill_label_id,
            text=f"fill: {result.fill_pct*100:.0f}%   conf: {result.confidence:.2f}",
        )

        # State banner
        if result.outcome_detected:
            c.itemconfig(self._state_id, text="EXCELLENT ✓", fill=_GREEN_W)
        elif result.meter_found and result.green_window_visible:
            c.itemconfig(self._state_id, text="TARGET LOCKED", fill=_ACTIVE)
        elif result.meter_found:
            c.itemconfig(self._state_id, text="METER ACTIVE", fill=_WARNING)
        else:
            c.itemconfig(self._state_id, text="WAITING", fill=_DIM)

    def _apply_learner(self, learner: "AdaptiveTimingLearner") -> None:
        c = self._canvas
        if not c:
            return

        mu    = learner.mu
        sigma = learner.sigma
        n     = learner.n_shots
        gr    = learner.green_rate

        # Aim marker
        bar_l, bar_r = 18, _W - 18
        aim_x = bar_l + int((bar_r - bar_l) * mu)
        c.coords(self._aim_line_id, aim_x, 63, aim_x, 91)

        c.itemconfig(self._stats_id,
                     text=f"shots: {n}  green: {gr:.1%}  aim: {mu:.1%}")
        c.itemconfig(self._mu_id,
                     text=f"μ={mu:.3f}  σ={sigma:.3f}")

    def _set_state(self, text: str, color: str) -> None:
        if self._canvas and self._state_id is not None:
            self._canvas.itemconfig(self._state_id, text=text, fill=color)

    def _do_flash_green(self) -> None:
        c = self._canvas
        if not c or self._state_id is None:
            return
        c.itemconfig(self._state_id, text="GREEN RELEASE ✓", fill=_GREEN_W)
        self._root.after(800,
                         lambda: c.itemconfig(self._state_id, text="WAITING", fill=_DIM))

    # ── Drag ──────────────────────────────────────────────────────────────────

    def _drag_start(self, event: tk.Event) -> None:
        self._drag_ox = event.x
        self._drag_oy = event.y

    def _drag_move(self, event: tk.Event) -> None:
        x = self._root.winfo_x() + (event.x - self._drag_ox)
        y = self._root.winfo_y() + (event.y - self._drag_oy)
        self._root.geometry(f"+{x}+{y}")


# ── Launcher (run from non-main thread on Windows) ────────────────────────────

def launch_overlay() -> Optional[GameOverlay]:
    """
    Create and start the overlay.  Returns the GameOverlay instance so
    callers can call update_detection() / update_learner() from other threads.

    Starts tkinter mainloop() in the calling thread — call from a dedicated
    daemon thread.
    """
    root    = tk.Tk()
    overlay = GameOverlay(root)
    # Position near top-left; user can drag
    root.geometry(f"+{100}+{100}")
    root.mainloop()
    return overlay
