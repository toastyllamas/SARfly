"""Offline map-tile cache for field use.

The ground station's base map needs tile imagery, but in the field the
SARfly access point has no internet, so tiles fetched live from OpenStreetMap
never load (a black map with only the vector markers drawn on top). This
module lets the ground station serve tiles from a local on-disk cache
(/data/tiles) instead: tiles requested while online are cached through, and a
prefetch pass downloads a chosen area's tiles ahead of time so the map works
with no connectivity.

Split into pure slippy-map tile math (testable, no I/O) and a small cache
that reads/writes the tile directory and fetches misses from OSM. The network
fetch is a module-level function so tests can substitute it.
"""

from __future__ import annotations

import math
import os
import urllib.request

# OSM's tile usage policy requires an identifying User-Agent and rules out
# heavy bulk downloading; prefetch is deliberately capped (see MAX_PREFETCH_TILES)
# and rate-limited to stay within acceptable low-volume use for a field tool.
OSM_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "SARfly/1.0 (offline SAR field map cache; low-volume area prefetch)"

MAX_PREFETCH_TILES = 20000  # hard cap so one request can't blow up disk / hammer OSM


def deg2tile(lat_deg: float, lon_deg: float, z: int) -> tuple[int, int]:
    """Slippy-map tile (x, y) containing a lat/lon at zoom z."""
    lat_rad = math.radians(lat_deg)
    n = 2 ** z
    x = int((lon_deg + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    # Clamp to the valid range so a bbox edge exactly on 180/85 doesn't overflow.
    x = max(0, min(n - 1, x))
    y = max(0, min(n - 1, y))
    return x, y


def tiles_in_bbox(south: float, west: float, north: float, east: float, z: int) -> list[tuple[int, int, int]]:
    """Every (z, x, y) tile covering the bbox at zoom z."""
    x0, y0 = deg2tile(north, west, z)  # north-west corner -> smallest y
    x1, y1 = deg2tile(south, east, z)  # south-east corner -> largest y
    return [
        (z, x, y)
        for x in range(min(x0, x1), max(x0, x1) + 1)
        for y in range(min(y0, y1), max(y0, y1) + 1)
    ]


def count_tiles(south: float, west: float, north: float, east: float,
                zoom_min: int, zoom_max: int) -> int:
    """How many tiles tile_list() would produce, computed arithmetically
    WITHOUT building the list. Callers must check this against a cap before
    calling tile_list -- a wide bbox at high zoom is billions of tiles, and
    materializing that list would hang/OOM the server."""
    total = 0
    for z in range(zoom_min, zoom_max + 1):
        x0, y0 = deg2tile(north, west, z)
        x1, y1 = deg2tile(south, east, z)
        total += (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)
    return total


def tile_list(south: float, west: float, north: float, east: float,
              zoom_min: int, zoom_max: int) -> list[tuple[int, int, int]]:
    """All (z, x, y) tiles covering the bbox across an inclusive zoom range.
    Guard with count_tiles() first -- this eagerly builds the whole list."""
    out: list[tuple[int, int, int]] = []
    for z in range(zoom_min, zoom_max + 1):
        out.extend(tiles_in_bbox(south, west, north, east, z))
    return out


def _fetch_tile(z: int, x: int, y: int, timeout: float = 10.0) -> bytes:
    """Download one tile from OSM. Isolated so tests can monkeypatch it."""
    req = urllib.request.Request(
        OSM_URL.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


class TileCache:
    def __init__(self, cache_dir: str) -> None:
        self._dir = cache_dir

    def path(self, z: int, x: int, y: int) -> str:
        return os.path.join(self._dir, str(z), str(x), f"{y}.png")

    def cached_path(self, z: int, x: int, y: int) -> str | None:
        """Path of the tile if it's already on disk, else None."""
        p = self.path(z, x, y)
        return p if os.path.exists(p) else None

    def store(self, z: int, x: int, y: int, data: bytes) -> str:
        p = self.path(z, x, y)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        # Write atomically so a crash mid-write can't leave a truncated tile
        # that would then be served forever as "cached".
        tmp = p + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
        return p

    def fetch_and_store(self, z: int, x: int, y: int) -> bytes:
        data = _fetch_tile(z, x, y)
        self.store(z, x, y, data)
        return data
