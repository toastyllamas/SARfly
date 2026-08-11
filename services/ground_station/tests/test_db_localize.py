"""Tests for Database.localizations() -- grouping detections by MAC and
running the localizer over each device's positioned samples."""

from __future__ import annotations

import math
import random
import sqlite3

from db import Database

_DETECTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    source_unit_id TEXT NOT NULL,
    mac TEXT NOT NULL,
    device_name TEXT,
    rssi_dbm INTEGER NOT NULL,
    tx_power_dbm INTEGER,
    adv_data_json TEXT,
    lat REAL,
    lon REAL,
    alt_m REAL,
    gps_fix_age_s REAL
)
"""

_LAT0, _LON0 = 44.5, -110.5


def _offset(east_m, north_m):
    return _LAT0 + north_m / 110_540.0, _LON0 + east_m / (
        111_320.0 * math.cos(math.radians(_LAT0))
    )


def _seed(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute(_DETECTIONS_SCHEMA)
    rng = random.Random(1)
    rows = []
    # A lawnmower path over a 160 m box.
    path = []
    for r in range(8):
        y = -80 + r * (160 / 7)
        xs = range(-80, 81, 4)
        path.extend((x, y) for x in (xs if r % 2 == 0 else reversed(list(xs))))

    # AA: a localizable emitter the path passes over, at (20, -10).
    for e, n in path:
        d = max(math.hypot(e - 20, n + 10), 1.0)
        rssi = int(-40 - 30 * math.log10(d) + rng.gauss(0, 4))
        lat, lon = _offset(e, n)
        rows.append(("AA:AA:AA:AA:AA:AA", rssi, lat, lon))
    # BB: heard at flat, weak RSSI everywhere -> not localizable.
    for e, n in path:
        lat, lon = _offset(e, n)
        rows.append(("BB:BB:BB:BB:BB:BB", int(-88 + rng.gauss(0, 1)), lat, lon))
    # CC: too few samples.
    for e, n in path[:3]:
        lat, lon = _offset(e, n)
        rows.append(("CC:CC:CC:CC:CC:CC", -50, lat, lon))

    conn.executemany(
        "INSERT INTO detections (timestamp_utc, source_unit_id, mac, rssi_dbm, lat, lon) "
        "VALUES ('2026-08-05T00:00:00+00:00', 'u', ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_localizes_only_localizable_devices(tmp_path):
    db_path = str(tmp_path / "d.sqlite3")
    _seed(db_path)
    locs = Database(db_path).localizations()

    assert "AA:AA:AA:AA:AA:AA" in locs
    assert "BB:BB:BB:BB:BB:BB" not in locs  # flat RSSI
    assert "CC:CC:CC:CC:CC:CC" not in locs  # too few samples

    fix = locs["AA:AA:AA:AA:AA:AA"]
    east = (fix["lon"] - _LON0) * 111_320.0 * math.cos(math.radians(_LAT0))
    north = (fix["lat"] - _LAT0) * 110_540.0
    assert math.hypot(east - 20, north + 10) < 20.0
    assert fix["confidence"] > 0.3
