"""Resolve which network interface is a specific USB WiFi adapter.

Linux assigns wlanN indices in USB probe order at boot, which is not stable
across reboots or replugs. Rather than hardcoding an index that can silently
point at the wrong radio after a reboot, find the adapter by its USB
vendor/product ID instead -- same approach as the BLE scanner's adapter.py,
just walking /sys/class/net instead of /sys/class/bluetooth.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

NET_CLASS_DIR = "/sys/class/net"


def _read_id(device_dir: str, filename: str) -> str | None:
    path = os.path.join(device_dir, filename)
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def find_wifi_adapter_by_usb_id(vendor_id: str, product_id: str) -> str | None:
    """Return the network interface name (e.g. "wlan1") whose USB
    idVendor/idProduct match, or None if not found (e.g. running without
    /sys, or adapter unplugged).
    """
    try:
        iface_names = sorted(os.listdir(NET_CLASS_DIR))
    except OSError as exc:
        logger.warning("cannot list %s: %s", NET_CLASS_DIR, exc)
        return None

    for iface_name in iface_names:
        device_link = os.path.join(NET_CLASS_DIR, iface_name, "device")
        if not os.path.islink(device_link):
            continue
        # device_link points at the USB *interface* node (e.g. .../1-1.2/1-1.2:1.0);
        # idVendor/idProduct live one level up, on the USB device node itself.
        interface_dir = os.path.realpath(device_link)
        usb_device_dir = os.path.dirname(interface_dir)
        vid = _read_id(usb_device_dir, "idVendor")
        pid = _read_id(usb_device_dir, "idProduct")
        if vid == vendor_id.lower() and pid == product_id.lower():
            return iface_name

    return None
