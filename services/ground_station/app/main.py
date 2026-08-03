"""Ground station: live view of the scanner's detection log.

Serves a single-page UI for tagging known devices (so new/unknown ones stand
out), a live-updating map, and on-demand KMZ export. Reads the same SQLite
file the scanner writes to; polls for new rows since there's no built-in
SQLite change notification, and broadcasts updates to connected browsers
over a WebSocket.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from db import Database
from kmz import build_kmz

logger = logging.getLogger("ground_station")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

DB_PATH = os.environ.get("DB_PATH", "/data/detections.sqlite3")
POLL_INTERVAL_S = float(os.environ.get("POLL_INTERVAL_S", "1.0"))
GRID_PRECISION = int(os.environ.get("GRID_PRECISION", "4"))
STATIC_DIR = Path(__file__).parent / "static"

db = Database(DB_PATH)


class ConnectionManager:
    def __init__(self) -> None:
        self._sockets: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._sockets.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._sockets.discard(ws)

    async def broadcast(self, message: dict) -> None:
        dead = []
        for ws in self._sockets:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._sockets.discard(ws)


manager = ConnectionManager()


async def poll_loop() -> None:
    last_id = db.max_detection_id()
    while True:
        await asyncio.sleep(POLL_INTERVAL_S)
        new_rows = db.detections_since(last_id)
        if new_rows:
            last_id = new_rows[-1]["id"]
            await manager.broadcast(
                {
                    "type": "devices",
                    "devices": db.device_summary(),
                    "new_count": len(new_rows),
                }
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poll_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/devices")
async def api_devices() -> list[dict]:
    return db.device_summary()


@app.get("/api/heatmap")
async def api_heatmap(mac: str | None = None) -> list[dict]:
    return db.heatmap(mac_filter=mac, precision=GRID_PRECISION)


@app.post("/api/devices/{mac}/tag")
async def api_tag(mac: str, body: dict) -> dict:
    status = body.get("status", "unknown")
    label = body.get("label")
    db.set_tag(mac, status, label)
    await manager.broadcast({"type": "devices", "devices": db.device_summary()})
    return {"ok": True}


@app.post("/api/devices/bulk_tag")
async def api_bulk_tag(body: dict) -> dict:
    macs = body.get("macs", [])
    status = body.get("status", "unknown")
    db.bulk_set_status(macs, status)
    await manager.broadcast({"type": "devices", "devices": db.device_summary()})
    return {"ok": True, "count": len(macs)}


@app.post("/api/reset")
async def api_reset() -> dict:
    db.reset()
    await manager.broadcast({"type": "devices", "devices": db.device_summary()})
    return {"ok": True}


@app.get("/api/kmz")
async def api_kmz() -> Response:
    data = build_kmz(db.device_summary())
    return Response(
        content=data,
        media_type="application/vnd.google-earth.kmz",
        headers={"Content-Disposition": "attachment; filename=ble_detections.kmz"},
    )


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        await ws.send_json({"type": "devices", "devices": db.device_summary()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
