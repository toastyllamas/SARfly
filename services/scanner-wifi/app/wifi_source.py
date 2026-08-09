"""802.11 monitor-mode capture: probe requests and beacons from nearby
stations/APs.

Treated the same way as the BLE/spectrum scanners -- an ephemeral RF
presence signal at a GPS position and time, not a trackable device
identity. Modern phones (iOS 8+, Android 6-10+) randomize their MAC address
for probe requests whenever they aren't associated to a network, so a MAC
seen here should not be treated as a stable long-term identifier for a
specific physical device -- it will typically change between scan sessions.
Beacon frames are less affected (an access point's BSSID is its persistent
identity), which is the main reason to capture both frame types rather than
just probe requests: a beacon-emitting device (e.g. a phone in hotspot
mode) is identifiable in a way a probe-requesting one usually isn't anymore.

Uses scapy for both capture and 802.11 dissection -- unlike hackrf_sweep/
rtl_power/ubertooth-btle, there's no external CLI tool to subprocess-wrap
here; scapy drives the monitor-mode socket directly. Channel switching and
putting the interface into monitor mode go through `iw`/`ip` subprocess
calls instead, since scapy has no cross-driver way to do either itself.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass

from scapy.layers.dot11 import Dot11, Dot11Beacon, Dot11Elt, Dot11ProbeReq
from scapy.layers.dot11 import RadioTap
from scapy.sendrecv import sniff

logger = logging.getLogger(__name__)

# This adapter's radio (MediaTek MT7610U) only supports 2.4GHz, channels
# 1-11 in the US regulatory domain (12-14 come back "disabled" from `iw phy`
# on this hardware) -- confirmed against the real device, not assumed.
# Hopping all 11 rather than just the non-overlapping 1/6/11 trio trades
# sweep speed for coverage: a SAR search shouldn't skip channels a
# non-standard AP/IoT device might be sitting on.
DEFAULT_CHANNELS: list[int] = list(range(1, 12))
DEFAULT_DWELL_S = 2.0


def set_monitor_mode(iface: str) -> None:
    subprocess.run(["ip", "link", "set", iface, "down"], check=True, capture_output=True)
    subprocess.run(
        ["iw", "dev", iface, "set", "type", "monitor"], check=True, capture_output=True
    )
    subprocess.run(["ip", "link", "set", iface, "up"], check=True, capture_output=True)


def set_channel(iface: str, channel: int) -> None:
    subprocess.run(
        ["iw", "dev", iface, "set", "channel", str(channel)], check=True, capture_output=True
    )


@dataclass
class StationFrame:
    mac: str
    rssi_dbm: int
    frame_type: str  # "probe_req" | "beacon"
    ssid: str | None


def _extract_ssid(pkt) -> str | None:
    elt = pkt.getlayer(Dot11Elt)
    while elt is not None:
        if elt.ID == 0:  # SSID information element
            raw = bytes(elt.info)
            if not raw:
                return None  # wildcard probe / hidden-SSID beacon
            return raw.decode("utf-8", errors="replace")
        elt = elt.payload.getlayer(Dot11Elt)
    return None


def parse_frame(pkt) -> StationFrame | None:
    """Return a StationFrame for a probe request or beacon with a usable
    source MAC and signal strength, or None for anything else (including
    frames radiotap couldn't attach a signal reading to -- rssi_dbm is a
    required column, so those are dropped here rather than inserted as 0).
    """
    if pkt.haslayer(Dot11ProbeReq):
        frame_type = "probe_req"
    elif pkt.haslayer(Dot11Beacon):
        frame_type = "beacon"
    else:
        return None

    mac = pkt[Dot11].addr2
    if mac is None:
        return None

    if not pkt.haslayer(RadioTap):
        return None
    rssi = getattr(pkt[RadioTap], "dBm_AntSignal", None)
    if rssi is None:
        return None

    return StationFrame(mac=mac, rssi_dbm=int(rssi), frame_type=frame_type, ssid=_extract_ssid(pkt))


def _sniff_one_dwell(iface: str, dwell_s: float) -> list:
    return sniff(iface=iface, timeout=dwell_s, store=True)


async def stream_frames(
    iface: str,
    channels: list[int] | None = None,
    dwell_s: float = DEFAULT_DWELL_S,
) -> AsyncIterator[StationFrame]:
    """Put iface into monitor mode and yield one StationFrame per relevant
    802.11 frame seen while hopping across `channels`.

    Runs a single continuous session: sets monitor mode once up front, then
    channel-hops forever. Raises (does not retry internally) if monitor-mode
    setup or a channel switch fails -- e.g. the adapter was unplugged -- so
    the caller can re-resolve the interface by USB id and start a fresh
    session, the same way main.py handles the BLE adapter potentially
    renaming across a replug. sniff() itself runs in a thread via
    run_in_executor since it's a blocking call.
    """
    channels = channels or DEFAULT_CHANNELS
    set_monitor_mode(iface)
    logger.info("monitor mode enabled on %s, hopping channels %s", iface, channels)

    loop = asyncio.get_running_loop()
    while True:
        for channel in channels:
            set_channel(iface, channel)
            packets = await loop.run_in_executor(None, _sniff_one_dwell, iface, dwell_s)
            for pkt in packets:
                if pkt.haslayer(Dot11):
                    parsed = parse_frame(pkt)
                    if parsed is not None:
                        yield parsed
