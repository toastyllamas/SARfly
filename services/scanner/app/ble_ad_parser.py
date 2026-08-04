"""Parser for raw BLE advertising data (AD structure) bytes.

ubertooth-btle prints decoded packets as human-readable debug text rather
than structured fields, but it does include the raw `AdvData:` hex blob --
which is exactly the AD structure bytes defined in Bluetooth Core Spec Vol 3,
Part C, Section 11. Parsing that ourselves (rather than scraping Ubertooth's
pretty-printed sub-lines) gives the same fields bleak's AdvertisementData
already provides for the BlueZ/UD100 path, so detections from either source
carry identical shape into storage.py/vendor_id.py.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

_BASE_UUID_SUFFIX = "-0000-1000-8000-00805f9b34fb"

# AD type codes we care about; see Bluetooth SIG "Generic Access Profile"
# assigned numbers for the full list.
_TYPE_SHORT_NAME = 0x08
_TYPE_COMPLETE_NAME = 0x09
_TYPE_TX_POWER = 0x0A
_TYPE_UUID16_INCOMPLETE = 0x02
_TYPE_UUID16_COMPLETE = 0x03
_TYPE_UUID128_INCOMPLETE = 0x06
_TYPE_UUID128_COMPLETE = 0x07
_TYPE_SERVICE_DATA_16 = 0x16
_TYPE_MANUFACTURER_DATA = 0xFF


@dataclass
class ParsedAdvData:
    device_name: str | None = None
    tx_power_dbm: int | None = None
    manufacturer_data: dict[str, str] = field(default_factory=dict)
    service_data: dict[str, str] = field(default_factory=dict)
    service_uuids: list[str] = field(default_factory=list)


def _uuid16_to_base(uuid16: int) -> str:
    return f"0000{uuid16:04x}{_BASE_UUID_SUFFIX}"


def _uuid128_bytes_to_str(raw: bytes) -> str:
    # AD structures store 128-bit UUIDs little-endian; standard UUID string
    # form is big-endian, so reverse before formatting.
    b = raw[::-1].hex()
    return f"{b[0:8]}-{b[8:12]}-{b[12:16]}-{b[16:20]}-{b[20:32]}"


def parse_ad_structure(adv_data_hex: str) -> ParsedAdvData:
    """Parse a space-or-contiguous hex string of AD structures.

    Malformed/truncated trailing bytes are ignored rather than raising --
    this runs on live-air captures, not a trusted/validated source.
    """
    result = ParsedAdvData()
    try:
        raw = bytes.fromhex(adv_data_hex.replace(" ", ""))
    except ValueError:
        return result

    i = 0
    n = len(raw)
    while i < n:
        length = raw[i]
        if length == 0:
            break
        end = i + 1 + length
        if end > n:
            break
        ad_type = raw[i + 1]
        value = raw[i + 2 : end]

        if ad_type in (_TYPE_SHORT_NAME, _TYPE_COMPLETE_NAME):
            result.device_name = value.decode("utf-8", errors="replace")
        elif ad_type == _TYPE_TX_POWER and len(value) >= 1:
            result.tx_power_dbm = struct.unpack("b", value[:1])[0]
        elif ad_type == _TYPE_MANUFACTURER_DATA and len(value) >= 2:
            company_id = int.from_bytes(value[:2], "little")
            result.manufacturer_data[str(company_id)] = value[2:].hex()
        elif ad_type in (_TYPE_UUID16_INCOMPLETE, _TYPE_UUID16_COMPLETE):
            for j in range(0, len(value) - 1, 2):
                uuid16 = int.from_bytes(value[j : j + 2], "little")
                result.service_uuids.append(_uuid16_to_base(uuid16))
        elif ad_type in (_TYPE_UUID128_INCOMPLETE, _TYPE_UUID128_COMPLETE):
            for j in range(0, len(value) - 15, 16):
                result.service_uuids.append(_uuid128_bytes_to_str(value[j : j + 16]))
        elif ad_type == _TYPE_SERVICE_DATA_16 and len(value) >= 2:
            uuid16 = int.from_bytes(value[:2], "little")
            result.service_data[_uuid16_to_base(uuid16)] = value[2:].hex()

        i = end

    return result
