"""Tests for SpectrumStorage."""

from __future__ import annotations

import sqlite3

from storage import SpectrumHit, SpectrumStorage


def test_insert_hit_persists_row(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    storage = SpectrumStorage(db_path)
    hit = SpectrumHit(
        timestamp_utc="2026-08-05T14:23:01+00:00",
        source_unit_id="ground-logger-spectrum-01",
        band="ism_2_4ghz",
        freq_hz=2450000000,
        power_dbm=-40.0,
        baseline_dbm=-70.0,
        lat=45.1,
        lon=-122.2,
        alt_m=350.0,
        gps_fix_age_s=1.2,
    )
    storage.insert_hit(hit)
    storage.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM spectrum_hits").fetchone()
    assert row["band"] == "ism_2_4ghz"
    assert row["freq_hz"] == 2450000000
    assert row["power_dbm"] == -40.0
    assert row["baseline_dbm"] == -70.0
    assert row["lat"] == 45.1
    assert row["source_unit_id"] == "ground-logger-spectrum-01"
    conn.close()


def test_insert_hit_allows_null_gps(tmp_path):
    db_path = tmp_path / "test2.sqlite3"
    storage = SpectrumStorage(db_path)
    hit = SpectrumHit(
        timestamp_utc="2026-08-05T14:23:01+00:00",
        source_unit_id="ground-logger-spectrum-01",
        band="keyfob",
        freq_hz=433920000,
        power_dbm=-55.0,
        baseline_dbm=-80.0,
        lat=None,
        lon=None,
        alt_m=None,
        gps_fix_age_s=None,
    )
    storage.insert_hit(hit)
    storage.close()

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT lat, lon FROM spectrum_hits").fetchone()
    assert row == (None, None)
    conn.close()
