"""
Controller overlay and visual Xbox controller map.

Always-on-top, semi-transparent tkinter window (borderless, draggable).
Runs entirely on the tkinter main thread.  Background threads post updates
via root.after(0, ...) — the only safe cross-thread tkinter call.

Features:
  - Live button highlighting (A/B/X/Y, bumpers, d-pad, thumbsticks, Start/Back)
  - Trigger fill bars (LT / RT)
  - Analog stick dot that tracks actual position
  - Shot event flash banner ("GREEN RELEASE ✓", "SHOT ARMED", etc.)
  - Draggable borderless window with close button
  - Active profile label and connection status
"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .xinput_reader import ControllerSnapshot

# Imported at module level — no per-call import overhead in hot _apply() path
from .xinput_reader import (
    BTN_A, BTN_B, BTN_X, BTN_Y, BTN_LB, BTN_RB,
    BTN_START, BTN_BACK, BTN_LS, BTN_RS,
    DPAD_UP, DPAD_DOWN, DPAD_LEFT, DPAD_RIGHT,
    ControllerSnapshot,
)

# ── Theme ─────────────────────────────────────────────────────────────────────
_BG      = "#111122"
_BODY    = "#1e1e3a"
_OUTLINE = "#3a3a5c"
_ACTIVE  = "#00ff99"
_TEXT    = "#d0d0e8"
_DIM     = "#2e2e4a"
_TRIG    = "#ff6b35"
_STICK   = "#4a9eff"
_A_CLR   = "#2ecc71"
_B_CLR   = "#e74c3c"
_X_CLR   = "#3498db"
_Y_CLR   = "#f0c040"
_CLOSE   = "#c0392b"


class ControllerOverlay:
    """
    Draws and manages the live controller visualisation window.
    All public methods that update UI state are safe to call from any thread.
    """

    W, H = 500, 310

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._canvas: Optional[tk.Canvas] = None

        # Canvas item IDs populated during build
        self._btn_items: dict[str, int]      = {}  # name → oval id
        self._btn_colors: dict[str, str]     = {}  # name → active color
        self._trig_bars: dict[str, int]      = {}  # "LT"/"RT" → rect id
        self._stick_dots: dict[str, tuple]   = {}  # "L"/"R" → (cx,cy,dot_id,r)
        self._status_id: Optional[int]       = None
        self._shot_id: Optional[int]         = None
        self._profile_id: Optional[int]      = None

        self._drag_ox = 0
        self._drag_oy = 0

        self._build()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self) -> None:
        r = self._root
        r.title("2K26 Shot Suite")
        r.wm_attributes("-topmost", True)
        r.wm_attributes("-alpha", 0.90)
        r.configure(bg=_BG)
        r.resizable(False, False)
        r.overrideredirect(True)   # borderless

        r.bind("<ButtonPress-1>",   self._drag_start)
        r.bind("<B1-Motion>",       self._drag_move)
        r.bind("<Escape>",          lambda _e: r.destroy())

        c = tk.Canvas(r, width=self.W, height=self.H,
                      bg=_BG, highlightthickness=0)
        c.pack()
        self._canvas = c

        self._draw_titlebar(c)
        self._draw_body(c)
        self._draw_face_buttons(c)
        self._draw_bumpers(c)
        self._draw_triggers(c)
        self._draw_dpad(c)
        self._draw_center_buttons(c)
        self._draw_sticks(c)
        self._draw_status(c)

    def _draw_titlebar(self, c: tk.Canvas) -> None:
        c.create_rectangle(0, 0, self.W, 24, fill="#0d0d1e", outline="")
        c.create_text(10, 12, text="2K26 Shot Suite", fill=_TEXT,
                      font=("Segoe UI", 9, "bold"), anchor="w")
        # Close button
        c.create_rectangle(self.W - 30, 2, self.W - 2, 22,
                           fill=_CLOSE, outline="", tags="close")
        c.create_text(self.W - 16, 12, text="✕", fill="white",
                      font=("Segoe UI", 9, "bold"), tags="close")
        c.tag_bind("close", "<Button-1>", lambda _e: self._root.destroy())

    def _draw_body(self, c: tk.Canvas) -> None:
        # Controller shell
        c.create_oval(55,  45, 445, 275, fill=_BODY, outline=_OUTLINE, width=2)
        c.create_oval(35, 155, 165, 285, fill="#181830", outline=_OUTLINE, width=1)
        c.create_oval(335, 155, 465, 285, fill="#181830", outline=_OUTLINE, width=1)
        # Xbox guide button
        c.create_oval(228, 118, 272, 158, fill="#0d0d1e", outline=_OUTLINE, width=2)
        c.create_text(250, 138, text="⬤", fill="#334455",
                      font=("Segoe UI", 14))

    def _draw_face_buttons(self, c: tk.Canvas) -> None:
        specs = {
            "A": (355, 200, _A_CLR, "A"),
            "B": (385, 172, _B_CLR, "B"),
            "X": (325, 172, _X_CLR, "X"),
            "Y": (355, 144, _Y_CLR, "Y"),
        }
        r = 15
        for name, (x, y, color, label) in specs.items():
            item = c.create_oval(x-r, y-r, x+r, y+r,
                                 fill=_DIM, outline=_OUTLINE, width=1)
            c.create_text(x, y, text=label, fill=_TEXT,
                          font=("Segoe UI", 9, "bold"))
            self._btn_items[name] = item
            self._btn_colors[name] = color

    def _draw_bumpers(self, c: tk.Canvas) -> None:
        specs = {
            "LB": (110, 62, "#9b59b6"),
            "RB": (390, 62, "#9b59b6"),
        }
        for name, (x, y, color) in specs.items():
            item = c.create_oval(x-28, y-12, x+28, y+12,
                                 fill=_DIM, outline=_OUTLINE, width=1)
            c.create_text(x, y, text=name, fill=_TEXT,
                          font=("Segoe UI", 8, "bold"))
            self._btn_items[name] = item
            self._btn_colors[name] = color

    def _draw_triggers(self, c: tk.Canvas) -> None:
        for name, x in (("LT", 95), ("RT", 405)):
            c.create_rectangle(x-45, 28, x+45, 52,
                               fill="#0d0d1e", outline=_OUTLINE)
            c.create_text(x, 40, text=name, fill=_TEXT,
                          font=("Segoe UI", 8))
            # fill bar (starts at min width)
            bar = c.create_rectangle(x-43, 30, x-43, 50,
                                     fill=_TRIG, outline="")
            self._trig_bars[name] = bar

    def _draw_dpad(self, c: tk.Canvas) -> None:
        cx, cy = 150, 210
        arm = 13
        pad = 5
        specs = {
            "DPAD_UP":    (cx,        cy - arm - pad, arm),
            "DPAD_DOWN":  (cx,        cy + arm + pad, arm),
            "DPAD_LEFT":  (cx - arm - pad, cy,        arm),
            "DPAD_RIGHT": (cx + arm + pad, cy,        arm),
        }
        for name, (x, y, r) in specs.items():
            item = c.create_oval(x-r, y-r, x+r, y+r,
                                 fill=_DIM, outline=_OUTLINE, width=1)
            arrow = {"DPAD_UP":"↑","DPAD_DOWN":"↓",
                     "DPAD_LEFT":"←","DPAD_RIGHT":"→"}[name]
            c.create_text(x, y, text=arrow, fill=_TEXT,
                          font=("Segoe UI", 9))
            self._btn_items[name] = item
            self._btn_colors[name] = _ACTIVE

    def _draw_center_buttons(self, c: tk.Canvas) -> None:
        specs = {
            "BACK":  (215, 150, "⊲"),
            "START": (285, 150, "⊳"),
            "LS":    (130, 200, "LS"),
            "RS":    (295, 230, "RS"),
        }
        for name, (x, y, label) in specs.items():
            item = c.create_oval(x-14, y-10, x+14, y+10,
                                 fill=_DIM, outline=_OUTLINE, width=1)
            c.create_text(x, y, text=label, fill=_TEXT,
                          font=("Segoe UI", 8))
            self._btn_items[name] = item
            self._btn_colors[name] = _ACTIVE

    def _draw_sticks(self, c: tk.Canvas) -> None:
        for side, cx, cy in (("L", 130, 200), ("R", 295, 230)):
            radius = 32
            c.create_oval(cx-radius, cy-radius, cx+radius, cy+radius,
                          fill="#0d0d1e", outline=_OUTLINE, width=2)
            dr = 9
            dot = c.create_oval(cx-dr, cy-dr, cx+dr, cy+dr,
                                fill=_STICK, outline="")
            self._stick_dots[side] = (cx, cy, dot, radius - dr - 2)

    def _draw_status(self, c: tk.Canvas) -> None:
        self._status_id = c.create_text(
            10, 290, text="● Disconnected", fill="#e74c3c",
            font=("Segoe UI", 8), anchor="w",
        )
        self._shot_id = c.create_text(
            250, 290, text="", fill=_ACTIVE,
            font=("Segoe UI", 9, "bold"),
        )
        self._profile_id = c.create_text(
            self.W - 10, 290, text="profile: default", fill=_DIM,
            font=("Segoe UI", 8), anchor="e",
        )

    # ── Public update API (call from any thread) ──────────────────────────────

    def update_snapshot(self, snap: object) -> None:
        """Schedule a UI refresh with the latest ControllerSnapshot."""
        self._root.after(0, self._apply, snap)

    def flash_event(self, label: str, duration_ms: int = 700) -> None:
        """Flash an event label in the status bar for `duration_ms` ms."""
        self._root.after(0, self._show_event, label, duration_ms)

    def set_profile_label(self, name: str) -> None:
        self._root.after(0, self._set_profile, name)

    # ── Internal UI updaters (main thread only) ───────────────────────────────

    def _apply(self, snap: object) -> None:
        # Fix #8: guard against non-ControllerSnapshot arriving via after(0)
        if not isinstance(snap, ControllerSnapshot):
            return
        c = self._canvas
        if c is None:
            return

        # Connection status
        if snap.connected:
            c.itemconfig(self._status_id, text="● Connected", fill=_ACTIVE)
        else:
            c.itemconfig(self._status_id, text="● Disconnected", fill="#e74c3c")

        # Buttons
        mask_map = {
            "A": BTN_A, "B": BTN_B, "X": BTN_X, "Y": BTN_Y,
            "LB": BTN_LB, "RB": BTN_RB,
            "START": BTN_START, "BACK": BTN_BACK,
            "LS": BTN_LS, "RS": BTN_RS,
            "DPAD_UP": DPAD_UP, "DPAD_DOWN": DPAD_DOWN,
            "DPAD_LEFT": DPAD_LEFT, "DPAD_RIGHT": DPAD_RIGHT,
        }
        buttons = snap.buttons  # type: ignore[attr-defined]
        for name, mask in mask_map.items():
            if name in self._btn_items:
                color = self._btn_colors[name] if (buttons & mask) else _DIM
                c.itemconfig(self._btn_items[name], fill=color)

        # Triggers
        self._set_trigger("LT", snap.lt, 95)
        self._set_trigger("RT", snap.rt, 405)

        # Sticks
        self._set_stick("L", snap.lx, snap.ly)
        self._set_stick("R", snap.rx, snap.ry)

    def _set_trigger(self, name: str, value: float, center_x: int) -> None:
        bar = self._trig_bars.get(name)
        if bar is None:
            return
        x0 = center_x - 43
        x1 = center_x + 43
        filled = x0 + int((x1 - x0) * max(0.0, min(1.0, value)))
        self._canvas.coords(bar, x0, 30, max(x0, filled), 50)  # type: ignore

    def _set_stick(self, side: str, nx: float, ny: float) -> None:
        entry = self._stick_dots.get(side)
        if entry is None:
            return
        cx, cy, dot_id, travel = entry
        px = cx + int(nx * travel)
        py = cy - int(ny * travel)   # screen Y is inverted
        dr = 9
        self._canvas.coords(dot_id, px - dr, py - dr, px + dr, py + dr)  # type: ignore

    def _show_event(self, label: str, duration_ms: int) -> None:
        c = self._canvas
        if c and self._shot_id is not None:
            c.itemconfig(self._shot_id, text=label)
            self._root.after(duration_ms,
                             lambda: c.itemconfig(self._shot_id, text=""))

    def _set_profile(self, name: str) -> None:
        c = self._canvas
        if c and self._profile_id is not None:
            c.itemconfig(self._profile_id, text=f"profile: {name}")

    # ── Drag ──────────────────────────────────────────────────────────────────

    def _drag_start(self, event: tk.Event) -> None:
        self._drag_ox = event.x
        self._drag_oy = event.y

    def _drag_move(self, event: tk.Event) -> None:
        x = self._root.winfo_x() + (event.x - self._drag_ox)
        y = self._root.winfo_y() + (event.y - self._drag_oy)
        self._root.geometry(f"+{x}+{y}")
