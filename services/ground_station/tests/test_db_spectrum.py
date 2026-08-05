"""Tests for Database's spectrum_hits query methods."""

from __future__ import annotations

import sqlite3

from db import Database

_SPECTRUM_SCHEMA = """
CREATE TABLE IF NOT EXISTS spectrum_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    source_unit_id TEXT NOT NULL,
    band TEXT NOT NULL,
    freq_hz INTEGER NOT NULL,
    power_dbm REAL NOT NULL,
    baseline_dbm REAL NOT NULL,
    lat REAL,
    lon REAL,
    alt_m REAL,
    gps_fix_age_s REAL
)
"""


def _seed_spectrum_hit(db_path, **overrides):
    conn = sqlite3.connect(db_path)
    conn.execute(_SPECTRUM_SCHEMA)
    row = {
        "timestamp_utc": "2026-08-05T14:23:01+00:00",
        "source_unit_id": "ground-logger-spectrum-01",
        "band": "ism_2_4ghz",
        "freq_hz": 2450000000,
        "power_dbm": -40.0,
        "baseline_dbm": -70.0,
        "lat": 45.1,
        "lon": -122.2,
        "alt_m": 350.0,
        "gps_fix_age_s": 1.2,
        **overrides,
    }
    conn.execute(
        """
        INSERT INTO spectrum_hits (
            timestamp_utc, source_unit_id, band, freq_hz, power_dbm,
            baseline_dbm, lat, lon, alt_m, gps_fix_age_s
        ) VALUES (:timestamp_utc, :source_unit_id, :band, :freq_hz, :power_dbm,
                   :baseline_dbm, :lat, :lon, :alt_m, :gps_fix_age_s)
        """,
        row,
    )
    conn.commit()
    conn.close()


def test_max_spectrum_hit_id_reflects_inserted_row(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    _seed_spectrum_hit(db_path)
    db = Database(str(db_path))
    assert db.max_spectrum_hit_id() == 1


def test_max_spectrum_hit_id_returns_zero_when_table_missing(tmp_path):
    db_path = tmp_path / "empty.sqlite3"
    db = Database(str(db_path))  # nothing seeded -- spectrum_hits never created
    assert db.max_spectrum_hit_id() == 0


def test_spectrum_hits_since_returns_new_rows_only(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    _seed_spectrum_hit(db_path, band="keyfob", freq_hz=433920000)
    _seed_spectrum_hit(db_path, band="cellular_low", freq_hz=850000000)
    db = Database(str(db_path))
    rows = db.spectrum_hits_since(1)
    assert len(rows) == 1
    assert rows[0]["band"] == "cellular_low"


def test_spectrum_hits_since_returns_empty_list_when_table_missing(tmp_path):
    db_path = tmp_path / "empty.sqlite3"
    db = Database(str(db_path))
    assert db.spectrum_hits_since(0) == []


def test_recent_spectrum_hits_shape(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    _seed_spectrum_hit(db_path)
    db = Database(str(db_path))
    hits = db.recent_spectrum_hits()
    assert len(hits) == 1
    assert hits[0]["band"] == "ism_2_4ghz"
    assert hits[0]["freq_hz"] == 2450000000
    assert hits[0]["lat"] == 45.1


def test_recent_spectrum_hits_returns_empty_list_when_table_missing(tmp_path):
    db_path = tmp_path / "empty.sqlite3"
    db = Database(str(db_path))
    assert db.recent_spectrum_hits() == []


def test_reset_clears_spectrum_hits(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    _seed_spectrum_hit(db_path)
    db = Database(str(db_path))
    assert db.max_spectrum_hit_id() == 1
    db.reset()
    assert db.recent_spectrum_hits() == []


def test_reset_does_not_crash_when_spectrum_hits_missing(tmp_path):
    db_path = tmp_path / "empty.sqlite3"
    db = Database(str(db_path))
    db.reset()  # must not raise even though spectrum_hits was never created
