"""
Web dashboard server.

Routes:
  GET /         — live HTML dashboard (auto-refreshes every 500 ms)
  GET /state    — raw JSON state snapshot
  GET /frame    — latest annotated detection frame (JPEG, ~10 FPS in browser)
  POST /config  — apply config patch (JSON body)
  POST /toggle  — flip auto-green ON/OFF  (body: {"enabled": true|false})
  GET /profiles — all built-in profiles as JSON
"""
from __future__ import annotations

import threading
from typing import Any, Optional

_state:    dict[str, Any] = {}
_lock      = threading.Lock()
_detector: Optional[Any]  = None   # set by start_web_server
_engine:   Optional[Any]  = None   # set by start_web_server


def push_state(state: dict[str, Any]) -> None:
    """Called from poll thread — non-blocking cache update only."""
    with _lock:
        _state.clear()
        _state.update(state)


_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>NBA 2K26 Shot Suite</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#07070f;color:#ccd;font-family:'Segoe UI',monospace;padding:20px;min-height:100vh}
h1{font-size:1.15rem;color:#00ff88;margin-bottom:14px;letter-spacing:.06em}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin-bottom:14px}
.card{background:#0f0f22;border:1px solid #222244;border-radius:8px;padding:12px}
.card h2{font-size:.7rem;color:#5566aa;text-transform:uppercase;letter-spacing:.12em;margin-bottom:8px}
.row{display:flex;justify-content:space-between;padding:3px 0;font-size:.82rem;border-bottom:1px solid #131326}
.row:last-child{border:none}
.val{font-weight:bold}
.green{color:#00ff88}.warn{color:#ffaa00}.red{color:#ff5555}.dim{color:#334}
.bar-bg{background:#0a0a1a;height:10px;border-radius:3px;overflow:hidden;margin-top:5px;position:relative}
.bar-fill{height:100%;background:#2255cc;transition:width .1s;border-radius:3px}
.bar-gw{position:absolute;top:0;height:100%;background:#00ff55;opacity:.55;border-radius:2px}
.bar-aim{position:absolute;top:-2px;width:2px;height:calc(100%+4px);background:#00ff88}
.event-banner{font-size:1rem;color:#00ff88;font-weight:bold;text-align:center;
              padding:7px;background:#0a1a0f;border:1px solid #00ff4422;
              border-radius:6px;margin-bottom:12px;min-height:34px;letter-spacing:.06em}
.toggle-badge{display:inline-block;padding:3px 10px;border-radius:12px;font-size:.75rem;font-weight:bold;margin-left:8px}
.toggle-on{background:#003322;color:#00ff88;border:1px solid #00ff8844}
.toggle-off{background:#1a1a1a;color:#556;border:1px solid #333}
.toggle-btn{display:block;width:100%;padding:14px;border-radius:10px;font-size:1.1rem;font-weight:bold;
            letter-spacing:.08em;cursor:pointer;border:2px solid;margin-bottom:14px;transition:all .15s}
.toggle-btn-on{background:#003322;color:#00ff88;border-color:#00ff88;box-shadow:0 0 18px #00ff8833}
.toggle-btn-off{background:#12121f;color:#445;border-color:#333}
.toggle-btn:active{transform:scale(.97)}
.frame-card{background:#0f0f22;border:1px solid #222244;border-radius:8px;padding:12px}
.frame-card h2{font-size:.7rem;color:#5566aa;text-transform:uppercase;letter-spacing:.12em;margin-bottom:8px}
#liveFrame{width:100%;border-radius:4px;display:block;max-height:320px;object-fit:contain;background:#000}
#ts{font-size:.65rem;color:#334;margin-top:10px;text-align:right}
</style>
</head>
<body>
<h1>⚡ NBA 2K26 Shot Suite
  <span id="toggleBadge" class="toggle-badge toggle-off">AUTO OFF</span>
</h1>
<div id="eventBanner" class="event-banner"></div>

<button id="toggleBtn" class="toggle-btn toggle-btn-off" onclick="doToggle()">
  ○ AUTO-GREEN: OFF — click to enable
</button>

<div class="grid" id="grid"></div>

<div class="frame-card">
  <h2>Live Detection View — what the system sees</h2>
  <img id="liveFrame" src="/frame" alt="No frame — mss/opencv required" onerror="this.alt='Detection unavailable (mss/opencv not installed)'"/>
</div>

<div id="ts"></div>

<script>
const pct  = v => ((v||0)*100).toFixed(1)+'%';
const fmt  = v => (v===undefined||v===null)?'—':v;
const clamp = (v,lo,hi)=>Math.max(lo,Math.min(hi,v));

let _toggling = false;

async function doToggle(){
  if(_toggling) return;
  _toggling = true;
  const btn = document.getElementById('toggleBtn');
  const isOn = btn.classList.contains('toggle-btn-on');
  try{
    const r = await fetch('/toggle', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({enabled: !isOn})
    });
    if(!r.ok) console.error('toggle failed', await r.text());
  } catch(e){ console.error(e); }
  _toggling = false;
  await refreshState();
}

function _applyToggleBtn(autoOn){
  const btn = document.getElementById('toggleBtn');
  const badge = document.getElementById('toggleBadge');
  if(autoOn){
    btn.textContent = '⚡ AUTO-GREEN: ON — click to disable';
    btn.className = 'toggle-btn toggle-btn-on';
    badge.textContent='AUTO ON'; badge.className='toggle-badge toggle-on';
  } else {
    btn.textContent = '○ AUTO-GREEN: OFF — click to enable';
    btn.className = 'toggle-btn toggle-btn-off';
    badge.textContent='AUTO OFF'; badge.className='toggle-badge toggle-off';
  }
}

async function refreshState(){
  try{
    const s = await (await fetch('/state')).json();

    _applyToggleBtn(s.auto_mode);

    // Event banner
    const banner = document.getElementById('eventBanner');
    if(s.event) banner.textContent = s.event;
    else if(s.shot_active) banner.textContent = '⚡ SHOT ARMED';
    else if(s.outcome_detected) banner.textContent = '✓ EXCELLENT';
    else banner.textContent = s.auto_mode ? '● Auto mode active — waiting for shot…' : '○ Auto-green OFF — click the button above to enable';

    // Meter bar values
    const fill  = clamp(s.fill_pct||0,0,1);
    const gw    = clamp(s.green_window_pct||0,0,1);
    const aim   = clamp(s.learner_mu||0.5,0,1);
    const gwHalf= 0.05;

    document.getElementById('grid').innerHTML = `
      <div class="card">
        <h2>Controller</h2>
        <div class="row"><span>Physical pad</span>
          <span class="val ${s.connected?'green':'red'}">${s.connected?'● Connected':'○ Disconnected'}</span></div>
        <div class="row"><span>Virtual pad</span>
          <span class="val ${s.vpad_available?'green':'warn'}">${s.vpad_available?'Ready':'Not installed'}</span></div>
        <div class="row"><span>Profile</span><span class="val">${fmt(s.current_profile)}</span></div>
        <div class="row"><span>LT / RT</span><span class="val">${pct(s.lt)} / ${pct(s.rt)}</span></div>
      </div>

      <div class="card">
        <h2>Shot Meter Detection</h2>
        <div class="row"><span>Meter found</span>
          <span class="val ${s.meter_found?'green':'dim'}">${s.meter_found?'● YES':'○ no'}</span></div>
        <div class="row"><span>Green window</span>
          <span class="val ${s.green_window_visible?'green':'dim'}">${s.green_window_visible?'● VISIBLE':'○ —'}</span></div>
        <div class="row"><span>Fill position</span><span class="val">${pct(s.fill_pct)}</span></div>
        <div class="row"><span>Green win at</span><span class="val green">${pct(s.green_window_pct)}</span></div>
        <div class="row"><span>Confidence</span><span class="val">${(s.cv_confidence||0).toFixed(2)}</span></div>
        <div class="row"><span>Result text</span>
          <span class="val ${s.outcome_detected?'green':'dim'}">${s.outcome_detected?'✓ DETECTED':'—'}</span></div>
        <div class="bar-bg" style="margin-top:8px">
          <div class="bar-fill" style="width:${pct(fill)}"></div>
          <div class="bar-gw"  style="left:${pct(Math.max(0,gw-gwHalf))};width:${pct(gwHalf*2)}"></div>
          <div class="bar-aim" style="left:${pct(aim)}"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:.65rem;color:#446;margin-top:3px">
          <span>0%</span><span style="color:#00ff88">▲ aim ${pct(aim)}</span><span>100%</span>
        </div>
      </div>

      <div class="card">
        <h2>Adaptive Learner</h2>
        <div class="row"><span>Shots logged</span><span class="val">${fmt(s.learner_shots)}</span></div>
        <div class="row"><span>Green rate</span>
          <span class="val ${(s.learner_green_pct||0)>0.6?'green':(s.learner_green_pct||0)>0.3?'warn':'red'}">
            ${pct(s.learner_green_pct)}</span></div>
        <div class="row"><span>Target μ (aim)</span><span class="val green">${pct(s.learner_mu)}</span></div>
        <div class="row"><span>Uncertainty σ</span><span class="val">${(s.learner_sigma||0.08).toFixed(3)}</span></div>
        <div class="row" style="margin-top:4px;font-size:.7rem;color:#556">
          <span colspan="2">σ shrinks as learner converges. μ drifts toward green window.</span>
        </div>
      </div>
    `;
    document.getElementById('ts').textContent = 'Updated: '+new Date().toLocaleTimeString();
  } catch(e){
    document.getElementById('eventBanner').textContent = 'Dashboard not responding…';
  }
}

// Live detection frame — separate refresh cycle (10 fps)
function refreshFrame(){
  const img = document.getElementById('liveFrame');
  const next = new Image();
  next.onload = ()=>{ img.src = next.src; };
  next.src = '/frame?t='+Date.now();
}

refreshState();
setInterval(refreshState, 500);
setInterval(refreshFrame, 100);
</script>
</body>
</html>"""


def start_web_server(
    config_mgr:   Any,
    suite:        Any,
    host:         str  = "127.0.0.1",
    port:         int  = 8420,
    open_browser: bool = True,
    detector:     Optional[Any] = None,
    engine:       Optional[Any] = None,
) -> None:
    global _detector, _engine
    _detector = detector
    _engine   = engine

    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse, HTMLResponse, Response
        import uvicorn

        app = FastAPI(title="NBA 2K26 Shot Suite")

        @app.get("/", response_class=HTMLResponse)
        async def root() -> HTMLResponse:
            return HTMLResponse(_DASHBOARD_HTML)

        @app.get("/state")
        async def get_state() -> JSONResponse:
            with _lock:
                return JSONResponse(dict(_state))

        @app.get("/frame")
        async def get_frame() -> Response:
            """Return the latest annotated detection frame as a JPEG."""
            if _detector is None:
                return Response(b"", media_type="image/jpeg")
            frame = _detector.get_annotated_frame()
            if frame is None:
                return Response(b"", media_type="image/jpeg")
            try:
                import cv2
                ok, buf = cv2.imencode(".jpg", frame,
                                       [cv2.IMWRITE_JPEG_QUALITY, 75])
                if ok:
                    return Response(buf.tobytes(), media_type="image/jpeg")
            except Exception:
                pass
            return Response(b"", media_type="image/jpeg")

        @app.post("/config")
        async def post_config(request: Request) -> JSONResponse:
            try:
                data = await request.json()
                config_mgr.apply_dict(data)
                return JSONResponse({"ok": True})
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        @app.post("/toggle")
        async def post_toggle(request: Request) -> JSONResponse:
            if _engine is None:
                return JSONResponse({"ok": False, "error": "engine not available"}, status_code=503)
            try:
                data = await request.json()
                enabled = bool(data.get("enabled", False))
                _engine.set_auto_mode(enabled)
                return JSONResponse({"ok": True, "auto_mode": enabled})
            except Exception as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

        @app.get("/profiles")
        async def get_profiles() -> JSONResponse:
            from .shot_timer import PROFILES
            return JSONResponse({k: {
                "animation_ms":    v.animation_ms,
                "green_start_pct": v.green_start_pct,
                "green_end_pct":   v.green_end_pct,
                "aim_percentile":  v.aim_percentile,
                "release_ms":      v.release_ms,
                "green_window_ms": v.green_window_ms,
            } for k, v in PROFILES.items()})

        t = threading.Thread(
            target=uvicorn.run,
            kwargs={"app": app, "host": host, "port": port, "log_level": "error"},
            daemon=True,
        )
        t.start()

        import time
        time.sleep(0.4)
        url = f"http://{host}:{port}"
        print(f"[WebServer] Dashboard: {url}")

        if open_browser:
            import webbrowser
            webbrowser.open(url)

    except ImportError:
        print("[WebServer] FastAPI/uvicorn not installed — dashboard disabled. "
              "Run: pip install fastapi 'uvicorn[standard]'")
