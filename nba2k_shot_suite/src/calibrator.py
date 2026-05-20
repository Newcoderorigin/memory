"""
Interactive shot meter calibration tool.

Usage (run standalone before launching the suite):
  python -m nba2k_shot_suite.src.calibrator

Steps:
  1. Launch NBA 2K26 and open Practice mode.
  2. Run this script. It takes a screenshot of your primary display.
  3. A window appears — drag to select the ROI enclosing the shot meter.
     Press ENTER/SPACE to confirm, C to re-select.
  4. A live preview window shows the current fill + green detection.
     Adjust HSV sliders until the fill bar is cleanly detected.
  5. Press S to save config to meter_config.json (next to this script).

Requires: opencv-python, mss  (dxcam optional but recommended for speed).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent.parent / "meter_config.json"


def _grab_screenshot() -> "np.ndarray":
    try:
        import dxcam
        cam = dxcam.create(output_color="BGR")
        frame = None
        # dxcam.grab() may return None if no new frame; retry briefly
        deadline = time.perf_counter() + 2.0
        while frame is None and time.perf_counter() < deadline:
            frame = cam.grab()
        del cam
        if frame is not None:
            return frame
    except Exception:
        pass

    import mss, numpy as np
    with mss.mss() as sct:
        mon = sct.monitors[1]   # primary display
        img = sct.grab(mon)
        return np.array(img)[:, :, :3]   # BGRA → BGR


def run_calibration() -> None:
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("[Calibrator] cv2 / numpy not installed. Run: pip install opencv-python numpy")
        return

    print("[Calibrator] Taking screenshot — switch to NBA 2K26 before this starts…")
    for i in range(3, 0, -1):
        print(f"  {i}…")
        time.sleep(1.0)

    screenshot = _grab_screenshot()
    if screenshot is None:
        print("[Calibrator] Screenshot failed.")
        return

    # ── Step 1: ROI selection ─────────────────────────────────────────────────
    print("\n[Calibrator] Select the shot meter region in the window.")
    print("  Drag to select ROI → Enter/Space to confirm → C to re-select → Esc to quit")

    roi_rect = cv2.selectROI(
        "Select Shot Meter ROI  (Enter=confirm  C=redo  Esc=quit)",
        screenshot,
        showCrosshair=True,
        fromCenter=False,
    )
    cv2.destroyAllWindows()

    if roi_rect == (0, 0, 0, 0):
        print("[Calibrator] No ROI selected.")
        return

    x, y, w, h = roi_rect
    roi_left, roi_top = int(x), int(y)
    roi_right, roi_bottom = int(x + w), int(y + h)
    print(f"[Calibrator] ROI: left={roi_left} top={roi_top} right={roi_right} bottom={roi_bottom}")

    # ── Step 2: HSV threshold tuning via trackbars ────────────────────────────
    roi_bgr = screenshot[roi_top:roi_bottom, roi_left:roi_right]
    if roi_bgr.size == 0:
        print("[Calibrator] Empty ROI — try again.")
        return

    win = "Threshold Tuning  (S=save  Q=quit)"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 800, 600)

    # Defaults matching NBA 2K26 green zone
    def tb(name: str, default: int, maxval: int) -> None:
        cv2.createTrackbar(name, win, default, maxval, lambda _: None)

    tb("Fill V min", 100, 255)
    tb("Fill col%", 25, 100)
    tb("Green H lo", 45, 179)
    tb("Green H hi", 95, 179)
    tb("Green S lo", 60, 255)
    tb("Green V lo", 60, 255)

    print("\n[Calibrator] Adjust sliders until fill bar + green zone are detected.")
    print("  Green overlay = fill pixels   Red overlay = green zone pixels")
    print("  S = save config    Q = quit\n")

    while True:
        roi_bgr_live = screenshot[roi_top:roi_bottom, roi_left:roi_right].copy()
        hsv = cv2.cvtColor(roi_bgr_live, cv2.COLOR_BGR2HSV)

        fill_v  = cv2.getTrackbarPos("Fill V min", win)
        fill_cp = cv2.getTrackbarPos("Fill col%", win) / 100.0
        g_h_lo  = cv2.getTrackbarPos("Green H lo", win)
        g_h_hi  = cv2.getTrackbarPos("Green H hi", win)
        g_s_lo  = cv2.getTrackbarPos("Green S lo", win)
        g_v_lo  = cv2.getTrackbarPos("Green V lo", win)

        v_channel = hsv[:, :, 2]
        bright_mask = (v_channel > fill_v).astype(np.uint8) * 255

        green_lo = np.array([g_h_lo, g_s_lo, g_v_lo])
        green_hi = np.array([g_h_hi, 255, 255])
        green_mask = cv2.inRange(hsv, green_lo, green_hi)

        # Overlay: green tint for fill, red tint for green zone
        overlay = roi_bgr_live.copy()
        overlay[bright_mask > 0] = (0, 200, 0)
        overlay[green_mask > 0]  = (0, 0, 220)
        blended = cv2.addWeighted(roi_bgr_live, 0.5, overlay, 0.5, 0)

        # Row-wise fill detection
        h_px, w_px = roi_bgr_live.shape[:2]
        min_cols = max(1, int(w_px * fill_cp))
        row_counts = (bright_mask > 0).sum(axis=1)
        fill_rows = np.where(row_counts >= min_cols)[0]

        fill_pct = 0.0
        if len(fill_rows) > 0:
            topmost = fill_rows.min()
            fill_pct = (h_px - topmost) / h_px
            cv2.line(blended, (0, int(topmost)), (w_px, int(topmost)), (0, 255, 255), 2)

        green_detected = cv2.countNonZero(green_mask) > (h_px * w_px * 0.02)

        label = f"Fill: {fill_pct:.1%}  Green: {'YES' if green_detected else 'no'}"
        cv2.putText(blended, label, (4, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

        scale = max(1, 400 // max(w_px, 1))
        if scale > 1:
            blended = cv2.resize(blended, (w_px * scale, h_px * scale), interpolation=cv2.INTER_NEAREST)

        cv2.imshow(win, blended)
        key = cv2.waitKey(30) & 0xFF

        if key in (ord('q'), 27):
            print("[Calibrator] Quit without saving.")
            break

        if key == ord('s'):
            config = {
                "roi": [roi_left, roi_top, roi_right, roi_bottom],
                "fill_v_threshold": fill_v,
                "min_col_fraction": fill_cp,
                "green_h_lo": g_h_lo,
                "green_h_hi": g_h_hi,
                "green_s_lo": g_s_lo,
                "green_v_lo": g_v_lo,
                "latency_ms": 8.0,
                "kalman_Q": 0.02,
                "kalman_R": 4.0,
                "target_hz": 240,
            }
            _CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
            print(f"[Calibrator] Config saved → {_CONFIG_PATH}")
            break

    cv2.destroyAllWindows()


def load_meter_config() -> "MeterConfig":
    """Load saved MeterConfig or return defaults."""
    from .meter_detector import MeterConfig
    if _CONFIG_PATH.exists():
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            roi = tuple(data.get("roi", MeterConfig().roi))
            return MeterConfig(
                roi=roi,  # type: ignore[arg-type]
                fill_v_threshold=data.get("fill_v_threshold", 100),
                min_col_fraction=data.get("min_col_fraction", 0.25),
                green_h_lo=data.get("green_h_lo", 45),
                green_h_hi=data.get("green_h_hi", 95),
                green_s_lo=data.get("green_s_lo", 60),
                green_v_lo=data.get("green_v_lo", 60),
                latency_ms=data.get("latency_ms", 8.0),
                kalman_Q=data.get("kalman_Q", 0.02),
                kalman_R=data.get("kalman_R", 4.0),
                target_hz=data.get("target_hz", 240),
            )
        except Exception as exc:
            print(f"[Calibrator] Failed to load meter_config.json: {exc} — using defaults")
    return MeterConfig()


def save_meter_config(cfg: "MeterConfig") -> None:
    """Persist a MeterConfig to meter_config.json."""
    from .meter_detector import MeterConfig
    data = {
        "roi": list(cfg.roi),
        "fill_v_threshold": cfg.fill_v_threshold,
        "min_col_fraction": cfg.min_col_fraction,
        "green_h_lo": cfg.green_h_lo,
        "green_h_hi": cfg.green_h_hi,
        "green_s_lo": cfg.green_s_lo,
        "green_v_lo": cfg.green_v_lo,
        "latency_ms": cfg.latency_ms,
        "kalman_Q": cfg.kalman_Q,
        "kalman_R": cfg.kalman_R,
        "target_hz": cfg.target_hz,
    }
    tmp = _CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    import os
    os.replace(tmp, _CONFIG_PATH)


if __name__ == "__main__":
    run_calibration()
