"""
Screen capture utility — mss for ~2–5 ms per grab on Windows.

Provides auto-location of the NBA 2K26 window via win32gui so the
capture region automatically tracks the game even if it's windowed.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np

try:
    import mss
    _MSS_OK = True
except ImportError:
    _MSS_OK = False
    print("[ScreenCapture] mss not installed. Run: pip install mss")

# mss GDI contexts are thread-local — each thread that calls grab() needs its own
# mss.mss() instance.  _tls.sct is created lazily on first use in each thread.
_tls = threading.local()


@dataclass
class CaptureRegion:
    """Screen-space bounding box (pixels, absolute monitor coordinates)."""
    left:   int
    top:    int
    width:  int
    height: int

    def as_mss(self) -> dict:
        return {"left": self.left, "top": self.top,
                "width": self.width, "height": self.height}


class ScreenCapture:
    """
    Thread-safe screen grabber.  Call grab() from any thread.

    Default region covers the lower-center portion of a 1920×1080 display
    where the shot meter and player model appear in NBA 2K26.
    Override with set_region() or call auto_locate() to find the game window.
    """

    # Default for 1920×1080 — adjust if running at a different resolution
    DEFAULT_REGION = CaptureRegion(left=700, top=480, width=520, height=480)

    def __init__(self, region: Optional[CaptureRegion] = None) -> None:
        self._region      = region or self.DEFAULT_REGION
        self._region_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return _MSS_OK

    def _sct(self) -> Optional[object]:
        """Return (or create) this thread's mss instance."""
        if not _MSS_OK:
            return None
        if not getattr(_tls, "sct", None):
            _tls.sct = mss.mss()
        return _tls.sct

    def grab(self) -> Optional[np.ndarray]:
        """
        Capture the current region → BGR uint8 ndarray, or None on failure.
        Creates a per-thread mss context on first call — safe to call from
        any thread.
        """
        sct = self._sct()
        if sct is None:
            return None
        with self._region_lock:
            region = self._region.as_mss()
        try:
            raw = sct.grab(region)                 # type: ignore[attr-defined]
            return np.array(raw)[:, :, :3]         # BGRA → BGR
        except Exception as exc:
            # Context may be stale after a screen layout change — reset it
            _tls.sct = None
            print(f"[ScreenCapture] grab error (context reset): {exc}")
            return None

    def set_region(self, region: CaptureRegion) -> None:
        with self._region_lock:
            self._region = region

    def auto_locate(self) -> bool:
        """
        Find the NBA 2K26 window via win32gui and set the capture region to
        cover the player / shot-meter area (lower-center 40% of the window).
        Returns True if the window was found, False otherwise.
        """
        try:
            import win32gui

            found: list[CaptureRegion] = []

            def _visit(hwnd: int, _: object) -> None:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                title = win32gui.GetWindowText(hwnd)
                if "2K26" in title or "NBA 2K" in title:
                    x, y, x2, y2 = win32gui.GetWindowRect(hwnd)
                    w, h = x2 - x, y2 - y
                    found.append(CaptureRegion(
                        left   = x + int(w * 0.28),
                        top    = y + int(h * 0.32),
                        width  = int(w * 0.44),
                        height = int(h * 0.52),
                    ))

            win32gui.EnumWindows(_visit, None)

            if found:
                self.set_region(found[0])
                print(f"[ScreenCapture] Game window found — region: {found[0]}")
                return True

        except ImportError:
            pass
        except Exception as exc:
            print(f"[ScreenCapture] auto_locate error: {exc}")

        return False
