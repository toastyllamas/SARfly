"""Advertisement source backed by an Ubertooth One instead of a BlueZ/HCI
adapter.

Ubertooth doesn't speak HCI and never shows up under /sys/class/bluetooth --
BlueZ has no way to drive it, so this does not go through bleak at all.
Instead it runs `ubertooth-btle -n` (advertisements-only, no connection
following) as a subprocess and parses its debug text output directly.

Output is one record per received packet. A new record always starts with a
`systime=...` line; everything up to (but not including) the next such line
belongs to it. This is more robust than splitting on blank lines, since a
single record can contain internal blank lines between its nested AD-type
breakdown and its trailing Data:/CRC: lines.

The "(valid)"/"(invalid)" tag on ubertooth-btle's "Advertising / AA ..."
line is *not* a CRC check -- it only reflects whether the access-address bit
pattern matched (confirmed against libbtbb's source: `access_address_ok`,
nothing else). Nothing in this capture path verifies CRC-24 on its own
(`-v1` doesn't affect it either), so bit-corrupted payloads -- garbled
device names, corrupted manufacturer data -- pass through it unfiltered.
ble_crc.py ports the real CRC-24 algorithm from the Ubertooth firmware
source and is used below as the actual validity gate, against the packet's
raw on-air bytes (the unlabeled hex dump line ubertooth-btle prints ahead of
its own decode).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ble_ad_parser import parse_ad_structure
from ble_crc import crc_ok

logger = logging.getLogger(__name__)

_HEADER_RE = re.compile(
    r"^systime=\d+ freq=\d+ addr=[0-9a-f]+ delta_t=[\d.]+ ms rssi=(-?\d+)"
)
_RAWHEX_RE = re.compile(r"^([0-9a-f]{2} )+[0-9a-f]{2}\s*$")
_ADVA_RE = re.compile(r"^\s*AdvA:\s+([0-9a-f:]+) \((public|random)\)")
_ADVDATA_RE = re.compile(r"^\s*AdvData:\s+([0-9a-f ]+?)\s*$")


@dataclass
class ParsedAdvertisement:
    mac: str
    rssi_dbm: int
    device_name: str | None
    tx_power_dbm: int | None
    manufacturer_data: dict[str, str]
    service_data: dict[str, str]
    service_uuids: list[str]


def _parse_record(lines: list[str]) -> ParsedAdvertisement | None:
    rssi: int | None = None
    raw_hex: str | None = None
    mac: str | None = None
    adv_data_hex: str | None = None

    for line in lines:
        if (m := _HEADER_RE.match(line)) is not None:
            rssi = int(m.group(1))
        elif raw_hex is None and _RAWHEX_RE.match(line):
            raw_hex = line.strip()
        elif (m := _ADVA_RE.match(line)) is not None:
            mac = m.group(1)
        elif (m := _ADVDATA_RE.match(line)) is not None:
            adv_data_hex = m.group(1)

    if rssi is None or mac is None or raw_hex is None:
        return None

    # The real validity gate -- see module docstring for why the tool's own
    # "(valid)" tag isn't this. raw_hex is [2-byte PDU header][6-byte
    # AdvA][AdvData][3-byte CRC] exactly as transmitted.
    raw = bytes.fromhex(raw_hex.replace(" ", ""))
    if len(raw) < 3 or not crc_ok(raw[:-3], raw[-3:]):
        return None

    ad = parse_ad_structure(adv_data_hex) if adv_data_hex else None
    return ParsedAdvertisement(
        mac=mac,
        rssi_dbm=rssi,
        device_name=ad.device_name if ad else None,
        tx_power_dbm=ad.tx_power_dbm if ad else None,
        manufacturer_data=ad.manufacturer_data if ad else {},
        service_data=ad.service_data if ad else {},
        service_uuids=ad.service_uuids if ad else [],
    )


async def stream_advertisements(
    device_index: int | None = None,
) -> AsyncIterator[ParsedAdvertisement]:
    """Run ubertooth-btle and yield one ParsedAdvertisement per valid packet.

    Runs until cancelled. If the subprocess dies (e.g. device unplugged),
    logs a warning, waits, and restarts it -- same reconnect philosophy as
    GpsClient.run().
    """
    args = ["ubertooth-btle", "-n"]
    if device_index is not None:
        args.append(f"-U{device_index}")

    while True:
        logger.info("starting: %s", " ".join(args))
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("ubertooth-btle not found on PATH; is it installed?")
            await asyncio.sleep(5)
            continue

        stderr_task = asyncio.create_task(_log_stderr(proc.stderr))
        buf: list[str] = []
        try:
            assert proc.stdout is not None
            async for raw_line in proc.stdout:
                line = raw_line.decode(errors="replace").rstrip("\n")
                if _HEADER_RE.match(line) and buf:
                    if (rec := _parse_record(buf)) is not None:
                        yield rec
                    buf = []
                buf.append(line)
            if (rec := _parse_record(buf)) is not None:
                yield rec
        finally:
            stderr_task.cancel()
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()

        logger.warning("ubertooth-btle exited (code %s); restarting in 5s", proc.returncode)
        await asyncio.sleep(5)


async def _log_stderr(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    async for raw_line in stream:
        logger.warning("ubertooth-btle: %s", raw_line.decode(errors="replace").rstrip("\n"))
