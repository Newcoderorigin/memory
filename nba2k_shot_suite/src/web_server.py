"""
Minimal web dashboard server.

Exposes:
  GET /state   — current suite state snapshot (JSON)
  POST /config — apply config patch (JSON body)

State is pushed from the poll thread via push_state(); the endpoint just
reads the latest cached snapshot — no blocking calls on the poll thread.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

_state: dict[str, Any] = {}
_lock = threading.Lock()


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
) -> None:
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse
        import uvicorn

        app = FastAPI(title="NBA 2K26 Shot Suite")

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
        # Give uvicorn a moment to bind before returning to caller
        import time
        time.sleep(0.35)
        print(f"[WebServer] Dashboard: http://{host}:{port}/state")

    except ImportError:
        print("[WebServer] FastAPI/uvicorn not installed — dashboard disabled. "
              "Run: pip install fastapi 'uvicorn[standard]'")
