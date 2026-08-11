"""SQLite storage for BLE detection records."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Detection:
    timestamp_utc: str
    source_unit_id: str
    mac: str
    device_name: str | None
    rssi_dbm: int
    tx_power_dbm: int | None
    adv_data_json: str
    lat: float | None
    lon: float | None
    alt_m: float | None
    gps_fix_age_s: float | None


SCHEMA = """
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
);
CREATE INDEX IF NOT EXISTS idx_detections_mac ON detections(mac);
CREATE INDEX IF NOT EXISTS idx_detections_timestamp ON detections(timestamp_utc);
"""


class Storage:
    """Thin wrapper around a single-writer SQLite log.

    WAL mode lets a second process (e.g. the ground-station app) read the
    same file concurrently without blocking this writer.
    """

    def __init__(self, db_path: str | Path) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        # Wait briefly for the write lock instead of erroring out with
        # "database is locked" and dropping the detection -- several scanners
        # plus the ground station all write/read this file concurrently.
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.executescript(SCHEMA)

    def insert_detection(self, d: Detection) -> None:
        self._conn.execute(
            """
            INSERT INTO detections (
                timestamp_utc, source_unit_id, mac, device_name, rssi_dbm,
                tx_power_dbm, adv_data_json, lat, lon, alt_m, gps_fix_age_s
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                d.timestamp_utc,
                d.source_unit_id,
                d.mac,
                d.device_name,
                d.rssi_dbm,
                d.tx_power_dbm,
                d.adv_data_json,
                d.lat,
                d.lon,
                d.alt_m,
                d.gps_fix_age_s,
            ),
        )

    def close(self) -> None:
        self._conn.close()


def adv_data_to_json(manufacturer_data: dict, service_data: dict, service_uuids: list) -> str:
    return json.dumps(
        {
            "manufacturer_data": {
                str(k): v.hex() if isinstance(v, bytes) else v
                for k, v in manufacturer_data.items()
            },
            "service_data": {
                str(k): v.hex() if isinstance(v, bytes) else v for k, v in service_data.items()
            },
            "service_uuids": list(service_uuids),
        }
    )
