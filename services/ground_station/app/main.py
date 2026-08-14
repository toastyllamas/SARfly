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
from tiles import MAX_PREFETCH_TILES, TileCache, count_tiles, tile_list

logger = logging.getLogger("ground_station")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

DB_PATH = os.environ.get("DB_PATH", "/data/detections.sqlite3")
POLL_INTERVAL_S = float(os.environ.get("POLL_INTERVAL_S", "1.0"))
GRID_PRECISION = int(os.environ.get("GRID_PRECISION", "4"))
STATIC_DIR = Path(__file__).parent / "static"
# Offline map-tile cache lives next to the DB (a persisted bind mount), so
# prefetched tiles survive restarts and are available with no connectivity.
TILE_CACHE_DIR = os.environ.get("TILE_CACHE_DIR", str(Path(DB_PATH).parent / "tiles"))

db = Database(DB_PATH)
tile_cache = TileCache(TILE_CACHE_DIR)

# State of the one-at-a-time area prefetch, polled by the UI.
prefetch_state: dict = {"running": False, "total": 0, "done": 0, "errors": 0}


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
    last_spectrum_id = db.max_spectrum_hit_id()
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
        new_spectrum_rows = db.spectrum_hits_since(last_spectrum_id)
        if new_spectrum_rows:
            last_spectrum_id = new_spectrum_rows[-1]["id"]
            await manager.broadcast(
                {
                    "type": "spectrum_hits",
                    "hits": [dict(r) for r in new_spectrum_rows],
                }
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poll_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/sw.js")
async def service_worker() -> FileResponse:
    # Served from the root path (not /static/) so its default scope is "/" and
    # it can control the whole app, not just /static/. Registered only under
    # HTTPS/localhost (see index.html) -- a no-op over the field HTTP AP.
    return FileResponse(
        STATIC_DIR / "sw.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/")
async def index() -> FileResponse:
    # no-cache = the browser must revalidate with the server before reusing a
    # cached copy, so a redeploy shows up on the next normal reload instead of
    # a stale page lingering (Safari/iPad cache the SPA shell aggressively).
    return FileResponse(
        STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
    )


@app.get("/api/devices")
async def api_devices() -> list[dict]:
    return db.device_summary()


@app.get("/api/heatmap")
async def api_heatmap(mac: str | None = None) -> list[dict]:
    return db.heatmap(mac_filter=mac, precision=GRID_PRECISION)


@app.get("/api/localizations")
async def api_localizations() -> dict[str, dict]:
    return db.localizations()


@app.get("/tiles/{z}/{x}/{y}.png")
async def api_tile(z: int, x: int, y: int) -> Response:
    """Serve a map tile from the local cache. On a cache miss, fetch it from
    the tile source (USGS) and cache it (works only when online); if that fails
    -- e.g. in the field with no connectivity and the tile wasn't prefetched --
    return 404 so the map just shows a blank tile there rather than erroring."""
    cached = tile_cache.cached_path(z, x, y)
    if cached is None:
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(None, tile_cache.fetch_and_store, z, x, y)
        except Exception:
            return Response(status_code=404)
        return Response(content=data, media_type="image/png")
    return FileResponse(cached, media_type="image/png")


async def _run_prefetch(tiles: list[tuple[int, int, int]]) -> None:
    """Download any not-yet-cached tiles in `tiles`, one at a time with a small
    delay to stay polite to the tile source, updating prefetch_state as it goes."""
    loop = asyncio.get_running_loop()
    try:
        for z, x, y in tiles:
            if tile_cache.cached_path(z, x, y) is None:
                try:
                    await loop.run_in_executor(None, tile_cache.fetch_and_store, z, x, y)
                    await asyncio.sleep(0.05)  # politeness throttle
                except Exception:
                    prefetch_state["errors"] += 1
            prefetch_state["done"] += 1
    finally:
        prefetch_state["running"] = False


@app.post("/api/tiles/prefetch")
async def api_tiles_prefetch(body: dict) -> dict:
    """Start caching all tiles covering a bbox across a zoom range, so the map
    works offline there later. bbox = {south, west, north, east}."""
    if prefetch_state["running"]:
        return {"ok": False, "reason": "already running", **prefetch_state}
    try:
        south, west = float(body["south"]), float(body["west"])
        north, east = float(body["north"]), float(body["east"])
        zoom_min = int(body.get("zoom_min", 12))
        zoom_max = int(body.get("zoom_max", 16))
    except (KeyError, ValueError, TypeError):
        return {"ok": False, "reason": "bad request -- need south/west/north/east"}
    # Count arithmetically BEFORE materializing -- a wide bbox at high zoom is
    # billions of tiles, and tile_list() on that would hang/OOM the server.
    count = count_tiles(south, west, north, east, zoom_min, zoom_max)
    if count > MAX_PREFETCH_TILES:
        return {"ok": False, "reason": "too many tiles", "count": count,
                "max": MAX_PREFETCH_TILES}
    tiles = tile_list(south, west, north, east, zoom_min, zoom_max)
    prefetch_state.update(running=True, total=len(tiles), done=0, errors=0)
    asyncio.create_task(_run_prefetch(tiles))
    return {"ok": True, "total": len(tiles)}


@app.get("/api/tiles/prefetch")
async def api_tiles_prefetch_status() -> dict:
    return prefetch_state


@app.get("/api/spectrum_hits")
async def api_spectrum_hits() -> list[dict]:
    return db.recent_spectrum_hits()


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


@app.post("/api/vacuum")
async def api_vacuum() -> dict:
    return db.vacuum()


@app.post("/api/reset")
async def api_reset() -> dict:
    db.reset()
    await manager.broadcast({"type": "devices", "devices": db.device_summary()})
    await manager.broadcast({"type": "spectrum_hits", "hits": [], "reset": True})
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
        await ws.send_json({"type": "spectrum_hits", "hits": db.recent_spectrum_hits()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(ws)
