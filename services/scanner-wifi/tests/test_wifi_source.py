import asyncio
import os

import pytest
from scapy.layers.dot11 import (
    Dot11,
    Dot11Beacon,
    Dot11Elt,
    Dot11ProbeReq,
    RadioTap,
)

from adapter import find_wifi_adapter_by_usb_id
from wifi_source import StationFrame, parse_frame, stream_frames


def _dot11(subtype: int, addr2: str = "aa:bb:cc:dd:ee:ff") -> Dot11:
    return Dot11(
        type=0,
        subtype=subtype,
        addr1="ff:ff:ff:ff:ff:ff",
        addr2=addr2,
        addr3="ff:ff:ff:ff:ff:ff",
    )


def _radiotap(rssi: int | None) -> RadioTap:
    if rssi is None:
        return RadioTap()
    return RadioTap(present="dBm_AntSignal", dBm_AntSignal=rssi)


def _roundtrip(pkt):
    """Real captures come off the wire as bytes -- round-tripping through
    bytes here (rather than using the in-memory-constructed layers
    directly) catches any assumption that only holds for freshly-built
    scapy objects and not for parsed ones.
    """
    return pkt.__class__(bytes(pkt))


def _probe_req(ssid: bytes | None = b"TestNet", rssi: int | None = -55, addr2="aa:bb:cc:dd:ee:ff"):
    pkt = _radiotap(rssi) / _dot11(subtype=4, addr2=addr2) / Dot11ProbeReq()
    if ssid is not None:
        pkt = pkt / Dot11Elt(ID=0, info=ssid)
    return _roundtrip(pkt)


def _beacon(ssid: bytes | None = b"SomeAP", rssi: int | None = -60, addr2="11:22:33:44:55:66"):
    pkt = _radiotap(rssi) / _dot11(subtype=8, addr2=addr2) / Dot11Beacon()
    if ssid is not None:
        pkt = pkt / Dot11Elt(ID=0, info=ssid)
    return _roundtrip(pkt)


def test_parse_probe_request_with_ssid():
    frame = parse_frame(_probe_req(ssid=b"HomeWifi", rssi=-42))
    assert frame == StationFrame(
        mac="aa:bb:cc:dd:ee:ff", rssi_dbm=-42, frame_type="probe_req", ssid="HomeWifi"
    )


def test_parse_probe_request_wildcard_ssid_is_none():
    frame = parse_frame(_probe_req(ssid=b""))
    assert frame is not None
    assert frame.ssid is None


def test_parse_beacon_with_ssid():
    frame = parse_frame(_beacon(ssid="CoffeeShop".encode(), rssi=-70))
    assert frame == StationFrame(
        mac="11:22:33:44:55:66", rssi_dbm=-70, frame_type="beacon", ssid="CoffeeShop"
    )


def test_parse_ignores_frames_that_are_not_probe_or_beacon():
    # subtype 11 = authentication -- a real frame type, just not one this
    # scanner cares about.
    pkt = _roundtrip(_radiotap(-50) / _dot11(subtype=11))
    assert parse_frame(pkt) is None


def test_parse_drops_frame_with_no_radiotap_layer():
    pkt = _dot11(subtype=4) / Dot11ProbeReq() / Dot11Elt(ID=0, info=b"x")
    assert parse_frame(pkt) is None


def test_parse_drops_frame_with_radiotap_but_no_signal_reading():
    """A required NOT NULL column (rssi_dbm) must never be filled with a
    fabricated value -- if radiotap didn't attach a signal reading, the
    frame is dropped rather than logged with a fake rssi_dbm=0.
    """
    frame = parse_frame(_probe_req(rssi=None))
    assert frame is None


def test_extract_ssid_decodes_non_utf8_bytes_without_raising():
    frame = parse_frame(_probe_req(ssid=b"\xff\xfe\x00bad-utf8"))
    assert frame is not None  # must not raise -- garbled SSID beats a crash


def test_find_wifi_adapter_by_usb_id(tmp_path, monkeypatch):
    net_dir = tmp_path / "class" / "net"
    usb_iface_dir = tmp_path / "devices" / "usb1" / "1-1" / "1-1.2" / "1-1.2:1.0"
    usb_iface_dir.mkdir(parents=True)
    usb_device_dir = usb_iface_dir.parent
    (usb_device_dir / "idVendor").write_text("0e8d\n")
    (usb_device_dir / "idProduct").write_text("7610\n")

    (net_dir / "wlan1").mkdir(parents=True)
    (net_dir / "wlan1" / "device").symlink_to(usb_iface_dir)
    (net_dir / "eth0").mkdir(parents=True)  # a non-matching interface, no crash expected

    import adapter

    monkeypatch.setattr(adapter, "NET_CLASS_DIR", str(net_dir))
    assert find_wifi_adapter_by_usb_id("0e8d", "7610") == "wlan1"
    assert find_wifi_adapter_by_usb_id("dead", "beef") is None


def test_find_wifi_adapter_by_usb_id_missing_sysfs_returns_none(monkeypatch):
    import adapter

    monkeypatch.setattr(adapter, "NET_CLASS_DIR", "/nonexistent/path")
    assert find_wifi_adapter_by_usb_id("0e8d", "7610") is None


def test_stream_frames_sets_monitor_mode_once_and_hops_channels(monkeypatch):
    import wifi_source

    calls = {"monitor": 0, "channels": []}

    def fake_set_monitor_mode(iface):
        calls["monitor"] += 1

    def fake_set_channel(iface, channel):
        calls["channels"].append(channel)

    def fake_sniff_one_dwell(iface, dwell_s):
        # One relevant frame on the first channel visited, nothing after.
        if len(calls["channels"]) == 1:
            return [_probe_req()]
        return []

    monkeypatch.setattr(wifi_source, "set_monitor_mode", fake_set_monitor_mode)
    monkeypatch.setattr(wifi_source, "set_channel", fake_set_channel)
    monkeypatch.setattr(wifi_source, "_sniff_one_dwell", fake_sniff_one_dwell)

    async def collect_one():
        async for frame in stream_frames("wlan1", channels=[1, 6, 11], dwell_s=0.01):
            return frame
        return None

    frame = asyncio.run(asyncio.wait_for(collect_one(), timeout=5.0))
    assert calls["monitor"] == 1
    assert frame.ssid == "TestNet"
    # Only channel 1 needed to be visited to find the one queued frame.
    assert calls["channels"][0] == 1


def test_stream_frames_propagates_monitor_mode_setup_failure(monkeypatch):
    """If the adapter was unplugged, monitor-mode setup fails -- this must
    raise (not silently retry forever inside stream_frames) so the caller
    can re-resolve the interface by USB id, the same way main.py handles a
    replugged BLE adapter potentially renaming.
    """
    import wifi_source

    def fake_set_monitor_mode(iface):
        raise OSError("no such device")

    monkeypatch.setattr(wifi_source, "set_monitor_mode", fake_set_monitor_mode)

    async def collect_one():
        async for _ in stream_frames("wlan1", channels=[1], dwell_s=0.01):
            pass

    with pytest.raises(OSError):
        asyncio.run(asyncio.wait_for(collect_one(), timeout=5.0))


def test_stream_frames_ignores_non_dot11_and_unparseable_frames(monkeypatch):
    import wifi_source

    monkeypatch.setattr(wifi_source, "set_monitor_mode", lambda iface: None)
    monkeypatch.setattr(wifi_source, "set_channel", lambda iface, ch: None)

    call_count = {"n": 0}

    def fake_sniff_one_dwell(iface, dwell_s):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # A frame with no RadioTap/Dot11 at all, plus one real hit.
            from scapy.packet import Raw

            return [Raw(b"not a wifi frame"), _beacon()]
        raise asyncio.CancelledError  # stop the infinite hop loop cleanly

    monkeypatch.setattr(wifi_source, "_sniff_one_dwell", fake_sniff_one_dwell)

    async def collect_one():
        async for frame in stream_frames("wlan1", channels=[1], dwell_s=0.01):
            return frame
        return None

    frame = asyncio.run(asyncio.wait_for(collect_one(), timeout=5.0))
    assert frame.frame_type == "beacon"
