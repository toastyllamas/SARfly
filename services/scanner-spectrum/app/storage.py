"""SQLite storage for multi-band spectrum energy-detection hits.

Lives in the same shared detections.sqlite3 file as the BLE scanners'
`detections` table (via the ./data volume), but in its own table -- a
spectrum hit has no persistent device identity like a BLE MAC to
tag/label/track across sightings, so it doesn't belong in `detections`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SpectrumHit:
    timestamp_utc: str
    source_unit_id: str
    band: str
    freq_hz: int
    power_dbm: float
    baseline_dbm: float
    lat: float | None
    lon: float | None
    alt_m: float | None
    gps_fix_age_s: float | None


SCHEMA = """
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
);
CREATE INDEX IF NOT EXISTS idx_spectrum_hits_band ON spectrum_hits(band);
CREATE INDEX IF NOT EXISTS idx_spectrum_hits_timestamp ON spectrum_hits(timestamp_utc);
"""


class SpectrumStorage:
    def __init__(self, db_path: str | Path) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(SCHEMA)

    def insert_hit(self, h: SpectrumHit) -> None:
        self._conn.execute(
            """
            INSERT INTO spectrum_hits (
                timestamp_utc, source_unit_id, band, freq_hz, power_dbm,
                baseline_dbm, lat, lon, alt_m, gps_fix_age_s
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                h.timestamp_utc,
                h.source_unit_id,
                h.band,
                h.freq_hz,
                h.power_dbm,
                h.baseline_dbm,
                h.lat,
                h.lon,
                h.alt_m,
                h.gps_fix_age_s,
            ),
        )

    def close(self) -> None:
        self._conn.close()
