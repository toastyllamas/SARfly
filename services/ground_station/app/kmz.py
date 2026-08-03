"""Build a KMZ (zipped KML) of current device positions for handoff to
Google Earth / ATAK / other GIS tools the ground team already carries.
"""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape

# KML color format is aabbggrr.
STATUS_COLORS = {
    "known": "ff00ff00",  # green
    "unknown": "ff0000ff",  # red -- draws the eye to undecided/new devices
    "ignore": "ff888888",  # gray
}


def _placemark(device: dict) -> str:
    lat = device.get("lat")
    lon = device.get("lon")
    if lat is None or lon is None:
        return ""

    mac = device["mac"]
    name = escape(device.get("device_name") or mac)
    status = device.get("status", "unknown")
    color = STATUS_COLORS.get(status, STATUS_COLORS["unknown"])
    vendor_line = ""
    if device.get("vendor_guess"):
        vendor_line = f"Possible vendor: {device['vendor_guess']} ({device.get('vendor_confidence')} confidence)<br/>"
    description_html = (
        f"MAC: {mac}<br/>Status: {status}<br/>Label: {device.get('label') or ''}<br/>"
        f"{vendor_line}"
        f"RSSI: {device.get('rssi_dbm')} dBm<br/>"
        f"First seen: {device.get('first_seen') or ''}<br/>"
        f"Last seen: {device.get('last_seen') or ''}<br/>"
        f"Detections: {device.get('count')}"
    )
    return f"""
    <Placemark>
      <name>{name}</name>
      <description><![CDATA[{description_html}]]></description>
      <Style><IconStyle><color>{color}</color></IconStyle></Style>
      <Point><coordinates>{lon},{lat},0</coordinates></Point>
    </Placemark>"""


def build_kmz(devices: list[dict]) -> bytes:
    placemarks = "\n".join(_placemark(d) for d in devices)
    kml = f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>BLE SAR Detections</name>
    {placemarks}
  </Document>
</kml>"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", kml)
    return buf.getvalue()
