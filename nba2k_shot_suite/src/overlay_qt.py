"""
Always-on-top shot HUD overlay for NBA 2K26 Auto Suite.

Features:
  - Draggable, minimizable (collapses to a slim title bar)
  - Real-time shot meter fill bar mirroring the in-game meter
  - Green zone indicator showing where to aim
  - Vision / Timer mode toggle
  - "SELECT METER AREA" button — triggers live ROI picker
  - Shot result flash (GREEN ✓ / TIMING / EARLY)
  - Works on top of borderless-windowed and fullscreen games

Runs on the main Qt thread. ShotSuite feeds state via set_state()
which is safe to call from any thread (uses QMetaObject.invokeMethod).
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from PyQt6.QtCore import Qt, QRect, QPoint, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import (
    QColor, QPainter, QBrush, QPen, QFont, QFontMetrics,
    QLinearGradient, QMouseEvent, QPaintEvent,
)
from PyQt6.QtWidgets import QApplication, QWidget


# ── Palette ───────────────────────────────────────────────────────────────────
_BG        = QColor(10, 10, 22, 230)
_SURFACE   = QColor(20, 20, 44, 255)
_BORDER    = QColor(37, 37, 64)
_TEXT      = QColor(208, 208, 232)
_DIM       = QColor(90, 90, 128)
_GREEN     = QColor(0, 255, 153)
_BLUE      = QColor(74, 158, 255)
_ORANGE    = QColor(255, 107, 53)
_YELLOW    = QColor(240, 192, 64)
_PURPLE    = QColor(123, 47, 247)
_RED       = QColor(231, 76, 60)


# ── Thread-safe bridge ────────────────────────────────────────────────────────

class _StateBridge(QObject):
    """Carries state updates from any thread into Qt's main thread."""
    state_updated = pyqtSignal(dict)


# ── Overlay widget ────────────────────────────────────────────────────────────

class ShotOverlay(QWidget):
    """
    Compact always-on-top HUD.

    Inject state from the poll thread via:
        overlay.push_state(state_dict)   # thread-safe
    """

    _W       = 210
    _H_FULL  = 310
    _H_MIN   = 34
    _RADIUS  = 10
    _BAR_W   = 36
    _BAR_H   = 130

    def __init__(self, suite: Any) -> None:
        super().__init__()
        self._suite    = suite
        self._state: dict[str, Any] = {}
        self._minimized = False
        self._drag_start: Optional[QPoint] = None
        self._last_event = ""
        self._event_color = _GREEN

        # Button rects (calculated during paint, used for click detection)
        self._btn_area: Optional[QRect] = None
        self._btn_mode: Optional[QRect] = None
        self._btn_min:  Optional[QRect] = None

        # Thread-safe bridge
        self._bridge = _StateBridge()
        self._bridge.state_updated.connect(self._on_state_updated)

        self._setup_window()

        # Refresh at 30 fps even when no push arrives
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(33)

    # ── Public API (call from any thread) ─────────────────────────────────────

    def push_state(self, state: dict[str, Any]) -> None:
        """Thread-safe state injection."""
        self._bridge.state_updated.emit(state)

    # ── Window setup ──────────────────────────────────────────────────────────

    def _setup_window(self) -> None:
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(self._W, self._H_FULL)

        # Default position: top-right corner, 20px margin
        screen = QApplication.primaryScreen()
        if screen:
            sg = screen.geometry()
            self.move(sg.width() - self._W - 20, 20)

    # ── Slot: receive state on main thread ────────────────────────────────────

    def _on_state_updated(self, state: dict[str, Any]) -> None:
        self._state = state
        ev = state.get("event", "")
        if ev:
            self._last_event = ev
            if "GREEN" in ev:
                self._event_color = _GREEN
            elif "EARLY" in ev:
                self._event_color = _YELLOW
            elif "VISION" in ev:
                self._event_color = _PURPLE
            else:
                self._event_color = _ORANGE
        self.update()

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        h = self.height()

        # Main background
        p.setPen(QPen(_BORDER, 1))
        p.setBrush(QBrush(_BG))
        p.drawRoundedRect(0, 0, self._W, h, self._RADIUS, self._RADIUS)

        self._paint_title(p)

        if not self._minimized:
            self._paint_body(p)

        p.end()

    def _paint_title(self, p: QPainter) -> None:
        s = self._state
        connected = s.get("connected", False)
        shot_active = s.get("shot_active", False)

        # Status dot
        dot = _GREEN if connected else _RED
        if shot_active:
            dot = _ORANGE
        p.setBrush(QBrush(dot))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(11, 11, 10, 10)

        # Title text
        p.setPen(QPen(_GREEN))
        f = QFont("Segoe UI", 10, QFont.Weight.Bold)
        p.setFont(f)
        p.drawText(27, 22, "2K AUTO")

        # Mode tag
        vm = s.get("vision_mode", False)
        tag_text = " VISION " if vm else " TIMER "
        tag_color = _PURPLE if vm else _ORANGE
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(tag_color))
        tag_x = 27 + QFontMetrics(f).horizontalAdvance("2K AUTO") + 6
        p.drawRoundedRect(tag_x, 8, QFontMetrics(QFont("Segoe UI", 7)).horizontalAdvance(tag_text) + 4, 16, 3, 3)
        p.setPen(QPen(QColor(10, 10, 22)))
        p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        p.drawText(tag_x + 2, 20, tag_text.strip())

        # Minimize button
        min_x = self._W - 32
        self._btn_min = QRect(min_x, 6, 22, 22)
        p.setPen(QPen(_DIM))
        p.setBrush(QBrush(_SURFACE))
        p.drawRoundedRect(self._btn_min, 4, 4)
        p.setPen(QPen(_TEXT))
        p.setFont(QFont("Segoe UI", 11))
        p.drawText(self._btn_min, Qt.AlignmentFlag.AlignCenter, "+" if self._minimized else "−")

    def _paint_body(self, p: QPainter) -> None:
        s   = self._state
        m   = s.get("meter", {})
        cfg = s.get("meter_cfg", {})

        fill_pct    = float(m.get("fill_pct", 0.0))
        vel_per_ms  = float(m.get("velocity_pct_per_ms", 0.0))
        detected    = bool(m.get("fill_detected", False))
        green_det   = bool(m.get("green_detected", False))
        green_start = float(cfg.get("green_start_pct", 0.55))
        green_end   = float(cfg.get("green_end_pct", 0.65))
        latency_ms  = float(m.get("latency_ms", 8.0))

        y = 38

        # ── Fill bar ──────────────────────────────────────────────────────────
        bx = 12
        by = y
        bw = self._BAR_W
        bh = self._BAR_H

        # Background
        p.setBrush(QBrush(QColor(8, 8, 20)))
        p.setPen(QPen(_BORDER))
        p.drawRoundedRect(bx, by, bw, bh, 4, 4)

        # Green zone band
        gz_top = by + int(bh * (1.0 - green_end))
        gz_h   = max(2, int(bh * (green_end - green_start)))
        p.setBrush(QBrush(QColor(0, 255, 153, 35)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRect(bx + 1, gz_top, bw - 2, gz_h)
        # Green zone border lines
        p.setPen(QPen(_GREEN, 1))
        p.drawLine(bx + 1, gz_top, bx + bw - 1, gz_top)
        p.drawLine(bx + 1, gz_top + gz_h, bx + bw - 1, gz_top + gz_h)

        # Fill bar itself
        fill_h = max(0, int(bh * min(1.0, fill_pct)))
        fy = by + bh - fill_h
        if fill_h > 0:
            if green_det:
                grad = QLinearGradient(bx, fy, bx, fy + fill_h)
                grad.setColorAt(0, _GREEN)
                grad.setColorAt(1, QColor(0, 180, 100))
                p.setBrush(QBrush(grad))
            elif detected:
                grad = QLinearGradient(bx, fy, bx, fy + fill_h)
                grad.setColorAt(0, _BLUE)
                grad.setColorAt(1, QColor(30, 100, 200))
                p.setBrush(QBrush(grad))
            else:
                p.setBrush(QBrush(_DIM))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(bx + 2, fy, bw - 4, fill_h, 3, 3)

        # Fill % label inside bar (centered at top of fill)
        if detected and fill_h > 14:
            p.setPen(QPen(QColor(0, 0, 0, 180)))
            p.setFont(QFont("Consolas", 7, QFont.Weight.Bold))
            label = f"{fill_pct*100:.0f}%"
            lw = QFontMetrics(p.font()).horizontalAdvance(label)
            p.drawText(bx + (bw - lw) // 2, fy + 11, label)

        # ── Stats column ──────────────────────────────────────────────────────
        sx = bx + bw + 10
        sy = by

        def stat(label: str, value: str, val_color: QColor, y_offset: int) -> None:
            p.setFont(QFont("Segoe UI", 8))
            p.setPen(QPen(_DIM))
            p.drawText(sx, sy + y_offset, label)
            p.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
            p.setPen(QPen(val_color))
            p.drawText(sx, sy + y_offset + 14, value)

        stat("Fill", f"{fill_pct*100:.0f}%",
             _GREEN if green_det else (_BLUE if detected else _DIM), 0)

        vel_disp = vel_per_ms * 1000.0
        vel_label = f"{vel_disp:.1f}%/s"
        vel_color = _YELLOW if vel_disp > 80 else (_BLUE if vel_disp > 20 else _DIM)
        stat("Speed", vel_label, vel_color, 32)

        stat("Green",
             "YES ✓" if green_det else ("no" if detected else "—"),
             _GREEN if green_det else _DIM, 64)

        stat("Lag", f"{latency_ms:.0f}ms", _DIM, 96)

        y = by + bh + 10

        # ── Vision mode status bar ────────────────────────────────────────────
        vm = s.get("vision_mode", False)
        backend = s.get("vision_backend", "none")
        vm_text = f"{'● LIVE' if (vm and detected) else ('○ SCANNING' if vm else '○ TIMER MODE')}"
        vm_color = _GREEN if (vm and detected) else (_YELLOW if vm else _DIM)
        p.setPen(QPen(vm_color))
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        p.drawText(12, y + 13, vm_text)
        if backend not in ("none", ""):
            p.setPen(QPen(_DIM))
            p.setFont(QFont("Segoe UI", 7))
            p.drawText(self._W - 60, y + 13, f"[{backend}]")
        y += 20

        # ── Last event ────────────────────────────────────────────────────────
        if self._last_event:
            ev_rect = QRect(12, y, self._W - 24, 24)
            p.setBrush(QBrush(QColor(self._event_color.red(),
                                     self._event_color.green(),
                                     self._event_color.blue(), 25)))
            p.setPen(QPen(self._event_color, 1))
            p.drawRoundedRect(ev_rect, 4, 4)
            p.setPen(QPen(self._event_color))
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            p.drawText(ev_rect, Qt.AlignmentFlag.AlignCenter, self._last_event[:26])
        y += 30

        # ── Buttons ───────────────────────────────────────────────────────────
        bw2 = (self._W - 28) // 2
        self._btn_area = QRect(12, y, bw2, 26)
        self._btn_mode = QRect(16 + bw2, y, bw2, 26)

        self._draw_btn(p, self._btn_area, "SET METER AREA", _SURFACE)
        mode_label = "DISABLE VISION" if vm else "ENABLE VISION"
        mode_bg = QColor(60, 20, 110) if vm else QColor(50, 20, 100)
        self._draw_btn(p, self._btn_mode, mode_label, mode_bg, _PURPLE)

    def _draw_btn(
        self,
        p: QPainter,
        rect: QRect,
        text: str,
        bg: QColor,
        text_color: QColor = _TEXT,
    ) -> None:
        p.setBrush(QBrush(bg))
        p.setPen(QPen(_BORDER))
        p.drawRoundedRect(rect, 5, 5)
        p.setPen(QPen(text_color))
        p.setFont(QFont("Segoe UI", 7, QFont.Weight.Bold))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    # ── Mouse: drag + click ───────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position().toPoint()

        # Minimize toggle
        if self._btn_min and self._btn_min.contains(pos):
            self._toggle_minimize()
            return

        if not self._minimized:
            if self._btn_area and self._btn_area.contains(pos):
                self._run_calibration()
                return
            if self._btn_mode and self._btn_mode.contains(pos):
                self._toggle_vision()
                return

        self._drag_start = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_start and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_start)

    def mouseReleaseEvent(self, _: QMouseEvent) -> None:
        self._drag_start = None

    # ── Actions ───────────────────────────────────────────────────────────────

    def _toggle_minimize(self) -> None:
        self._minimized = not self._minimized
        self.setFixedHeight(self._H_MIN if self._minimized else self._H_FULL)

    def _toggle_vision(self) -> None:
        vm = self._state.get("vision_mode", False)
        self._suite.set_vision_mode(not vm)

    def _run_calibration(self) -> None:
        def _bg() -> None:
            try:
                from .calibrator import run_calibration, load_meter_config
                run_calibration()
                cfg = load_meter_config()
                self._suite.update_meter_config(cfg)
                print("[Overlay] Calibration saved and applied.")
            except Exception as exc:
                print(f"[Overlay] Calibration error: {exc}")
        threading.Thread(target=_bg, daemon=True, name="calibrator").start()
