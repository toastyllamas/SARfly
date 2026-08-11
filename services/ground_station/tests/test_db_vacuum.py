"""Tests for Database.vacuum() reclaiming free pages after deletes."""

from __future__ import annotations

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
    lat REAL, lon REAL, alt_m REAL, gps_fix_age_s REAL
)
"""


def _bloat(db_path, n=20000):
    conn = sqlite3.connect(db_path)
    conn.execute(_DETECTIONS_SCHEMA)
    conn.executemany(
        "INSERT INTO detections (timestamp_utc, source_unit_id, mac, rssi_dbm, adv_data_json) "
        "VALUES ('2026-08-05T00:00:00+00:00', 'u', ?, -50, ?)",
        [(f"AA:BB:CC:{i:04x}"[:17], "x" * 200) for i in range(n)],
    )
    conn.commit()
    conn.close()


def test_vacuum_reclaims_space_after_delete(tmp_path):
    db_path = str(tmp_path / "d.sqlite3")
    _bloat(db_path)
    db = Database(db_path)
    db._conn.execute("DELETE FROM detections")  # free pages, but file stays big
    res = db.vacuum()
    assert res["after_bytes"] < res["before_bytes"]


def test_reset_compacts_the_file(tmp_path):
    # reset() should leave a small file, not one bloated by the just-deleted rows.
    db_path = str(tmp_path / "d.sqlite3")
    _bloat(db_path)
    db = Database(db_path)
    import os
    big = os.path.getsize(db_path)
    db.reset()
    assert os.path.getsize(db_path) < big
