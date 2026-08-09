"""Ground/wide-area 802.11 station scanner.

Same job as scanner/main.py (log GPS-tagged RF detections to the shared
SQLite detections log), sourced from a monitor-mode-capable USB WiFi
adapter instead of a Bluetooth radio -- see wifi_source.py for capture
details and why MACs seen here aren't a reliable long-term device identity.
Meant to run alongside the BLE/spectrum scanners, tagged with its own
SOURCE_UNIT_ID, not instead of them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import UTC, datetime

from adapter import find_wifi_adapter_by_usb_id
from gps_client import GpsClient
from storage import Detection, Storage
from wifi_source import DEFAULT_CHANNELS, DEFAULT_DWELL_S, StationFrame, stream_frames

logger = logging.getLogger("scanner-wifi")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


DB_PATH = _env("DB_PATH", "/data/detections.sqlite3")
GPSD_HOST = _env("GPSD_HOST", "127.0.0.1")
GPSD_PORT = int(_env("GPSD_PORT", "2947"))
SOURCE_UNIT_ID = _env("SOURCE_UNIT_ID", "ground-logger-wifi-01")
# Explicit override, e.g. "wlan1". Usually left unset -- the wlanN index for
# a USB adapter isn't stable across reboots/replugs, so by default we
# resolve it by USB vendor/product ID instead (see WIFI_ADAPTER_USB_VID/PID).
WIFI_IFACE = os.environ.get("WIFI_IFACE")
# Defaults to a MediaTek MT7610U-based dongle (the one validated for this
# component).
WIFI_ADAPTER_USB_VID = _env("WIFI_ADAPTER_USB_VID", "0e8d")
WIFI_ADAPTER_USB_PID = _env("WIFI_ADAPTER_USB_PID", "7610")
WIFI_DWELL_S = float(_env("WIFI_DWELL_S", str(DEFAULT_DWELL_S)))
LOG_LEVEL = _env("LOG_LEVEL", "INFO")

STALE_FIX_WARN_S = 30.0


def _adv_data_json(frame: StationFrame) -> str:
    return json.dumps({"frame_type": frame.frame_type, "ssid": frame.ssid})


async def run() -> None:
    logging.basicConfig(
        level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger.info(
        "starting wifi scanner: db=%s gpsd=%s:%s adapter_override=%s unit=%s",
        DB_PATH,
        GPSD_HOST,
        GPSD_PORT,
        WIFI_IFACE or "(auto)",
        SOURCE_UNIT_ID,
    )

    storage = Storage(DB_PATH)
    gps = GpsClient(GPSD_HOST, GPSD_PORT)
    gps_task = asyncio.create_task(gps.run())

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    # Retries on any failure -- adapter not found, monitor-mode setup
    # failed, or the capture session raised partway through (e.g. the
    # adapter was unplugged) -- rather than letting the process crash and
    # depending on Docker's restart policy. Re-resolves the interface by
    # USB id on every pass since a replug can change wlanN, same reasoning
    # as the primary BLE scanner's find_adapter_by_usb_id use in main.py.
    while not stop_event.is_set():
        iface = WIFI_IFACE or find_wifi_adapter_by_usb_id(
            WIFI_ADAPTER_USB_VID, WIFI_ADAPTER_USB_PID
        )
        if not iface:
            logger.warning(
                "could not resolve wifi adapter by USB id %s:%s; retrying in 5s",
                WIFI_ADAPTER_USB_VID,
                WIFI_ADAPTER_USB_PID,
            )
            await asyncio.sleep(5)
            continue

        logger.info("using wifi adapter %s", iface)
        try:
            async for frame in stream_frames(iface, DEFAULT_CHANNELS, WIFI_DWELL_S):
                fix = gps.latest_fix()
                fix_age = time.monotonic() - fix.received_at if fix else None
                if fix_age is not None and fix_age > STALE_FIX_WARN_S:
                    logger.warning("GPS fix is %.0fs old", fix_age)

                detection = Detection(
                    timestamp_utc=datetime.now(UTC).isoformat(),
                    source_unit_id=SOURCE_UNIT_ID,
                    mac=frame.mac,
                    device_name=frame.ssid,
                    rssi_dbm=frame.rssi_dbm,
                    tx_power_dbm=None,
                    adv_data_json=_adv_data_json(frame),
                    lat=fix.lat if fix else None,
                    lon=fix.lon if fix else None,
                    alt_m=fix.alt_m if fix else None,
                    gps_fix_age_s=fix_age,
                )
                storage.insert_detection(detection)
                logger.debug(
                    "%s %s ssid=%r rssi=%s lat=%s lon=%s",
                    frame.frame_type,
                    frame.mac,
                    frame.ssid,
                    frame.rssi_dbm,
                    detection.lat,
                    detection.lon,
                )
                if stop_event.is_set():
                    break
        except Exception:
            logger.exception("wifi capture session failed; retrying in 5s")
            await asyncio.sleep(5)

    logger.info("shutting down")
    gps_task.cancel()
    storage.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
