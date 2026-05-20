# src/web_server.py
"""
FastAPI web server for live dashboard and WebSocket state push.
Serves HTML overlay + receives live config updates.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

_app = FastAPI()
_websocket_queue: asyncio.Queue = asyncio.Queue()
_active_connections: list[WebSocket] = []


@_app.get("/")
async def get_dashboard() -> HTMLResponse:
    """Serve live dashboard HTML."""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>2K26 Shot Suite Dashboard</title>
        <style>
            body { font-family: 'Segoe UI', sans-serif; margin: 0; background: #0a0e27; color: #e0e0ff; }
            .container { max-width: 1200px; margin: 0 auto; padding: 20px; }
            .stat-box { background: #1a1f3a; border: 1px solid #3a3a5c; border-radius: 8px; 
                        padding: 20px; margin: 10px 0; }
            .stat-label { font-size: 12px; color: #8080aa; text-transform: uppercase; }
            .stat-value { font-size: 28px; font-weight: bold; color: #00ff99; }
            .control { margin: 10px 0; }
            input, button { padding: 10px; background: #1a1f3a; border: 1px solid #3a3a5c; 
                            color: #e0e0ff; border-radius: 4px; }
            button { cursor: pointer; background: #004400; }
            button:hover { background: #006600; }
            .toggle { display: inline-flex; align-items: center; gap: 10px; }
            #chart { height: 300px; margin: 20px 0; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🎯 NBA 2K26 Shot Suite</h1>
            
            <div class="stat-box">
                <div class="stat-label">Green Rate</div>
                <div class="stat-value" id="greenRate">0%</div>
            </div>
            
            <div class="stat-box">
                <div class="stat-label">Timing Offset (Learned)</div>
                <div class="stat-value" id="offsetMs">+0.0 ms</div>
                <div class="stat-label">Confidence: <span id="offsetSigma">8.0 ms</span></div>
            </div>
            
            <div class="stat-box">
                <div class="stat-label">Auto-Shoot Mode</div>
                <div class="toggle">
                    <input type="checkbox" id="autoShoot" onchange="toggleAutoShoot()">
                    <span id="autoShootLabel">OFF</span>
                </div>
            </div>
            
            <div class="stat-box">
                <div class="stat-label">Learning Enabled</div>
                <div class="toggle">
                    <input type="checkbox" id="learning" checked onchange="toggleLearning()">
                    <span id="learningLabel">ON</span>
                </div>
            </div>
            
            <div class="stat-box">
                <div class="stat-label">Last 10 Shots (ms error)</div>
                <canvas id="chart"></canvas>
            </div>
        </div>
        
        <script>
            const ws = new WebSocket('ws://127.0.0.1:8420/ws');
            const shots = [];
            
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                
                document.getElementById('greenRate').innerText = 
                    (data.green_rate * 100).toFixed(1) + '%';
                document.getElementById('offsetMs').innerText = 
                    (data.offset_ms > 0 ? '+' : '') + data.offset_ms.toFixed(1) + ' ms';
                document.getElementById('offsetSigma').innerText = 
                    data.offset_sigma.toFixed(1) + ' ms';
                
                if (data.last_error_ms !== undefined) {
                    shots.push(data.last_error_ms);
                    if (shots.length > 10) shots.shift();
                    updateChart();
                }
            };
            
            function toggleAutoShoot() {
                const checked = document.getElementById('autoShoot').checked;
                document.getElementById('autoShootLabel').innerText = checked ? 'ON' : 'OFF';
                ws.send(JSON.stringify({ action: 'set_auto_shoot', value: checked }));
            }
            
            function toggleLearning() {
                const checked = document.getElementById('learning').checked;
                document.getElementById('learningLabel').innerText = checked ? 'ON' : 'OFF';
                ws.send(JSON.stringify({ action: 'set_learning', value: checked }));
            }
            
            function updateChart() {
                // Simple ASCII chart (replace with Chart.js for production)
                const canvas = document.getElementById('chart');
                if (canvas.getContext) {
                    const ctx = canvas.getContext('2d');
                    ctx.fillStyle = '#1a1f3a';
                    ctx.fillRect(0, 0, canvas.width, canvas.height);
                    
                    ctx.strokeStyle = '#00ff99';
                    ctx.beginPath();
                    shots.forEach((val, i) => {
                        const x = (i / 10) * canvas.width;
                        const y = 150 - (val / 40) * 150;  // scale: ±40ms → full height
                        if (i === 0) ctx.moveTo(x, y);
                        else ctx.lineTo(x, y);
                    });
                    ctx.stroke();
                }
            }
        </script>
    </body>
    </html>
    """)


@_app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint for live state push."""
    await websocket.accept()
    _active_connections.append(websocket)
    try:
        while True:
            data = await websocket.receive_json()
            # Handle client commands (auto_shoot toggle, learning enable, etc.)
            # Forward to main suite
            pass
    except WebSocketDisconnect:
        _active_connections.remove(websocket)


async def push_state_to_clients(state: dict[str, Any]) -> None:
    """Push state to all connected WebSocket clients."""
    if not _active_connections:
        return
    
    dead = []
    for conn in _active_connections:
        try:
            await conn.send_json(state)
        except Exception:
            dead.append(conn)
    
    for conn in dead:
        _active_connections.remove(conn)


def push_state(state: dict[str, Any]) -> None:
    """Sync wrapper — push to event loop safely."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(push_state_to_clients(state), loop)
    except Exception:
        pass  # WebSocket not available


_server_thread: Optional[threading.Thread] = None
_loop: Optional[asyncio.AbstractEventLoop] = None


def start_web_server(
    config_mgr: Any,
    suite: Any,
    host: str = "127.0.0.1",
    port: int = 8420,
) -> None:
    """Start FastAPI server in background thread."""
    global _server_thread, _loop
    
    def run_server():
        global _loop
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
        
        config = uvicorn.Config(
            _app,
            host=host,
            port=port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        _loop.run_until_complete(server.serve())
    
    _server_thread = threading.Thread(target=run_server, daemon=True)
    _server_thread.start()
    
    import time
    time.sleep(0.5)  # Give server time to bind
    print(f"[WebServer] Running at http://{host}:{port}")