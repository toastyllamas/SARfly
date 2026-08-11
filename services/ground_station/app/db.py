"""Reads the scanner's detection log and owns the device_tags table.

Both tables live in the same SQLite file (shared via a volume with the
scanner service). WAL mode lets this process read/write concurrently with
the scanner's inserts.
"""

from __future__ import annotations

import os
import sqlite3

from localize import Sample, estimate
from vendor_id import classify

TAGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_tags (
    mac TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'unknown',
    label TEXT,
    tagged_at TEXT
);
"""


class Database:
    def __init__(self, db_path: str) -> None:
        # isolation_level=None -> autocommit; without it, sqlite3 leaves the
        # write from set_tag() in an open transaction, which freezes this
        # connection's view of the database and hides subsequent inserts
        # made by the scanner process.
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        # Let writes/VACUUM wait briefly for a gap rather than erroring out the
        # instant a scanner happens to hold the write lock.
        self._conn.execute("PRAGMA busy_timeout=5000;")
        self._conn.executescript(TAGS_SCHEMA)

    def max_detection_id(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM detections"
            ).fetchone()
        except sqlite3.OperationalError:
            return 0  # detections table not created yet -- no BLE/WiFi scanner here
        return row["m"]

    def detections_since(self, since_id: int) -> list[sqlite3.Row]:
        try:
            return self._conn.execute(
                "SELECT * FROM detections WHERE id > ? ORDER BY id", (since_id,)
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # no detections table (spectrum-only deployment)

    def device_summary(self) -> list[dict]:
        try:
            latest_rows = self._conn.execute(
                """
                SELECT d.mac, d.device_name, d.rssi_dbm, d.lat, d.lon, d.alt_m,
                       d.timestamp_utc AS last_seen
                FROM detections d
                JOIN (
                    SELECT mac, MAX(id) AS max_id FROM detections GROUP BY mac
                ) latest ON d.mac = latest.mac AND d.id = latest.max_id
                """
            ).fetchall()
            count_rows = self._conn.execute(
                """
                SELECT mac, COUNT(*) AS count, MIN(timestamp_utc) AS first_seen
                FROM detections GROUP BY mac
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return []  # no detections table yet -- nothing to summarize
        tag_rows = self._conn.execute(
            "SELECT mac, status, label FROM device_tags"
        ).fetchall()

        counts_by_mac = {r["mac"]: r for r in count_rows}
        tags_by_mac = {r["mac"]: r for r in tag_rows}
        vendor_by_mac = self._vendor_guesses()

        devices = []
        for row in latest_rows:
            mac = row["mac"]
            count_row = counts_by_mac.get(mac)
            tag_row = tags_by_mac.get(mac)
            vendor_guess = vendor_by_mac.get(mac)
            devices.append(
                {
                    "mac": mac,
                    "device_name": row["device_name"],
                    "rssi_dbm": row["rssi_dbm"],
                    "lat": row["lat"],
                    "lon": row["lon"],
                    "alt_m": row["alt_m"],
                    "last_seen": row["last_seen"],
                    "first_seen": count_row["first_seen"] if count_row else row["last_seen"],
                    "count": count_row["count"] if count_row else 1,
                    "status": tag_row["status"] if tag_row else "unknown",
                    "label": tag_row["label"] if tag_row else None,
                    "vendor_guess": vendor_guess["vendor"] if vendor_guess else None,
                    "vendor_confidence": vendor_guess["confidence"] if vendor_guess else None,
                }
            )
        devices.sort(key=lambda d: d["last_seen"], reverse=True)
        return devices

    def _vendor_guesses(self) -> dict[str, dict]:
        """Classify every distinct (name, advertisement-data) variant ever
        seen per MAC, not just the latest one -- identifying fields like
        manufacturer data or a service UUID don't necessarily appear in
        every single advertising event, so checking only the newest
        detection risks false negatives.
        """
        try:
            rows = self._conn.execute(
                "SELECT DISTINCT mac, device_name, adv_data_json FROM detections"
            ).fetchall()
        except sqlite3.OperationalError:
            return {}  # no detections table yet
        guesses: dict[str, dict] = {}
        for r in rows:
            mac = r["mac"]
            if mac in guesses:
                continue  # already found a match for this mac
            guess = classify(mac, r["device_name"], r["adv_data_json"])
            if guess:
                guesses[mac] = guess
        return guesses

    def localizations(self) -> dict[str, dict]:
        """Estimate an emitter location for every device with enough
        positioned detections, keyed by MAC.

        Pulls all positioned (lat, lon, rssi) samples in one pass and groups
        by MAC in Python -- the estimator is a closed-form weighted centroid,
        so even tens of thousands of samples localize in milliseconds.
        Devices that can't be localized (too few samples, or heard at flat
        RSSI) are simply absent from the result.
        """
        try:
            rows = self._conn.execute(
                """
                SELECT mac, lat, lon, rssi_dbm FROM detections
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                ORDER BY mac
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return {}  # no detections table (spectrum-only deployment)
        by_mac: dict[str, list[Sample]] = {}
        for r in rows:
            by_mac.setdefault(r["mac"], []).append(
                Sample(lat=r["lat"], lon=r["lon"], rssi_dbm=r["rssi_dbm"])
            )
        out: dict[str, dict] = {}
        for mac, samples in by_mac.items():
            result = estimate(samples)
            if result is None:
                continue
            out[mac] = {
                "lat": result.lat,
                "lon": result.lon,
                "semi_major_m": result.semi_major_m,
                "semi_minor_m": result.semi_minor_m,
                "orientation_deg": result.orientation_deg,
                "confidence": result.confidence,
                "n_samples": result.n_samples,
                "rssi_span_db": result.rssi_span_db,
            }
        return out

    def heatmap(self, mac_filter: str | None = None, precision: int = 4) -> list[dict]:
        """Grid-bin all positioned detections by rounded lat/lon (roughly
        11m per grid step at precision=4) and count hits per cell.
        """
        query = """
            SELECT ROUND(lat, ?) AS glat, ROUND(lon, ?) AS glon, COUNT(*) AS hits
            FROM detections
            WHERE lat IS NOT NULL AND lon IS NOT NULL
        """
        params: list = [precision, precision]
        if mac_filter:
            query += " AND mac LIKE ?"
            params.append(f"%{mac_filter}%")
        query += " GROUP BY glat, glon"
        try:
            rows = self._conn.execute(query, params).fetchall()
        except sqlite3.OperationalError:
            return []  # no detections table (spectrum-only deployment)
        return [{"lat": r["glat"], "lon": r["glon"], "hits": r["hits"]} for r in rows]

    def max_spectrum_hit_id(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM spectrum_hits"
            ).fetchone()
        except sqlite3.OperationalError:
            return 0  # scanner-spectrum hasn't run yet, or isn't deployed at all
        return row["m"]

    def spectrum_hits_since(self, since_id: int) -> list[sqlite3.Row]:
        try:
            return self._conn.execute(
                "SELECT * FROM spectrum_hits WHERE id > ? ORDER BY id", (since_id,)
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    def recent_spectrum_hits(self, limit: int = 2000) -> list[dict]:
        try:
            rows = self._conn.execute(
                """
                SELECT id, timestamp_utc, source_unit_id, band, freq_hz, power_dbm,
                       baseline_dbm, lat, lon, alt_m, gps_fix_age_s
                FROM spectrum_hits
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    def set_tag(self, mac: str, status: str, label: str | None) -> None:
        self._conn.execute(
            """
            INSERT INTO device_tags (mac, status, label, tagged_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(mac) DO UPDATE SET
                status = excluded.status,
                label = excluded.label,
                tagged_at = excluded.tagged_at
            """,
            (mac, status, label),
        )

    def reset(self) -> None:
        """Wipe all detections, spectrum hits, and tags. Deliberately leaves
        sqlite's autoincrement bookkeeping alone so new ids keep climbing
        rather than restarting at 1 -- that keeps the poll loop's "highest id
        seen so far" tracking valid with no extra coordination after a reset.
        """
        try:
            self._conn.execute("DELETE FROM detections")
        except sqlite3.OperationalError:
            pass  # scanner may not have created it yet
        try:
            self._conn.execute("DELETE FROM device_tags")
        except sqlite3.OperationalError:
            pass  # unlikely, but handle gracefully
        try:
            self._conn.execute("DELETE FROM spectrum_hits")
        except sqlite3.OperationalError:
            pass  # scanner-spectrum never deployed -- nothing to clear
        # A bulk DELETE leaves the freed pages in the file rather than
        # returning them to the OS -- the main source of the file ballooning
        # to hundreds of MB for a few thousand rows. Reclaim right here, when
        # the tables were just emptied and there's almost nothing to rewrite.
        try:
            self.vacuum()
        except sqlite3.OperationalError:
            pass  # couldn't get the lock in time; not worth failing a reset over

    def vacuum(self) -> dict:
        """Rebuild the database file to reclaim free pages SQLite otherwise
        keeps (see reset()), and truncate the WAL. Returns before/after file
        sizes in bytes so callers can report how much was reclaimed.

        Safe to run during live capture: the connection's busy_timeout lets
        VACUUM wait for a brief gap in scanner writes. VACUUM needs temp space
        roughly the size of the current file while it rebuilds.
        """
        before = os.path.getsize(self._db_path) if os.path.exists(self._db_path) else 0
        self._conn.execute("VACUUM")
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = os.path.getsize(self._db_path) if os.path.exists(self._db_path) else 0
        return {"before_bytes": before, "after_bytes": after}

    def bulk_set_status(self, macs: list[str], status: str) -> None:
        """Set status for many devices at once, preserving any existing
        label (only set_tag touches labels).
        """
        self._conn.executemany(
            """
            INSERT INTO device_tags (mac, status, label, tagged_at)
            VALUES (?, ?, NULL, datetime('now'))
            ON CONFLICT(mac) DO UPDATE SET
                status = excluded.status,
                tagged_at = excluded.tagged_at
            """,
            [(mac, status) for mac in macs],
        )
