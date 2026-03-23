#!/usr/bin/env python3
"""
Soundmeter Web Server — FastAPI + WebSocket + SQLite
Usage:
    python server.py --port COM3              # Windows + real device
    python server.py --port /dev/ttyUSB0      # Linux + real device
    python server.py --demo                   # demo mode (no device)
Then open http://localhost:8000
"""
import argparse
import asyncio
import json
import math
import sys
import time
import threading
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).parent))
from soundmeter import SoundMeter, Storage

# ── Global application state ──────────────────────────────────────────────────

class AppState:
    def __init__(self):
        self.connected   = False
        self.status_msg  = "Инициализация..."
        self.start_time  = time.time()
        self.meas_count  = 0
        self.lmax        = 0.0
        self.leq_buf: deque = deque(maxlen=3600)   # rolling 1h for Leq
        self.chart_buf: deque = deque(maxlen=120)  # last 2 min for chart
        self.last: dict  = {}

g = AppState()
_clients: Set[WebSocket] = set()
_loop: Optional[asyncio.AbstractEventLoop] = None
_queue: Optional[asyncio.Queue] = None
_storage: Optional[Storage] = None

# ── Leq / Lmax helpers ────────────────────────────────────────────────────────

def compute_leq(buf: deque) -> Optional[float]:
    if not buf:
        return None
    mean_power = sum(10 ** (v / 10) for v in buf) / len(buf)
    return round(10 * math.log10(mean_power), 1)

# ── WebSocket connection manager ──────────────────────────────────────────────

async def broadcast(msg: dict):
    dead = set()
    for ws in _clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)

# ── Device reading thread ─────────────────────────────────────────────────────

def device_thread(serial_port: str, baud: int, addr: int):
    """Runs in a background thread. Puts data dicts into asyncio queue."""
    global _loop, _queue

    def push(msg: dict):
        if _loop and _queue:
            _loop.call_soon_threadsafe(_queue.put_nowait, msg)

    def push_status(msg: str, connected: bool = False):
        g.connected  = connected
        g.status_msg = msg
        push({"type": "status", "connected": connected, "message": msg})

    while True:
        push_status("Подключение...")
        try:
            meter = SoundMeter(serial_port, baud, addr)
        except Exception as e:
            push_status(f"Ошибка порта: {e}")
            time.sleep(5)
            continue

        if not meter.ping():
            push_status("Прибор не отвечает")
            meter.close()
            time.sleep(5)
            continue

        meter.lock_keyboard()
        meter.set_mode(50)            # MODE_SLM
        meter.configure_template()
        push_status("Измерение", connected=True)

        try:
            while True:
                t0 = time.time()
                data = meter.read_data()
                if data is None:
                    push_status("Нет данных", connected=True)
                    time.sleep(1)
                    continue

                push({"type": "data", "data": data})
                elapsed = time.time() - t0
                time.sleep(max(0.0, 1.0 - elapsed))

        except Exception as e:
            push_status(f"Потеря связи: {e}")
            try:
                meter.close()
            except Exception:
                pass
            time.sleep(3)


def demo_thread():
    """Demo mode — generates random data without a real device."""
    import random

    def push(msg: dict):
        if _loop and _queue:
            _loop.call_soon_threadsafe(_queue.put_nowait, msg)

    g.connected  = True
    g.status_msg = "Демо-режим"
    push({"type": "status", "connected": True, "message": "Демо-режим"})

    t = 0
    while True:
        base = 65 + 5 * math.sin(t / 30)
        data = {
            "a_rms":    round(base + random.uniform(-1, 1), 1),
            "a_slow":   round(base - 1 + random.uniform(-0.5, 0.5), 1),
            "a_fast":   round(base + 2 + random.uniform(-1.5, 1.5), 1),
            "a_imp":    round(base + 4 + random.uniform(-1, 1), 1),
            "c_rms":    round(base + 2 + random.uniform(-1, 1), 1),
            "lin_rms":  round(base + 4 + random.uniform(-1, 1), 1),
            "battery_level": 85,
        }
        push({"type": "data", "data": data})
        t += 1
        time.sleep(1)

# ── Data processing task (async) ──────────────────────────────────────────────

async def processor():
    """Reads from queue, updates state, broadcasts to WS clients."""
    global _queue
    while True:
        msg = await _queue.get()

        if msg["type"] == "status":
            await broadcast(msg)
            continue

        data = msg["data"]
        a_rms = data.get("a_rms")

        # Update stats
        if a_rms is not None:
            g.leq_buf.append(a_rms)
            g.chart_buf.append({
                "t": datetime.now().strftime("%H:%M:%S"),
                "v": a_rms,
            })
            if a_rms > g.lmax:
                g.lmax = a_rms

        leq = compute_leq(g.leq_buf)
        g.meas_count += 1
        g.last = data

        if _storage:
            _storage.save(data)

        uptime = int(time.time() - g.start_time)
        await broadcast({
            "type":      "measurement",
            "ts":        datetime.now().strftime("%H:%M:%S"),
            "data":      data,
            "leq":       leq,
            "lmax":      round(g.lmax, 1),
            "count":     g.meas_count,
            "uptime":    uptime,
            "connected": g.connected,
            "status":    g.status_msg,
            "chart":     list(g.chart_buf)[-60:],   # last 60s for chart
        })

# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop, _queue
    _loop  = asyncio.get_event_loop()
    _queue = asyncio.Queue()

    asyncio.create_task(processor())

    args = app.state.args
    if args.demo:
        t = threading.Thread(target=demo_thread, daemon=True)
    else:
        t = threading.Thread(
            target=device_thread,
            args=(args.port, args.baud, args.addr),
            daemon=True,
        )
    t.start()
    yield


app = FastAPI(title="Soundmeter", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)

    # Send current state immediately on connect
    await ws.send_json({
        "type":      "init",
        "connected": g.connected,
        "status":    g.status_msg,
        "leq":       compute_leq(g.leq_buf),
        "lmax":      round(g.lmax, 1),
        "count":     g.meas_count,
        "uptime":    int(time.time() - g.start_time),
        "chart":     list(g.chart_buf)[-60:],
    })

    try:
        while True:
            await ws.receive_text()   # keep-alive
    except WebSocketDisconnect:
        _clients.discard(ws)


@app.get("/api/history")
async def history(limit: int = 100):
    if not _storage:
        return JSONResponse([])
    rows = _storage.conn.execute(
        "SELECT timestamp, data FROM measurements ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return JSONResponse([{"ts": r[0], **json.loads(r[1])} for r in reversed(rows)])


@app.post("/api/reset")
async def reset_stats():
    g.lmax = 0.0
    g.leq_buf.clear()
    g.meas_count = 0
    g.start_time = time.time()
    return {"ok": True}


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Soundmeter web server")
    parser.add_argument("--port",  default="COM3",         help="Serial port")
    parser.add_argument("--baud",  type=int, default=115200)
    parser.add_argument("--addr",  type=int, default=1,    help="Device address")
    parser.add_argument("--db",    default="soundmeter.db")
    parser.add_argument("--host",  default="0.0.0.0")
    parser.add_argument("--web",   type=int, default=8000, help="HTTP port")
    parser.add_argument("--demo",  action="store_true",    help="Demo mode (no device)")
    args = parser.parse_args()

    global _storage
    _storage = Storage(args.db)

    app.state.args = args
    print(f"\n  Soundmeter UI → http://localhost:{args.web}\n")
    uvicorn.run(app, host=args.host, port=args.web, log_level="warning")


if __name__ == "__main__":
    main()
