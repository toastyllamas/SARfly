"""Ground/wide-area BLE detection logger.

Scans continuously for BLE advertisements (via BlueZ, over the Sena UD100
or any other adapter BlueZ recognizes), tags each detection with the most
recent GPS fix, and appends it to a local SQLite log.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from datetime import UTC, datetime

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from adapter import find_adapter_by_usb_id
from gps_client import GpsClient
from storage import Detection, Storage, adv_data_to_json

logger = logging.getLogger("scanner")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


DB_PATH = _env("DB_PATH", "/data/detections.sqlite3")
GPSD_HOST = _env("GPSD_HOST", "127.0.0.1")
GPSD_PORT = int(_env("GPSD_PORT", "2947"))
SOURCE_UNIT_ID = _env("SOURCE_UNIT_ID", "ground-logger")
# Explicit override, e.g. "hci0". Usually left unset -- the hci index for a
# USB adapter isn't stable across reboots/replugs, so by default we resolve
# the adapter by USB vendor/product ID instead (see BLE_ADAPTER_USB_VID/PID).
BLE_ADAPTER = os.environ.get("BLE_ADAPTER")
# Defaults to the Sena UD100 (CSR8510 chipset).
BLE_ADAPTER_USB_VID = _env("BLE_ADAPTER_USB_VID", "0a12")
BLE_ADAPTER_USB_PID = _env("BLE_ADAPTER_USB_PID", "0001")
LOG_LEVEL = _env("LOG_LEVEL", "INFO")

# GPS fixes older than this are logged but flagged via gps_fix_age_s rather
# than silently treated as current -- useful if gpsd loses satellite lock
# while the unit keeps scanning.
STALE_FIX_WARN_S = 30.0


def make_detection_handler(storage: Storage, gps: GpsClient):
    def on_detection(device: BLEDevice, adv: AdvertisementData) -> None:
        fix = gps.latest_fix()
        fix_age = time.monotonic() - fix.received_at if fix else None
        if fix_age is not None and fix_age > STALE_FIX_WARN_S:
            logger.warning("GPS fix is %.0fs old", fix_age)

        detection = Detection(
            timestamp_utc=datetime.now(UTC).isoformat(),
            source_unit_id=SOURCE_UNIT_ID,
            mac=device.address,
            device_name=adv.local_name or device.name,
            rssi_dbm=adv.rssi,
            tx_power_dbm=adv.tx_power,
            adv_data_json=adv_data_to_json(
                adv.manufacturer_data, adv.service_data, adv.service_uuids
            ),
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

    return on_detection


async def run() -> None:
    logging.basicConfig(
        level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger.info(
        "starting scanner: db=%s gpsd=%s:%s adapter_override=%s unit=%s",
        DB_PATH,
        GPSD_HOST,
        GPSD_PORT,
        BLE_ADAPTER or "(auto)",
        SOURCE_UNIT_ID,
    )

    storage = Storage(DB_PATH)
    gps = GpsClient(GPSD_HOST, GPSD_PORT)
    gps_task = asyncio.create_task(gps.run())

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    adapter = BLE_ADAPTER or find_adapter_by_usb_id(
        BLE_ADAPTER_USB_VID, BLE_ADAPTER_USB_PID
    )
    scanner_kwargs = {"detection_callback": make_detection_handler(storage, gps)}
    if adapter:
        logger.info("using BLE adapter %s", adapter)
        scanner_kwargs["bluez"] = {"adapter": adapter}
    else:
        logger.warning(
            "could not resolve adapter by USB id %s:%s; falling back to bleak's default adapter",
            BLE_ADAPTER_USB_VID,
            BLE_ADAPTER_USB_PID,
        )

    scanner = BleakScanner(**scanner_kwargs)
    async with scanner:
        logger.info("scanning...")
        await stop_event.wait()

    logger.info("shutting down")
    gps_task.cancel()
    storage.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
