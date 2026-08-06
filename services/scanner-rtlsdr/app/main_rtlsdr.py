"""RTL-SDR multi-band spectrum energy-detection logger.

Uses an RTL-SDR USB dongle with rtl_power to sweep a fixed default list of
frequency bands (keyfob, cellular_low -- scoped to R820T tuner coverage),
GPS-tags any reading that exceeds that band's per-frequency calibrated
baseline, and appends it to the shared SQLite log as a spectrum_hits row.
Meant to run alongside the BLE scanners (main.py, main_ubertooth.py),
tagged with its own SOURCE_UNIT_ID -- a spectrum hit has no persistent
device identity like a BLE MAC, so it isn't merged into the detections
table. See docs/superpowers/specs/2026-08-06-rtlsdr-spectrum-scanner-design.md
for the full design.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from datetime import UTC, datetime

from gps_client import GpsClient
from rtlsdr_source import DEFAULT_CALIBRATION_S, DEFAULT_DWELL_S, DEFAULT_MARGIN_DB, stream_hits
from storage import SpectrumHit, SpectrumStorage

logger = logging.getLogger("scanner-rtlsdr")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


DB_PATH = _env("DB_PATH", "/data/detections.sqlite3")
GPSD_HOST = _env("GPSD_HOST", "127.0.0.1")
GPSD_PORT = int(_env("GPSD_PORT", "2947"))
SOURCE_UNIT_ID = _env("SOURCE_UNIT_ID", "ground-logger-rtlsdr")
RTLSDR_CALIBRATION_S = float(_env("RTLSDR_CALIBRATION_S", str(DEFAULT_CALIBRATION_S)))
RTLSDR_DWELL_S = float(_env("RTLSDR_DWELL_S", str(DEFAULT_DWELL_S)))
RTLSDR_HIT_MARGIN_DB = float(_env("RTLSDR_HIT_MARGIN_DB", str(DEFAULT_MARGIN_DB)))
LOG_LEVEL = _env("LOG_LEVEL", "INFO")

STALE_FIX_WARN_S = 30.0


async def run() -> None:
    logging.basicConfig(
        level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger.info(
        "starting rtlsdr scanner: db=%s gpsd=%s:%s calibration=%.0fs dwell=%.0fs margin=%.0fdB unit=%s",
        DB_PATH,
        GPSD_HOST,
        GPSD_PORT,
        RTLSDR_CALIBRATION_S,
        RTLSDR_DWELL_S,
        RTLSDR_HIT_MARGIN_DB,
        SOURCE_UNIT_ID,
    )

    storage = SpectrumStorage(DB_PATH)
    gps = GpsClient(GPSD_HOST, GPSD_PORT)
    gps_task = asyncio.create_task(gps.run())

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    async def consume() -> None:
        async for reading in stream_hits(
            margin_db=RTLSDR_HIT_MARGIN_DB,
            calibration_s=RTLSDR_CALIBRATION_S,
            dwell_s=RTLSDR_DWELL_S,
        ):
            fix = gps.latest_fix()
            fix_age = time.monotonic() - fix.received_at if fix else None
            if fix_age is not None and fix_age > STALE_FIX_WARN_S:
                logger.warning("GPS fix is %.0fs old", fix_age)

            hit = SpectrumHit(
                timestamp_utc=datetime.now(UTC).isoformat(),
                source_unit_id=SOURCE_UNIT_ID,
                band=reading.band,
                freq_hz=reading.freq_hz,
                power_dbm=reading.power_dbm,
                baseline_dbm=reading.baseline_dbm,
                lat=fix.lat if fix else None,
                lon=fix.lon if fix else None,
                alt_m=fix.alt_m if fix else None,
                gps_fix_age_s=fix_age,
            )
            storage.insert_hit(hit)
            logger.debug(
                "hit band=%s freq=%.3fMHz power=%.1fdBm baseline=%.1fdBm lat=%s lon=%s",
                hit.band,
                hit.freq_hz / 1_000_000,
                hit.power_dbm,
                hit.baseline_dbm,
                hit.lat,
                hit.lon,
            )

    consume_task = asyncio.create_task(consume())
    logger.info("sweeping...")
    await stop_event.wait()

    logger.info("shutting down")
    consume_task.cancel()
    gps_task.cancel()
    storage.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
