"""Ground/wide-area BLE detection logger -- Ubertooth One variant.

Same job as main.py (log GPS-tagged BLE advertisements to the shared SQLite
detections log), but sourced from an Ubertooth One's raw-RF capture instead
of a BlueZ/HCI adapter -- see ubertooth_source.py for why those are two
separate code paths rather than one adapter-swappable one. Meant to run
alongside main.py's scanner, tagged with a distinct SOURCE_UNIT_ID, not
instead of it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import UTC, datetime

from gps_client import GpsClient
from storage import Detection, Storage
from ubertooth_source import ParsedAdvertisement, stream_advertisements

logger = logging.getLogger("scanner-ubertooth")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


DB_PATH = _env("DB_PATH", "/data/detections.sqlite3")
GPSD_HOST = _env("GPSD_HOST", "127.0.0.1")
GPSD_PORT = int(_env("GPSD_PORT", "2947"))
SOURCE_UNIT_ID = _env("SOURCE_UNIT_ID", "ground-logger-ubertooth")
# Only needed with more than one Ubertooth attached to the same host.
_device_index_raw = os.environ.get("UBERTOOTH_DEVICE_INDEX")
UBERTOOTH_DEVICE_INDEX = int(_device_index_raw) if _device_index_raw else None
LOG_LEVEL = _env("LOG_LEVEL", "INFO")

STALE_FIX_WARN_S = 30.0


def _adv_data_json(adv: ParsedAdvertisement) -> str:
    return json.dumps(
        {
            "manufacturer_data": adv.manufacturer_data,
            "service_data": adv.service_data,
            "service_uuids": adv.service_uuids,
        }
    )


async def run() -> None:
    logging.basicConfig(
        level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger.info(
        "starting ubertooth scanner: db=%s gpsd=%s:%s device_index=%s unit=%s",
        DB_PATH,
        GPSD_HOST,
        GPSD_PORT,
        UBERTOOTH_DEVICE_INDEX if UBERTOOTH_DEVICE_INDEX is not None else "(default)",
        SOURCE_UNIT_ID,
    )

    storage = Storage(DB_PATH)
    gps = GpsClient(GPSD_HOST, GPSD_PORT)
    gps_task = asyncio.create_task(gps.run())

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    async def consume() -> None:
        async for adv in stream_advertisements(UBERTOOTH_DEVICE_INDEX):
            fix = gps.latest_fix()
            fix_age = time.monotonic() - fix.received_at if fix else None
            if fix_age is not None and fix_age > STALE_FIX_WARN_S:
                logger.warning("GPS fix is %.0fs old", fix_age)

            detection = Detection(
                timestamp_utc=datetime.now(UTC).isoformat(),
                source_unit_id=SOURCE_UNIT_ID,
                mac=adv.mac,
                device_name=adv.device_name,
                rssi_dbm=adv.rssi_dbm,
                tx_power_dbm=adv.tx_power_dbm,
                adv_data_json=_adv_data_json(adv),
                lat=fix.lat if fix else None,
                lon=fix.lon if fix else None,
                alt_m=fix.alt_m if fix else None,
                gps_fix_age_s=fix_age,
            )
            storage.insert_detection(detection)
            logger.debug(
                "%s rssi=%s name=%r lat=%s lon=%s",
                detection.mac,
                detection.rssi_dbm,
                detection.device_name,
                detection.lat,
                detection.lon,
            )

    consume_task = asyncio.create_task(consume())
    logger.info("scanning...")
    await stop_event.wait()

    logger.info("shutting down")
    consume_task.cancel()
    gps_task.cancel()
    storage.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
