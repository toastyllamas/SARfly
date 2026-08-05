"""Minimal asyncio client for gpsd's JSON protocol.

Connects to gpsd (running on the host, reachable over the loopback address
because the container uses host networking), issues a WATCH command, and
keeps the most recent position fix (TPV report) available for the scanner
to attach to detections. No third-party gpsd client library required.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Fix:
    lat: float
    lon: float
    alt_m: float | None
    received_at: float  # time.monotonic() when this fix was received


class GpsClient:
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._fix: Fix | None = None

    def latest_fix(self) -> Fix | None:
        return self._fix

    async def run(self) -> None:
        """Reconnect loop; call as a background asyncio task."""
        while True:
            try:
                await self._connect_and_read()
            except (ConnectionError, OSError) as exc:
                logger.warning("gpsd connection lost (%s); retrying in 5s", exc)
            await asyncio.sleep(5)

    async def _connect_and_read(self) -> None:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            writer.write(b'?WATCH={"enable":true,"json":true};\n')
            await writer.drain()
            logger.info("connected to gpsd at %s:%s", self._host, self._port)
            async for line in reader:
                self._handle_line(line)
        finally:
            writer.close()

    def _handle_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        if msg.get("class") != "TPV":
            return
        # mode: 0=no fix, 1=no fix, 2=2D fix, 3=3D fix
        if msg.get("mode", 0) < 2 or "lat" not in msg or "lon" not in msg:
            return
        self._fix = Fix(
            lat=msg["lat"],
            lon=msg["lon"],
            alt_m=msg.get("alt"),
            received_at=time.monotonic(),
        )
