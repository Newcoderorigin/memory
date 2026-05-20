"""
Web dashboard server.

Routes:
  GET /        — live HTML dashboard (auto-refreshes every 500 ms)
  GET /state   — raw JSON state snapshot
  POST /config — apply config patch (JSON body)
  GET /profiles — all built-in profiles
"""
from __future__ import annotations

import threading
from typing import Any

_state: dict[str, Any] = {}
_lock = threading.Lock()

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>NBA 2K26 Shot Suite</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0a0a14;color:#ccd;font-family:'Segoe UI',monospace;padding:24px}
  h1{font-size:1.2rem;color:#00ff88;margin-bottom:16px;letter-spacing:.05em}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}
  .card{background:#111128;border:1px solid #2a2a4a;border-radius:8px;padding:14px}
  .card h2{font-size:.75rem;color:#6677aa;text-transform:uppercase;letter-spacing:.1em;margin-bottom:10px}
  .row{display:flex;justify-content:space-between;padding:3px 0;font-size:.85rem;border-bottom:1px solid #1a1a2e}
  .row:last-child{border:none}
  .val{color:#00ff88;font-weight:bold}
  .val.warn{color:#ffaa00}
  .val.dim{color:#445}
  .val.red{color:#ff4444}
  .bar-bg{background:#151525;height:12px;border-radius:4px;overflow:hidden;margin-top:6px}
  .bar-fill{height:100%;background:#3377ff;transition:width .12s}
  .bar-green{background:#00ff55;position:absolute;height:100%;opacity:.6}
  .bar-wrap{position:relative}
  .event{font-size:1.1rem;color:#00ff88;text-align:center;padding:8px;min-height:36px;font-weight:bold;letter-spacing:.05em}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
  .dot.on{background:#00ff88}
  .dot.off{background:#ff4444}
  #ts{font-size:.7rem;color:#334;margin-top:16px;text-align:right}
</style>
</head>
<body>
<h1>⚡ NBA 2K26 Shot Suite — Live Dashboard</h1>
<div id="event-banner" class="event"></div>
<div class="grid" id="grid"></div>
<div id="ts"></div>
<script>
const fmt = v => v === undefined || v === null ? '—' : v;
const pct = v => (v*100).toFixed(1)+'%';
const ms  = v => v.toFixed(1)+' ms';

async function refresh(){
  try{
    const r = await fetch('/state');
    const s = await r.json();

    const banner = document.getElementById('event-banner');
    if(s.event) banner.textContent = s.event;
    else if(s.shot_active) banner.textContent = 'SHOT ARMED ⚡';
    else banner.textContent = '';

    const connected = s.connected;
    const vpad = s.vpad_available;

    document.getElementById('grid').innerHTML = `
      <div class="card">
        <h2>Controller</h2>
        <div class="row"><span>Physical</span>
          <span class="val ${connected?'':'red'}">
            <span class="dot ${connected?'on':'off'}"></span>${connected?'Connected':'Disconnected'}
          </span></div>
        <div class="row"><span>Virtual Pad</span>
          <span class="val ${vpad?'':'warn'}">${vpad?'Ready':'Not installed'}</span></div>
        <div class="row"><span>Profile</span><span class="val">${fmt(s.current_profile)}</span></div>
        <div class="row"><span>Shot active</span><span class="val ${s.shot_active?'warn':'dim'}">${s.shot_active?'YES':'no'}</span></div>
        <div class="row"><span>LT / RT</span><span class="val">${pct(s.lt||0)} / ${pct(s.rt||0)}</span></div>
      </div>

      <div class="card">
        <h2>Vision — Shot Meter</h2>
        <div class="row"><span>Meter found</span>
          <span class="val ${s.meter_found?'':'dim'}">${s.meter_found?'YES':'no'}</span></div>
        <div class="row"><span>Green window</span>
          <span class="val ${s.green_window_visible?'':'dim'}">${s.green_window_visible?'VISIBLE':'—'}</span></div>
        <div class="row"><span>Fill</span><span class="val">${pct(s.fill_pct||0)}</span></div>
        <div class="row"><span>Green at</span><span class="val">${pct(s.green_window_pct||0)}</span></div>
        <div class="row"><span>Confidence</span><span class="val">${(s.cv_confidence||0).toFixed(2)}</span></div>
        <div class="row"><span>Outcome text</span>
          <span class="val ${s.outcome_detected?'':'dim'}">${s.outcome_detected?'DETECTED':'—'}</span></div>
        <div class="bar-bg bar-wrap">
          <div class="bar-fill" style="width:${pct(s.fill_pct||0)}"></div>
        </div>
      </div>

      <div class="card">
        <h2>Adaptive Learner</h2>
        <div class="row"><span>Shots taken</span><span class="val">${fmt(s.learner_shots)}</span></div>
        <div class="row"><span>Green rate</span><span class="val">${pct(s.learner_green_pct||0)}</span></div>
        <div class="row"><span>Target μ</span><span class="val">${pct(s.learner_mu||0.5)}</span></div>
        <div class="row"><span>Uncertainty σ</span><span class="val">${(s.learner_sigma||0.08).toFixed(3)}</span></div>
      </div>
    `;

    document.getElementById('ts').textContent =
      'Last update: ' + new Date().toLocaleTimeString();
  } catch(e){
    document.getElementById('event-banner').textContent = 'Server not responding…';
  }
}
refresh();
setInterval(refresh, 500);
</script>
</body>
</html>"""


def push_state(state: dict[str, Any]) -> None:
    """Called from poll thread — non-blocking cache update only."""
    with _lock:
        _state.clear()
        _state.update(state)


def start_web_server(
    config_mgr: Any,
    suite: Any,
    host: str = "127.0.0.1",
    port: int = 8420,
    open_browser: bool = True,
) -> None:
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse, HTMLResponse
        import uvicorn

        app = FastAPI(title="NBA 2K26 Shot Suite")

        @app.get("/", response_class=HTMLResponse)
        async def root() -> HTMLResponse:
            return HTMLResponse(_DASHBOARD_HTML)

        @app.get("/state")
        async def get_state() -> JSONResponse:
            with _lock:
                return JSONResponse(dict(_state))

        @app.post("/config")
        async def post_config(request: Request) -> JSONResponse:
            try:
                data = await request.json()
                config_mgr.apply_dict(data)
                return JSONResponse({"ok": True})
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
