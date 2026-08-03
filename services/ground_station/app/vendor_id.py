"""Best-effort vendor fingerprinting from passively-captured BLE advertisement
data (no connecting/pairing -- just what's already in manufacturer_data,
service_uuids, and the device name of a detection).

This identifies device *type* ("this looks like a Garmin watch"), not
mission relevance -- a rescuer's own Garmin is indistinguishable from a
missing hiker's. Still tag/bulk-tag known devices as usual; this just turns
a cryptic MAC into an informed guess worth prioritizing as a lead.

Structured as a list of fingerprint rules so more vendors (Apple Watch,
Fitbit, Suunto, Coros, Polar, ...) can be added later without touching the
matching logic itself.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass
class VendorFingerprint:
    vendor: str
    company_ids: set[int]
    uuid_patterns: list[re.Pattern]
    name_patterns: list[re.Pattern]


def _uuid_pattern(prefix4hex: str) -> re.Pattern:
    return re.compile(rf"^{prefix4hex}[0-9a-f]{{4}}-667b-11e3-949a-0800200c9a66$", re.I)


def _name_pattern(word: str) -> re.Pattern:
    return re.compile(re.escape(word), re.I)


FINGERPRINTS: list[VendorFingerprint] = [
    VendorFingerprint(
        vendor="Garmin",
        # Bluetooth SIG company identifier for Garmin International, Inc.
        company_ids={135},  # 0x0087
        # Garmin's proprietary GATT service family (Multi-Link/GDI protocol,
        # Varia accessories, etc.) all share this 128-bit base UUID.
        uuid_patterns=[_uuid_pattern("6a4e")],
        # Known Garmin wearable/product line names. Weaker signal alone --
        # corroborating, not proof -- but cheap and useful when present.
        name_patterns=[
            _name_pattern(w)
            for w in (
                "forerunner", "fenix", "venu", "vivoactive", "vivosport",
                "vivomove", "vivosmart", "vivofit", "instinct", "epix",
                "quatix", "tactix", "marq", "enduro", "approach", "descent",
                "edge", "lily",
            )
        ]
        + [re.compile(r"\bd2\b", re.I)],
    ),
]


def classify(device_name: str | None, adv_data_json: str | None) -> dict | None:
    """Return {"vendor": ..., "confidence": "high"|"medium", "reason": ...}
    for the first matching fingerprint, or None if nothing matches.
    """
    manufacturer_ids: set[int] = set()
    service_uuids: list[str] = []
    if adv_data_json:
        try:
            adv = json.loads(adv_data_json)
        except (json.JSONDecodeError, TypeError):
            adv = {}
        for key in adv.get("manufacturer_data", {}):
            try:
                manufacturer_ids.add(int(key))
            except ValueError:
                pass
        service_uuids = adv.get("service_uuids", [])

    for fp in FINGERPRINTS:
        if manufacturer_ids & fp.company_ids:
            return {"vendor": fp.vendor, "confidence": "high", "reason": "manufacturer_id"}
        if any(p.match(u) for u in service_uuids for p in fp.uuid_patterns):
            return {"vendor": fp.vendor, "confidence": "high", "reason": "service_uuid"}
        if device_name and any(p.search(device_name) for p in fp.name_patterns):
            return {"vendor": fp.vendor, "confidence": "medium", "reason": "device_name"}

    return None
