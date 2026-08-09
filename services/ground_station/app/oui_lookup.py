"""IEEE OUI (MAC address prefix -> registered vendor) lookup.

Bundled as a static CSV (data/oui.csv, from IEEE's public MA-L registry)
rather than fetched live -- same offline-first philosophy as
vendor_id.py's hardcoded BLE fingerprints, just covering the generic
"whose hardware is this" case instead of a specific product-family match.

Meaningless for locally-administered (randomized) MAC addresses: modern
phones randomize their MAC for BLE while unpaired and for WiFi probe
requests (see README's WiFi scanner notes), and a randomized MAC's first
three octets were never actually assigned by IEEE to anyone. Looking one up
anyway risks a coincidentally-real-looking but meaningless vendor name, so
is_locally_administered() gates every lookup below.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_OUI_CSV_PATH = Path(__file__).parent / "data" / "oui.csv"


def is_locally_administered(mac: str) -> bool:
    """True if the U/L bit (bit 1 of the first octet) is set -- this MAC
    was locally assigned/randomized, not IEEE-allocated, so its prefix
    carries no vendor information.
    """
    first_octet = mac.split(":")[0].split("-")[0]
    return bool(int(first_octet, 16) & 0x02)


def _load_oui_table() -> dict[str, str]:
    table: dict[str, str] = {}
    try:
        with open(_OUI_CSV_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                table[row["oui_prefix"]] = row["vendor"]
    except OSError as exc:
        logger.warning("could not load OUI database from %s: %s", _OUI_CSV_PATH, exc)
    return table


_OUI_TABLE = _load_oui_table()


def lookup_oui(mac: str) -> str | None:
    """Return the IEEE-registered vendor name for mac's OUI prefix, or None
    if the MAC is locally administered (randomized) or its prefix isn't in
    the database.
    """
    if is_locally_administered(mac):
        return None
    prefix = mac.replace(":", "").replace("-", "").upper()[:6]
    return _OUI_TABLE.get(prefix)
