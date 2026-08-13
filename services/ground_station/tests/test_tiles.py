"""Tests for the offline tile cache: slippy-map math (pure) and the on-disk
cache (fetch monkeypatched so no network)."""

from __future__ import annotations

import tiles
from tiles import TileCache, count_tiles, deg2tile, tile_list, tiles_in_bbox


def test_count_tiles_matches_list_length():
    bbox = (44.40, -110.60, 44.50, -110.40)
    assert count_tiles(*bbox, 12, 16) == len(tile_list(*bbox, 12, 16))


def test_count_tiles_is_cheap_for_huge_bbox():
    # Whole-world-ish bbox at high zoom: the count must come back instantly
    # (arithmetic), never by building the multi-billion-tile list.
    n = count_tiles(-60.0, -160.0, 70.0, 160.0, 12, 16)
    assert n > 20000  # far past any sane cap -> caller rejects without materializing


def test_deg2tile_quadrants_at_zoom_1():
    # Zoom 1 splits the world into 2x2 tiles: west/east by lon 0, north/south by lat 0.
    assert deg2tile(45.0, -90.0, 1) == (0, 0)   # NW
    assert deg2tile(45.0, 90.0, 1) == (1, 0)    # NE
    assert deg2tile(-45.0, -90.0, 1) == (0, 1)  # SW
    assert deg2tile(-45.0, 90.0, 1) == (1, 1)   # SE


def test_deg2tile_clamps_to_valid_range():
    z = 5
    n = 2 ** z
    x, y = deg2tile(85.0, 180.0, z)  # extreme edges
    assert 0 <= x < n and 0 <= y < n


def test_tiles_in_bbox_covers_both_corners():
    south, west, north, east = 44.40, -110.60, 44.50, -110.40
    z = 14
    result = tiles_in_bbox(south, west, north, east, z)
    assert (z,) + deg2tile(north, west, z) in result  # NW corner tile present
    assert (z,) + deg2tile(south, east, z) in result  # SE corner tile present
    # It's a contiguous rectangle of tiles.
    xs = {x for _, x, _ in result}
    ys = {y for _, _, y in result}
    assert len(result) == len(xs) * len(ys)


def test_tile_list_spans_zoom_range():
    bbox = (44.40, -110.60, 44.50, -110.40)
    zmin, zmax = 12, 15
    all_tiles = tile_list(*bbox, zmin, zmax)
    per_zoom = {z: sum(1 for tz, _, _ in all_tiles if tz == z) for z in range(zmin, zmax + 1)}
    assert set(per_zoom) == {12, 13, 14, 15}
    assert len(all_tiles) == sum(per_zoom.values())
    # Higher zoom covers the same area with more (>=) tiles.
    assert per_zoom[15] >= per_zoom[12]


def test_cache_store_then_hit(tmp_path):
    c = TileCache(str(tmp_path))
    assert c.cached_path(14, 100, 200) is None
    c.store(14, 100, 200, b"\x89PNG-fake")
    p = c.cached_path(14, 100, 200)
    assert p is not None
    assert open(p, "rb").read() == b"\x89PNG-fake"


def test_fetch_and_store_uses_injected_fetcher(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(z, x, y, timeout=10.0):
        calls.append((z, x, y))
        return b"tiledata"

    monkeypatch.setattr(tiles, "_fetch_tile", fake_fetch)
    c = TileCache(str(tmp_path))
    data = c.fetch_and_store(13, 7, 9)
    assert data == b"tiledata"
    assert calls == [(13, 7, 9)]
    assert c.cached_path(13, 7, 9) is not None
