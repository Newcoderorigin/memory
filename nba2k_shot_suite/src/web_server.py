"""
FastAPI web dashboard for the NBA 2K26 Shot Suite.

Architecture
────────────
  sync XInput thread  ──call_soon_threadsafe──▶  asyncio.Queue
  asyncio broadcaster task  ◀── drains queue ──▶  WebSocket fan-out
  FastAPI REST endpoints  ◀──────────────────────  browser

The event loop reference is captured inside the lifespan context so the
sync push_state() function can inject frames without polling or locking.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, field_validator, model_validator

# ── Shared globals (set before server starts) ─────────────────────────────────
_config_mgr: Optional[Any] = None   # ConfigManager
_suite_ref:  Optional[Any] = None   # ShotSuite

# ── Async-domain globals (set inside lifespan) ────────────────────────────────
_loop:        Optional[asyncio.AbstractEventLoop] = None
_state_queue: Optional[asyncio.Queue[str]]        = None
_manager:     Optional["ConnectionManager"]       = None
_loop_ready   = threading.Event()


# ── WebSocket connection manager ──────────────────────────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: str) -> None:
        async with self._lock:
            clients = set(self._clients)
        dead: set[WebSocket] = set()
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        if dead:
            async with self._lock:
                self._clients -= dead

    @property
    def count(self) -> int:
        return len(self._clients)


# ── Background broadcaster task ───────────────────────────────────────────────

async def _broadcaster() -> None:
    while True:
        try:
            msg = await _state_queue.get()  # type: ignore[union-attr]
            if _manager is not None:
                await _manager.broadcast(msg)
        except asyncio.CancelledError:
            break
        except Exception:
            pass


# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _loop, _state_queue, _manager
    _loop = asyncio.get_running_loop()
    _state_queue = asyncio.Queue(maxsize=64)
    _manager = ConnectionManager()
    _loop_ready.set()

    task = asyncio.create_task(_broadcaster())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="2K26 Shot Suite", lifespan=_lifespan)


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(_DASHBOARD_HTML)


@app.get("/api/state")
async def api_state() -> dict[str, Any]:
    if _suite_ref is None:
        return {"error": "suite not initialised"}
    return _suite_ref.current_state_dict()


@app.get("/api/config")
async def api_config() -> dict[str, Any]:
    if _config_mgr is None:
        return {}
    return _config_mgr.get_dict()


@app.get("/api/profiles")
async def api_profiles() -> dict[str, Any]:
    from .shot_timer import PROFILES
    return {
        name: {
            "animation_ms": p.animation_ms,
            "green_start_pct": p.green_start_pct,
            "green_end_pct": p.green_end_pct,
            "aim_percentile": p.aim_percentile,
            "release_ms": round(p.release_ms, 1),
            "green_window_ms": round(p.green_window_ms, 1),
        }
        for name, p in PROFILES.items()
    }


class ProfileSwitch(BaseModel):
    name: str


@app.post("/api/profile")
async def api_switch_profile(body: ProfileSwitch) -> dict[str, str]:
    from .shot_timer import PROFILES
    if body.name not in PROFILES:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Unknown profile: {body.name!r}")
    if _config_mgr is not None:
        _config_mgr.switch_profile(body.name)
    return {"status": "ok", "profile": body.name}


class SettingsBody(BaseModel):
    active_profile:    Optional[str]   = None
    animation_ms:      Optional[float] = None
    green_start_pct:   Optional[float] = None
    green_end_pct:     Optional[float] = None
    aim_percentile:    Optional[float] = None
    hbr_sigma_ms:      Optional[float] = None
    hbr_tau_ms:        Optional[float] = None
    hold_base_ms:      Optional[float] = None
    ramp_steps:        Optional[int]   = None
    ramp_exponent:     Optional[float] = None
    stick_noise_sigma: Optional[float] = None
    vision_mode:       Optional[bool]  = None
    vision_latency_ms: Optional[float] = None

    @field_validator("green_start_pct", "green_end_pct", "aim_percentile", mode="after")
    @classmethod
    def _pct_range(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("must be between 0.0 and 1.0")
        return v

    @field_validator("animation_ms", "hbr_sigma_ms", "hbr_tau_ms", "hold_base_ms", mode="after")
    @classmethod
    def _positive_ms(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("must be positive")
        return v

    @model_validator(mode="after")
    def _green_window_order(self) -> "SettingsBody":
        s = self.green_start_pct
        e = self.green_end_pct
        if s is not None and e is not None and s >= e:
            raise ValueError("green_start_pct must be < green_end_pct")
        return self


@app.post("/api/settings")
async def api_settings(body: SettingsBody) -> dict[str, Any]:
    if _config_mgr is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Config manager not ready")

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        _config_mgr.apply_dict(updates)
    except (ValueError, KeyError) as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # If vision_mode toggled, notify suite
    if "vision_mode" in updates and _suite_ref is not None:
        _suite_ref.set_vision_mode(bool(updates["vision_mode"]))
    if "vision_latency_ms" in updates and _suite_ref is not None:
        _suite_ref.set_vision_latency(float(updates["vision_latency_ms"]))

    return {"status": "ok", "applied": updates}


class MeterConfigBody(BaseModel):
    roi_left:          Optional[int]   = None
    roi_top:           Optional[int]   = None
    roi_right:         Optional[int]   = None
    roi_bottom:        Optional[int]   = None
    fill_v_threshold:  Optional[int]   = None
    min_col_fraction:  Optional[float] = None
    green_h_lo:        Optional[int]   = None
    green_h_hi:        Optional[int]   = None
    green_s_lo:        Optional[int]   = None
    green_v_lo:        Optional[int]   = None
    latency_ms:        Optional[float] = None
    kalman_Q:          Optional[float] = None
    kalman_R:          Optional[float] = None


@app.get("/api/meter_config")
async def api_meter_config() -> dict[str, Any]:
    if _suite_ref is None:
        return {}
    cfg = _suite_ref.get_meter_config()
    l, t, r, b = cfg.roi
    return {
        "roi_left": l, "roi_top": t, "roi_right": r, "roi_bottom": b,
        "fill_v_threshold": cfg.fill_v_threshold,
        "min_col_fraction": cfg.min_col_fraction,
        "green_h_lo": cfg.green_h_lo,
        "green_h_hi": cfg.green_h_hi,
        "green_s_lo": cfg.green_s_lo,
        "green_v_lo": cfg.green_v_lo,
        "latency_ms": cfg.latency_ms,
        "kalman_Q": cfg.kalman_Q,
        "kalman_R": cfg.kalman_R,
        "backend": _suite_ref.vision_backend,
    }


@app.post("/api/meter_config")
async def api_set_meter_config(body: MeterConfigBody) -> dict[str, Any]:
    if _suite_ref is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Suite not ready")

    from .meter_detector import MeterConfig
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    current = _suite_ref.get_meter_config()
    l, t, r, b = current.roi
    new_cfg = MeterConfig(
        roi=(
            updates.get("roi_left", l),
            updates.get("roi_top", t),
            updates.get("roi_right", r),
            updates.get("roi_bottom", b),
        ),
        fill_v_threshold=updates.get("fill_v_threshold", current.fill_v_threshold),
        min_col_fraction=updates.get("min_col_fraction", current.min_col_fraction),
        green_h_lo=updates.get("green_h_lo", current.green_h_lo),
        green_h_hi=updates.get("green_h_hi", current.green_h_hi),
        green_s_lo=updates.get("green_s_lo", current.green_s_lo),
        green_v_lo=updates.get("green_v_lo", current.green_v_lo),
        latency_ms=updates.get("latency_ms", current.latency_ms),
        kalman_Q=updates.get("kalman_Q", current.kalman_Q),
        kalman_R=updates.get("kalman_R", current.kalman_R),
    )
    _suite_ref.update_meter_config(new_cfg)
    return {"status": "ok"}


@app.post("/api/calibrate")
async def api_calibrate() -> dict[str, str]:
    """Trigger the interactive calibration tool in a background thread."""
    if _suite_ref is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Suite not ready")

    def _run() -> None:
        try:
            from .calibrator import run_calibration, load_meter_config
            run_calibration()
            cfg = load_meter_config()
            _suite_ref.update_meter_config(cfg)
        except Exception as exc:
            print(f"[Calibrator] Error: {exc}")

    threading.Thread(target=_run, name="calibrator", daemon=True).start()
    return {"status": "calibration started — check the cv2 window"}


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws/state")
async def ws_state(ws: WebSocket) -> None:
    if _manager is None:
        await ws.close(code=1013)
        return
    await _manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await _manager.disconnect(ws)


# ── Public sync API (call from any thread) ────────────────────────────────────

def push_state(state: dict[str, Any]) -> None:
    if _loop is None or _state_queue is None:
        return
    msg = json.dumps(state)

    def _safe_put() -> None:
        try:
            _state_queue.put_nowait(msg)  # type: ignore[union-attr]
        except asyncio.QueueFull:
            pass

    try:
        _loop.call_soon_threadsafe(_safe_put)
    except RuntimeError:
        pass


def start_web_server(
    config_mgr: Any,
    suite: Any,
    host: str = "127.0.0.1",
    port: int = 8420,
) -> None:
    global _config_mgr, _suite_ref
    _config_mgr = config_mgr
    _suite_ref = suite

    uconfig = uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False)
    server = uvicorn.Server(uconfig)

    t = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    t.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn failed to start within 10 s")
        time.sleep(0.05)

    _loop_ready.wait(timeout=5.0)
    print(f"\n  Dashboard → http://{host}:{port}\n")


# ── Inline dashboard HTML ─────────────────────────────────────────────────────

_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>2K26 Shot Suite</title>
<style>
:root{
  --bg:#0d0d1e;--surface:#161628;--border:#252540;
  --text:#d0d0e8;--dim:#5a5a80;--accent:#00ff99;
  --a:#2ecc71;--b:#e74c3c;--x:#4a9eff;--y:#f0c040;
  --trig:#ff6b35;--danger:#e74c3c;--inactive:#252540;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:13px;padding:14px;min-height:100vh}
h1{font-size:17px;font-weight:700;color:var(--accent);letter-spacing:.02em}
.header{display:flex;justify-content:space-between;align-items:center;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin-bottom:12px}
.ws-row{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--dim)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--danger);transition:background .3s}
.dot.on{background:var(--accent)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.span2{grid-column:1/-1}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px}
.panel h2{font-size:11px;font-weight:700;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;margin-bottom:12px}
.ctrl-wrap{display:flex;justify-content:center;padding:8px 0}
svg text{pointer-events:none;user-select:none}
.field{margin-bottom:9px}
.field label{display:block;font-size:11px;color:var(--dim);margin-bottom:3px}
.field input,.field select{width:100%;background:#0a0a18;border:1px solid var(--border);color:var(--text);padding:5px 8px;border-radius:4px;font-size:12px}
.field input:focus,.field select:focus{outline:none;border-color:var(--accent)}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.save-btn{width:100%;background:var(--accent);color:#0a0a18;border:none;padding:9px;border-radius:5px;font-size:13px;font-weight:700;cursor:pointer;margin-top:10px;letter-spacing:.02em}
.save-btn:hover{filter:brightness(1.1)}
.save-btn:active{filter:brightness(.9)}
.save-msg{font-size:11px;color:var(--accent);text-align:center;height:16px;margin-top:4px}
.save-msg.err{color:var(--danger)}
.log{height:210px;overflow-y:auto;font-family:'Consolas',monospace;font-size:11px;display:flex;flex-direction:column-reverse}
.ev{padding:3px 5px;border-radius:3px;margin-bottom:2px}
.ev-green{color:var(--accent)}
.ev-armed{color:var(--trig)}
.ev-vision{color:#c77dff}
.ev-dim{color:var(--dim)}
.badge{display:inline-block;padding:1px 6px;border-radius:10px;font-size:10px;font-weight:700;background:var(--x);color:#fff;margin-left:6px}
.badge-vision{background:#7b2ff7}
/* Shot meter */
.meter-wrap{display:flex;gap:14px;align-items:flex-end;padding:8px 0}
.meter-bar-outer{width:28px;height:180px;background:#0a0a18;border:1px solid var(--border);border-radius:4px;position:relative;overflow:hidden}
.meter-bar-fill{position:absolute;bottom:0;left:0;right:0;height:0%;background:var(--dim);border-radius:0 0 3px 3px;transition:height .05s linear}
.meter-bar-green-zone{position:absolute;left:0;right:0;background:rgba(0,255,153,.18);border-top:1px solid var(--accent);border-bottom:1px solid var(--accent)}
.meter-info{font-size:12px;line-height:1.7}
.meter-info b{color:var(--accent)}
.toggle-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.toggle{position:relative;display:inline-block;width:42px;height:22px}
.toggle input{opacity:0;width:0;height:0}
.slider-sw{position:absolute;cursor:pointer;top:0;left:0;right:0;bottom:0;background:#252540;border-radius:22px;transition:.3s}
.slider-sw:before{position:absolute;content:"";height:16px;width:16px;left:3px;bottom:3px;background:#8888aa;border-radius:50%;transition:.3s}
input:checked + .slider-sw{background:var(--accent)}
input:checked + .slider-sw:before{transform:translateX(20px);background:#0a0a18}
</style>
</head>
<body>
<div class="header">
  <div style="display:flex;align-items:center;gap:10px">
    <h1>2K26 Shot Suite</h1>
    <span class="badge">X = SHOT</span>
    <span class="badge badge-vision" id="visionBadge" style="display:none">VISION</span>
  </div>
  <div class="ws-row">
    <div class="dot" id="wsDot"></div>
    <span id="wsLabel">Connecting…</span>
  </div>
</div>

<div class="grid">
  <!-- Controller diagram -->
  <div class="panel span2">
    <h2>Controller Map</h2>
    <div class="ctrl-wrap">
      <svg id="ctrl" viewBox="0 0 500 280" width="500" height="280" xmlns="http://www.w3.org/2000/svg">
        <ellipse cx="250" cy="155" rx="198" ry="108" fill="#1a1a30" stroke="#2a2a48" stroke-width="2"/>
        <ellipse cx="95"  cy="218" rx="68"  ry="58"  fill="#141428" stroke="#2a2a48" stroke-width="1"/>
        <ellipse cx="405" cy="218" rx="68"  ry="58"  fill="#141428" stroke="#2a2a48" stroke-width="1"/>
        <!-- LT -->
        <rect x="52" y="22" width="110" height="24" rx="4" fill="#0a0a18" stroke="#2a2a48"/>
        <rect id="lt-fill" x="54" y="24" width="0" height="20" rx="3" fill="#ff6b35"/>
        <text x="107" y="37" text-anchor="middle" fill="#5a5a80" font-size="10" font-weight="600">LT</text>
        <!-- RT -->
        <rect x="338" y="22" width="110" height="24" rx="4" fill="#0a0a18" stroke="#2a2a48"/>
        <rect id="rt-fill" x="340" y="24" width="0" height="20" rx="3" fill="#ff6b35"/>
        <text x="393" y="37" text-anchor="middle" fill="#5a5a80" font-size="10" font-weight="600">RT</text>
        <!-- LB / RB -->
        <rect id="btn-lb" x="62" y="62" width="88" height="20" rx="6" fill="#252540" stroke="#2a2a48"/>
        <text x="106" y="75" text-anchor="middle" fill="#8888aa" font-size="10" font-weight="600">LB</text>
        <rect id="btn-rb" x="350" y="62" width="88" height="20" rx="6" fill="#252540" stroke="#2a2a48"/>
        <text x="394" y="75" text-anchor="middle" fill="#8888aa" font-size="10" font-weight="600">RB</text>
        <!-- Guide -->
        <circle cx="250" cy="140" r="14" fill="#0d0d1e" stroke="#2a2a48" stroke-width="1.5"/>
        <circle cx="250" cy="140" r="7" fill="#1a1a30"/>
        <!-- Back / Start -->
        <ellipse id="btn-back"  cx="213" cy="150" rx="16" ry="11" fill="#252540" stroke="#2a2a48"/>
        <text x="213" y="154" text-anchor="middle" fill="#8888aa" font-size="9">⊲</text>
        <ellipse id="btn-start" cx="287" cy="150" rx="16" ry="11" fill="#252540" stroke="#2a2a48"/>
        <text x="287" y="154" text-anchor="middle" fill="#8888aa" font-size="9">⊳</text>
        <!-- D-pad -->
        <circle id="btn-dup"    cx="152" cy="188" r="12" fill="#252540" stroke="#2a2a48"/>
        <text x="152" y="192" text-anchor="middle" fill="#8888aa" font-size="11">↑</text>
        <circle id="btn-ddown"  cx="152" cy="222" r="12" fill="#252540" stroke="#2a2a48"/>
        <text x="152" y="226" text-anchor="middle" fill="#8888aa" font-size="11">↓</text>
        <circle id="btn-dleft"  cx="130" cy="205" r="12" fill="#252540" stroke="#2a2a48"/>
        <text x="130" y="209" text-anchor="middle" fill="#8888aa" font-size="11">←</text>
        <circle id="btn-dright" cx="174" cy="205" r="12" fill="#252540" stroke="#2a2a48"/>
        <text x="174" y="209" text-anchor="middle" fill="#8888aa" font-size="11">→</text>
        <!-- Left stick -->
        <circle cx="138" cy="188" r="32" fill="#0a0a18" stroke="#2a2a48" stroke-width="1.5"/>
        <circle id="btn-ls" cx="138" cy="188" r="12" fill="#252540" stroke="#2a2a48"/>
        <circle id="ls-dot" cx="138" cy="188" r="7" fill="#4a9eff" opacity=".8"/>
        <!-- Right stick -->
        <circle cx="292" cy="220" r="32" fill="#0a0a18" stroke="#2a2a48" stroke-width="1.5"/>
        <circle id="btn-rs" cx="292" cy="220" r="12" fill="#252540" stroke="#2a2a48"/>
        <circle id="rs-dot" cx="292" cy="220" r="7" fill="#4a9eff" opacity=".8"/>
        <!-- Face buttons -->
        <circle cx="332" cy="168" r="18" fill="none" stroke="#4a9eff" stroke-width="1" opacity=".4" id="x-ring"/>
        <circle id="btn-x" cx="332" cy="168" r="14" fill="#252540" stroke="#4a9eff" stroke-width="1.5"/>
        <text x="332" y="172" text-anchor="middle" fill="#4a9eff" font-size="11" font-weight="700">X</text>
        <circle id="btn-y" cx="358" cy="142" r="14" fill="#252540" stroke="#2a2a48"/>
        <text x="358" y="147" text-anchor="middle" fill="#8888aa" font-size="11" font-weight="700">Y</text>
        <circle id="btn-b" cx="384" cy="168" r="14" fill="#252540" stroke="#2a2a48"/>
        <text x="384" y="172" text-anchor="middle" fill="#8888aa" font-size="11" font-weight="700">B</text>
        <circle id="btn-a" cx="358" cy="194" r="14" fill="#252540" stroke="#2a2a48"/>
        <text x="358" y="199" text-anchor="middle" fill="#8888aa" font-size="11" font-weight="700">A</text>
        <text id="shot-label" x="250" y="270" text-anchor="middle" fill="#00ff99" font-size="12" font-weight="700" opacity="0"></text>
      </svg>
    </div>
  </div>

  <!-- Shot meter panel -->
  <div class="panel">
    <h2>Shot Meter <span id="backendLabel" style="font-weight:400;text-transform:none;color:var(--dim);font-size:10px"></span></h2>
    <div class="toggle-row">
      <label class="toggle">
        <input type="checkbox" id="visionToggle">
        <span class="slider-sw"></span>
      </label>
      <span style="font-size:12px">Vision Mode <span style="color:var(--dim);font-size:11px">(auto-green)</span></span>
    </div>
    <div class="meter-wrap">
      <div>
        <div style="font-size:10px;color:var(--dim);margin-bottom:4px;text-align:center">FILL</div>
        <div class="meter-bar-outer" id="meterBarOuter">
          <div class="meter-bar-green-zone" id="meterGreenZone"></div>
          <div class="meter-bar-fill" id="meterBarFill"></div>
        </div>
      </div>
      <div class="meter-info">
        <div>Fill: <b id="mFill">—</b></div>
        <div>Speed: <b id="mVel">—</b></div>
        <div>Detected: <b id="mDetected">—</b></div>
        <div>Green: <b id="mGreen">—</b></div>
        <div style="margin-top:8px;font-size:11px;color:var(--dim)">Latency comp:</div>
        <div><b id="mLatency">8 ms</b></div>
      </div>
    </div>
    <div style="border-top:1px solid var(--border);margin:10px 0"></div>
    <div class="field">
      <label>Latency compensation (ms)</label>
      <input type="number" id="f-latency" value="8" min="0" max="50" step="0.5">
    </div>
    <button class="save-btn" id="calibrateBtn" style="background:#7b2ff7;margin-top:4px">Run Calibration</button>
    <div class="save-msg" id="calibMsg"></div>
  </div>

  <!-- Settings -->
  <div class="panel">
    <h2>Settings</h2>
    <form id="settingsForm">
      <div class="field">
        <label>Active Profile</label>
        <select id="f-profile" name="active_profile">
          <option value="default">default</option>
          <option value="quick">quick</option>
          <option value="slow">slow</option>
          <option value="midrange">midrange</option>
        </select>
      </div>
      <div class="row2">
        <div class="field">
          <label>Animation ms</label>
          <input type="number" id="f-anim" name="animation_ms" step="10" min="100" max="2000">
        </div>
        <div class="field">
          <label>Aim Percentile</label>
          <input type="number" id="f-aim" name="aim_percentile" step="0.05" min="0" max="1">
        </div>
      </div>
      <div class="row2">
        <div class="field">
          <label>Green Start %</label>
          <input type="number" id="f-gs" name="green_start_pct" step="0.01" min="0" max="1">
        </div>
        <div class="field">
          <label>Green End %</label>
          <input type="number" id="f-ge" name="green_end_pct" step="0.01" min="0" max="1">
        </div>
      </div>
      <div style="border-top:1px solid var(--border);margin:10px 0"></div>
      <div class="row2">
        <div class="field">
          <label>HBR Sigma ms</label>
          <input type="number" id="f-sigma" name="hbr_sigma_ms" step="0.5" min="0.5">
        </div>
        <div class="field">
          <label>HBR Tau ms</label>
          <input type="number" id="f-tau" name="hbr_tau_ms" step="0.5" min="0">
        </div>
      </div>
      <div class="row2">
        <div class="field">
          <label>Hold Base ms</label>
          <input type="number" id="f-hold" name="hold_base_ms" step="1" min="10">
        </div>
        <div class="field">
          <label>Ramp Steps</label>
          <input type="number" id="f-ramp" name="ramp_steps" step="1" min="2" max="20">
        </div>
      </div>
      <div class="row2">
        <div class="field">
          <label>Ramp Exponent</label>
          <input type="number" id="f-rampexp" name="ramp_exponent" step="0.1" min="1">
        </div>
        <div class="field">
          <label>Stick Noise σ</label>
          <input type="number" id="f-noise" name="stick_noise_sigma" step="0.001" min="0">
        </div>
      </div>
      <button type="submit" class="save-btn">Save / Apply Changes</button>
      <div class="save-msg" id="saveMsg"></div>
    </form>
  </div>

  <!-- Event log -->
  <div class="panel span2">
    <h2>Shot Event Log</h2>
    <div class="log" id="eventLog"></div>
  </div>
</div>

<script>
const B = {
  DPAD_UP:0x0001,DPAD_DOWN:0x0002,DPAD_LEFT:0x0004,DPAD_RIGHT:0x0008,
  START:0x0010,BACK:0x0020,LS:0x0040,RS:0x0080,
  LB:0x0100,RB:0x0200,A:0x1000,B:0x2000,X:0x4000,Y:0x8000
};
const BTN_MAP = {
  'btn-a':B.A,'btn-b':B.B,'btn-x':B.X,'btn-y':B.Y,
  'btn-lb':B.LB,'btn-rb':B.RB,'btn-start':B.START,'btn-back':B.BACK,
  'btn-ls':B.LS,'btn-rs':B.RS,
  'btn-dup':B.DPAD_UP,'btn-ddown':B.DPAD_DOWN,
  'btn-dleft':B.DPAD_LEFT,'btn-dright':B.DPAD_RIGHT
};
const ACTIVE_COLORS = {
  'btn-a':'#2ecc71','btn-b':'#e74c3c','btn-x':'#4a9eff','btn-y':'#f0c040',
  'btn-lb':'#9b59b6','btn-rb':'#9b59b6'
};

const $ = id => document.getElementById(id);
const wsDot    = $('wsDot');
const wsLabel  = $('wsLabel');
const shotLbl  = $('shot-label');
const eventLog = $('eventLog');
const saveMsg  = $('saveMsg');
let shotFlash  = null;

// ── Meter display ──────────────────────────────────────────────────────────
function updateMeter(s) {
  const m = s.meter || {};
  const fill = m.fill_pct != null ? m.fill_pct : null;
  const vel  = m.velocity_pct_per_ms != null ? m.velocity_pct_per_ms : null;
  const det  = m.fill_detected;
  const grn  = m.green_detected;

  $('mFill').textContent      = fill != null ? (fill*100).toFixed(1)+'%' : '—';
  $('mVel').textContent       = vel  != null ? (vel*1000).toFixed(2)+'%/s' : '—';
  $('mDetected').textContent  = det  != null ? (det ? 'YES' : 'no') : '—';
  $('mGreen').textContent     = grn  != null ? (grn ? '✓ GREEN' : 'no') : '—';
  $('mGreen').style.color     = grn  ? 'var(--accent)' : 'var(--dim)';
  $('mLatency').textContent   = (m.latency_ms != null ? m.latency_ms : 8) + ' ms';

  const bar = $('meterBarFill');
  if (fill != null) {
    bar.style.height = (fill * 100).toFixed(1) + '%';
    bar.style.background = grn ? 'var(--accent)' : (det ? '#4a9eff' : 'var(--dim)');
  }

  // Vision mode badge
  const vm = s.vision_mode;
  $('visionBadge').style.display = vm ? 'inline-block' : 'none';

  // Update green zone overlay on meter bar
  const cfg = s.meter_cfg || {};
  const gs = cfg.green_start_pct != null ? cfg.green_start_pct : 0.55;
  const ge = cfg.green_end_pct   != null ? cfg.green_end_pct   : 0.65;
  const barH = 180;
  const zoneTop  = (1 - ge) * barH;
  const zoneH    = (ge - gs) * barH;
  const gz = $('meterGreenZone');
  gz.style.top    = zoneTop.toFixed(1) + 'px';
  gz.style.height = zoneH.toFixed(1) + 'px';
}

// ── Controller state ───────────────────────────────────────────────────────
function applyState(s) {
  const btns = s.buttons || 0;
  for (const [id, mask] of Object.entries(BTN_MAP)) {
    const el = $(id);
    if (!el) continue;
    el.setAttribute('fill', (btns & mask) ? (ACTIVE_COLORS[id] || '#00ff99') : '#252540');
  }
  const xRing = $('x-ring');
  if (xRing) xRing.setAttribute('opacity', (btns & B.X) ? '1' : '.35');

  $('lt-fill').setAttribute('width', Math.round(s.lt * 106));
  $('rt-fill').setAttribute('width', Math.round(s.rt * 106));
  moveStick('ls-dot', 138, 188, s.lx, s.ly);
  moveStick('rs-dot', 292, 220, s.rx, s.ry);

  if (s.shot_active && !shotFlash) {
    shotLbl.setAttribute('opacity','1');
    shotLbl.textContent = s.vision_mode ? '● VISION ARMED' : '● ARMED';
  } else if (!s.shot_active && !shotFlash) {
    shotLbl.setAttribute('opacity','0');
  }
  if (s.event) addEvent(s.event);

  updateMeter(s);

  // Sync toggle
  const tog = $('visionToggle');
  if (tog && tog.dataset.userTouched !== '1') {
    tog.checked = !!s.vision_mode;
  }
  const bl = $('backendLabel');
  if (bl && s.vision_backend) bl.textContent = '[' + s.vision_backend + ']';
}

function moveStick(id, cx, cy, nx, ny) {
  const el = $(id); if (!el) return;
  const travel = 20;
  el.setAttribute('cx', (cx + nx * travel).toFixed(1));
  el.setAttribute('cy', (cy - ny * travel).toFixed(1));
}

// ── Event log ──────────────────────────────────────────────────────────────
const events = [];
function addEvent(label) {
  const ts = new Date().toLocaleTimeString('en-US',{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
  events.unshift({label,ts});
  if (events.length > 40) events.length = 40;
  renderLog();
  clearTimeout(shotFlash);
  shotLbl.setAttribute('opacity','1');
  shotLbl.textContent = label;
  shotFlash = setTimeout(() => { shotLbl.setAttribute('opacity','0'); shotFlash = null; }, 700);
}

function renderLog() {
  eventLog.innerHTML = events.map(e => {
    const cls = e.label.includes('GREEN') ? 'ev-green'
               : e.label.includes('VISION') ? 'ev-vision'
               : e.label.includes('ARMED') ? 'ev-armed' : 'ev-dim';
    return `<div class="ev ${cls}">${e.ts}&nbsp;&nbsp;${e.label}</div>`;
  }).join('');
}

// ── Vision mode toggle ─────────────────────────────────────────────────────
$('visionToggle').addEventListener('change', async function() {
  this.dataset.userTouched = '1';
  const on = this.checked;
  try {
    await fetch('/api/settings', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({vision_mode: on})
    });
    addEvent(on ? 'VISION MODE ON' : 'VISION MODE OFF');
  } catch(e) { console.warn('vision toggle error', e); }
  setTimeout(() => { this.dataset.userTouched = '0'; }, 3000);
});

// ── Latency save ───────────────────────────────────────────────────────────
$('calibrateBtn').addEventListener('click', async () => {
  const lat = parseFloat($('f-latency').value) || 8;
  try {
    await fetch('/api/settings', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({vision_latency_ms: lat})
    });
    const r = await fetch('/api/calibrate', {method:'POST'});
    const d = await r.json();
    $('calibMsg').textContent = d.status || 'started';
    setTimeout(() => { $('calibMsg').textContent = ''; }, 4000);
  } catch(e) { $('calibMsg').textContent = String(e); }
});

// ── Settings form ──────────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    const d = await r.json();
    const fields = {
      'f-profile':'active_profile','f-anim':'animation_ms','f-aim':'aim_percentile',
      'f-gs':'green_start_pct','f-ge':'green_end_pct',
      'f-sigma':'hbr_sigma_ms','f-tau':'hbr_tau_ms',
      'f-hold':'hold_base_ms','f-ramp':'ramp_steps',
      'f-rampexp':'ramp_exponent','f-noise':'stick_noise_sigma'
    };
    for (const [fid, key] of Object.entries(fields)) {
      const el = $(fid);
      if (el && d[key] !== undefined) el.value = d[key];
    }
    if (d.vision_mode !== undefined) {
      $('visionToggle').checked = !!d.vision_mode;
    }
    if (d.vision_latency_ms !== undefined) $('f-latency').value = d.vision_latency_ms;
  } catch(e) { console.warn('loadConfig failed', e); }
}

$('settingsForm').addEventListener('submit', async e => {
  e.preventDefault();
  const body = {};
  const fields = {
    'f-profile':'active_profile','f-anim':'animation_ms','f-aim':'aim_percentile',
    'f-gs':'green_start_pct','f-ge':'green_end_pct',
    'f-sigma':'hbr_sigma_ms','f-tau':'hbr_tau_ms',
    'f-hold':'hold_base_ms','f-ramp':'ramp_steps',
    'f-rampexp':'ramp_exponent','f-noise':'stick_noise_sigma'
  };
  const numFields = new Set([
    'animation_ms','aim_percentile','green_start_pct','green_end_pct',
    'hbr_sigma_ms','hbr_tau_ms','hold_base_ms','ramp_steps',
    'ramp_exponent','stick_noise_sigma'
  ]);
  for (const [fid, key] of Object.entries(fields)) {
    const el = $(fid);
    if (!el || el.value === '') continue;
    body[key] = numFields.has(key) ? Number(el.value) : el.value;
  }
  try {
    const r = await fetch('/api/settings', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'error');
    showMsg('✓ Applied', false);
    addEvent('CONFIG SAVED');
  } catch(err) { showMsg('✗ '+err.message, true); }
});

function showMsg(txt, isErr) {
  saveMsg.textContent = txt;
  saveMsg.className = 'save-msg' + (isErr ? ' err' : '');
  setTimeout(() => { saveMsg.textContent = ''; saveMsg.className = 'save-msg'; }, 3000);
}

// ── WebSocket ──────────────────────────────────────────────────────────────
let ws = null;
let reconnectTimer = null;
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws/state`);
  ws.onopen = () => { wsDot.classList.add('on'); wsLabel.textContent = 'Connected'; clearTimeout(reconnectTimer); };
  ws.onmessage = ev => { try { applyState(JSON.parse(ev.data)); } catch(e) {} };
  ws.onclose = ws.onerror = () => {
    wsDot.classList.remove('on'); wsLabel.textContent = 'Reconnecting…';
    reconnectTimer = setTimeout(connect, 2000);
  };
}
loadConfig();
connect();
</script>
</body>
</html>"""
