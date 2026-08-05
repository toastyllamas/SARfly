# Component F: Multi-Band Spectrum Scanner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a HackRF-based drone-mounted energy scanner that sweeps cellular, 2.4GHz ISM, and sub-GHz keyfob bands, flags energy above a per-flight calibrated baseline, and shows the resulting GPS-tagged hits as a new toggleable layer on the existing ground-station map — additive to (not a replacement for) the BLE scanners.

**Architecture:** A new self-contained `services/scanner-spectrum/` service wraps the `hackrf_sweep` CLI as a subprocess (same pattern `services/scanner/app/ubertooth_source.py` uses for `ubertooth-btle`), does a one-time per-flight baseline calibration pass then sweeps bands forever, and writes GPS-tagged hits into a new `spectrum_hits` table in the same shared `detections.sqlite3`. The ground-station gets new read methods, a new API endpoint, a new WebSocket message type, and a new Leaflet layer colored by band.

**Tech Stack:** Python 3.12 (stdlib only for the new scanner), `hackrf_sweep` (built from source in the container, ships with the HackRF host tools), SQLite, FastAPI/WebSocket (existing ground-station), Leaflet.js (existing map).

**Full design reference:** `docs/superpowers/specs/2026-08-05-multiband-spectrum-scanner-design.md`

## Global Constraints

- Python 3.12-slim base image for the new service's final stage (matches `services/scanner/Dockerfile*`).
- No new third-party Python dependencies for `scanner-spectrum` — stdlib only (`asyncio`, `re`, `math`, `sqlite3`, `dataclasses`), matching the minimal-dependency style of `services/scanner/app/ubertooth_source.py`.
- Every env var is read via a local `_env(name, default)` helper, UPPER_SNAKE_CASE, and documented in README's Configuration section.
- Every SQLite connection uses `isolation_level=None` (autocommit) — omitting this previously caused a real bug (a connection's view of new rows froze after any write left an open transaction).
- Any subprocess-backed data source must retry forever every 5s on failure or a missing device rather than crash — this is what makes unattended field-SBC boot safe, matching `GpsClient.run()` / `ubertooth_source.stream_advertisements()`.
- New tables are created via `CREATE TABLE IF NOT EXISTS` by their writer service; the ground-station (a reader) does not create `spectrum_hits` itself, matching how it already relies on the primary scanner to create `detections`.
- Git commits end with:
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  ```

---

### Task 1: `spectrum_source.py` — pure parsing, band, and detection logic

**Files:**
- Create: `services/scanner-spectrum/app/spectrum_source.py`
- Create: `services/scanner-spectrum/tests/test_spectrum_source.py`
- Create: `services/scanner-spectrum/pytest.ini`

**Interfaces:**
- Produces: `Band` (frozen dataclass: `name: str, low_hz: int, high_hz: int`), `DEFAULT_BANDS: list[Band]`, `SpectrumHitReading` (dataclass: `band: str, freq_hz: int, power_dbm: float, baseline_dbm: float`), `parse_sweep_line(line: str) -> tuple[int, int, float, list[float]] | None`, `bin_center_freqs(hz_low: int, bin_width: float, count: int) -> list[int]`, `expand_readings(hz_low: int, bin_width: float, db_values: list[float]) -> list[tuple[int, float]]`, `_readings_from_line(line: str) -> list[tuple[int, float]]`, `band_for_freq(freq_hz: int, bands: list[Band] | None = None) -> str | None`, `average_power(powers: list[float]) -> float`, `detect_hits(readings: list[tuple[int, float]], band_name: str, baseline_dbm: float, margin_db: float) -> list[SpectrumHitReading]`

- [ ] **Step 1: Write the failing tests**

Create `services/scanner-spectrum/pytest.ini`:

```ini
[pytest]
pythonpath = app
```

Create `services/scanner-spectrum/tests/test_spectrum_source.py`:

```python
"""Tests for pure spectrum-sweep parsing/band/detection logic."""

from __future__ import annotations

from spectrum_source import (
    DEFAULT_BANDS,
    SpectrumHitReading,
    average_power,
    band_for_freq,
    bin_center_freqs,
    detect_hits,
    expand_readings,
    parse_sweep_line,
)


def test_parse_sweep_line_valid():
    line = "2026-08-05, 14:23:01.123456, 2400000000, 2405000000, 5000.00, 20, -54.32, -61.10, -58.44"
    assert parse_sweep_line(line) == (2400000000, 2405000000, 5000.0, [-54.32, -61.10, -58.44])


def test_parse_sweep_line_malformed_returns_none():
    assert parse_sweep_line("not a sweep line at all") is None


def test_parse_sweep_line_bad_db_values_returns_none():
    line = "2026-08-05, 14:23:01.123456, 2400000000, 2405000000, 5000.00, 20, notanumber"
    assert parse_sweep_line(line) is None


def test_bin_center_freqs():
    assert bin_center_freqs(2400000000, 1000.0, 3) == [2400000500, 2400001500, 2400002500]


def test_expand_readings():
    assert expand_readings(2400000000, 1000.0, [-50.0, -60.0]) == [
        (2400000500, -50.0),
        (2400001500, -60.0),
    ]


def test_readings_from_line_combines_parse_and_expand():
    from spectrum_source import _readings_from_line

    line = "2026-08-05, 14:23:01.123456, 2400000000, 2405000000, 1000.00, 2, -50.0, -60.0"
    assert _readings_from_line(line) == [(2400000500, -50.0), (2400001500, -60.0)]


def test_readings_from_line_malformed_returns_empty_list():
    from spectrum_source import _readings_from_line

    assert _readings_from_line("garbage") == []


def test_band_for_freq_matches_ism():
    assert band_for_freq(2450000000) == "ism_2_4ghz"


def test_band_for_freq_no_match_outside_any_default_band():
    assert band_for_freq(500000000) is None


def test_band_for_freq_boundary_is_inclusive_low_exclusive_high():
    ism = next(b for b in DEFAULT_BANDS if b.name == "ism_2_4ghz")
    assert band_for_freq(ism.low_hz) == "ism_2_4ghz"
    assert band_for_freq(ism.high_hz) != "ism_2_4ghz"


def test_average_power_empty_returns_zero():
    assert average_power([]) == 0.0


def test_average_power_computes_mean():
    assert average_power([-50.0, -60.0, -40.0]) == -50.0


def test_detect_hits_above_threshold():
    readings = [(2450000000, -40.0), (2451000000, -70.0)]
    hits = detect_hits(readings, "ism_2_4ghz", baseline_dbm=-70.0, margin_db=10.0)
    assert hits == [
        SpectrumHitReading(band="ism_2_4ghz", freq_hz=2450000000, power_dbm=-40.0, baseline_dbm=-70.0)
    ]


def test_detect_hits_none_above_threshold():
    readings = [(2450000000, -75.0)]
    assert detect_hits(readings, "ism_2_4ghz", baseline_dbm=-70.0, margin_db=10.0) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/scanner-spectrum && python3 -m venv .venv && .venv/bin/pip install pytest && .venv/bin/pytest tests/ -v`
Expected: collection error — `ModuleNotFoundError: No module named 'spectrum_source'` (the file doesn't exist yet).

- [ ] **Step 3: Write the minimal implementation**

Create `services/scanner-spectrum/app/spectrum_source.py`:

```python
"""Multi-band spectrum energy-detection source, backed by hackrf_sweep.

hackrf_sweep (ships with the HackRF host tools) natively retunes across a
given frequency range and streams power-per-bin readings as CSV lines --
this module wraps it as a subprocess (see the calibration/sweep functions
appended in a later change) rather than hand-rolling IQ capture and FFT in
Python, the same "wrap the purpose-built CLI tool" pattern
ubertooth_source.py uses for ubertooth-btle.

This half of the module is pure logic (line parsing, band bucketing,
threshold detection) with no I/O, kept separate from the subprocess/asyncio
plumbing so it can be unit tested directly. See
docs/superpowers/specs/2026-08-05-multiband-spectrum-scanner-design.md for
the full design and band-list rationale.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Band:
    name: str
    low_hz: int
    high_hz: int


# Fixed default sweep order -- not user-configurable per mission (YAGNI for
# now; see design doc Section 4 for the rationale behind each range).
DEFAULT_BANDS: list[Band] = [
    Band("cellular_low", 698_000_000, 960_000_000),
    Band("cellular_mid", 1_710_000_000, 2_200_000_000),
    Band("ism_2_4ghz", 2_400_000_000, 2_483_500_000),
    Band("keyfob", 300_000_000, 450_000_000),
]


@dataclass
class SpectrumHitReading:
    band: str
    freq_hz: int
    power_dbm: float
    baseline_dbm: float


# hackrf_sweep CSV line: date, time, hz_low, hz_high, hz_bin_width,
# num_samples, dB, dB, dB, ... (one dB reading per bin across the segment).
_LINE_RE = re.compile(
    r"^[\d-]+,\s*[\d:.]+,\s*(?P<low>\d+),\s*(?P<high>\d+),\s*"
    r"(?P<binw>[\d.]+),\s*(?P<n>\d+),\s*(?P<rest>.+)$"
)


def parse_sweep_line(line: str) -> tuple[int, int, float, list[float]] | None:
    """Parse one hackrf_sweep CSV output line into
    (hz_low, hz_high, hz_bin_width, db_values), or None if the line doesn't
    match (e.g. a stray log line interleaved on stdout).
    """
    m = _LINE_RE.match(line.strip())
    if not m:
        return None
    try:
        db_values = [float(x) for x in m.group("rest").split(",")]
    except ValueError:
        return None
    return int(m.group("low")), int(m.group("high")), float(m.group("binw")), db_values


def bin_center_freqs(hz_low: int, bin_width: float, count: int) -> list[int]:
    return [int(hz_low + bin_width * (i + 0.5)) for i in range(count)]


def expand_readings(hz_low: int, bin_width: float, db_values: list[float]) -> list[tuple[int, float]]:
    freqs = bin_center_freqs(hz_low, bin_width, len(db_values))
    return list(zip(freqs, db_values))


def _readings_from_line(line: str) -> list[tuple[int, float]]:
    parsed = parse_sweep_line(line)
    if parsed is None:
        return []
    hz_low, _hz_high, bin_width, db_values = parsed
    return expand_readings(hz_low, bin_width, db_values)


def band_for_freq(freq_hz: int, bands: list[Band] | None = None) -> str | None:
    for band in (bands if bands is not None else DEFAULT_BANDS):
        if band.low_hz <= freq_hz < band.high_hz:
            return band.name
    return None


def average_power(powers: list[float]) -> float:
    return sum(powers) / len(powers) if powers else 0.0


def detect_hits(
    readings: list[tuple[int, float]], band_name: str, baseline_dbm: float, margin_db: float
) -> list[SpectrumHitReading]:
    threshold = baseline_dbm + margin_db
    return [
        SpectrumHitReading(band=band_name, freq_hz=freq, power_dbm=power, baseline_dbm=baseline_dbm)
        for freq, power in readings
        if power > threshold
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ -v` (from `services/scanner-spectrum/`)
Expected: all tests PASS (13 passed).

- [ ] **Step 5: Commit**

```bash
git add services/scanner-spectrum/app/spectrum_source.py \
        services/scanner-spectrum/tests/test_spectrum_source.py \
        services/scanner-spectrum/pytest.ini
git commit -m "$(cat <<'EOF'
Add pure sweep-parsing/band/detection logic for the spectrum scanner

parse_sweep_line/band_for_freq/detect_hits are the core of Component F:
bucket hackrf_sweep's CSV output into the fixed default band list and
flag readings above a calibrated per-band baseline. Kept separate from
the subprocess/asyncio plumbing (added next) so this logic is directly
unit testable.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `SpectrumStorage` — SQLite persistence for spectrum hits

**Files:**
- Create: `services/scanner-spectrum/app/storage.py`
- Create: `services/scanner-spectrum/tests/test_storage.py`

**Interfaces:**
- Consumes: nothing from Task 1
- Produces: `SpectrumHit` (dataclass: `timestamp_utc: str, source_unit_id: str, band: str, freq_hz: int, power_dbm: float, baseline_dbm: float, lat: float | None, lon: float | None, alt_m: float | None, gps_fix_age_s: float | None`), `SpectrumStorage(db_path: str | Path)` with `.insert_hit(h: SpectrumHit) -> None` and `.close() -> None`

- [ ] **Step 1: Write the failing tests**

Create `services/scanner-spectrum/tests/test_storage.py`:

```python
"""Tests for SpectrumStorage."""

from __future__ import annotations

import sqlite3

from storage import SpectrumHit, SpectrumStorage


def test_insert_hit_persists_row(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    storage = SpectrumStorage(db_path)
    hit = SpectrumHit(
        timestamp_utc="2026-08-05T14:23:01+00:00",
        source_unit_id="ground-logger-spectrum-01",
        band="ism_2_4ghz",
        freq_hz=2450000000,
        power_dbm=-40.0,
        baseline_dbm=-70.0,
        lat=45.1,
        lon=-122.2,
        alt_m=350.0,
        gps_fix_age_s=1.2,
    )
    storage.insert_hit(hit)
    storage.close()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM spectrum_hits").fetchone()
    assert row["band"] == "ism_2_4ghz"
    assert row["freq_hz"] == 2450000000
    assert row["power_dbm"] == -40.0
    assert row["baseline_dbm"] == -70.0
    assert row["lat"] == 45.1
    assert row["source_unit_id"] == "ground-logger-spectrum-01"
    conn.close()


def test_insert_hit_allows_null_gps(tmp_path):
    db_path = tmp_path / "test2.sqlite3"
    storage = SpectrumStorage(db_path)
    hit = SpectrumHit(
        timestamp_utc="2026-08-05T14:23:01+00:00",
        source_unit_id="ground-logger-spectrum-01",
        band="keyfob",
        freq_hz=433920000,
        power_dbm=-55.0,
        baseline_dbm=-80.0,
        lat=None,
        lon=None,
        alt_m=None,
        gps_fix_age_s=None,
    )
    storage.insert_hit(hit)
    storage.close()

    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT lat, lon FROM spectrum_hits").fetchone()
    assert row == (None, None)
    conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_storage.py -v` (from `services/scanner-spectrum/`)
Expected: `ModuleNotFoundError: No module named 'storage'`

- [ ] **Step 3: Write the minimal implementation**

Create `services/scanner-spectrum/app/storage.py`:

```python
"""SQLite storage for multi-band spectrum energy-detection hits.

Lives in the same shared detections.sqlite3 file as the BLE scanners'
`detections` table (via the ./data volume), but in its own table -- a
spectrum hit has no persistent device identity like a BLE MAC to
tag/label/track across sightings, so it doesn't belong in `detections`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SpectrumHit:
    timestamp_utc: str
    source_unit_id: str
    band: str
    freq_hz: int
    power_dbm: float
    baseline_dbm: float
    lat: float | None
    lon: float | None
    alt_m: float | None
    gps_fix_age_s: float | None


SCHEMA = """
CREATE TABLE IF NOT EXISTS spectrum_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    source_unit_id TEXT NOT NULL,
    band TEXT NOT NULL,
    freq_hz INTEGER NOT NULL,
    power_dbm REAL NOT NULL,
    baseline_dbm REAL NOT NULL,
    lat REAL,
    lon REAL,
    alt_m REAL,
    gps_fix_age_s REAL
);
CREATE INDEX IF NOT EXISTS idx_spectrum_hits_band ON spectrum_hits(band);
CREATE INDEX IF NOT EXISTS idx_spectrum_hits_timestamp ON spectrum_hits(timestamp_utc);
"""


class SpectrumStorage:
    def __init__(self, db_path: str | Path) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(SCHEMA)

    def insert_hit(self, h: SpectrumHit) -> None:
        self._conn.execute(
            """
            INSERT INTO spectrum_hits (
                timestamp_utc, source_unit_id, band, freq_hz, power_dbm,
                baseline_dbm, lat, lon, alt_m, gps_fix_age_s
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                h.timestamp_utc,
                h.source_unit_id,
                h.band,
                h.freq_hz,
                h.power_dbm,
                h.baseline_dbm,
                h.lat,
                h.lon,
                h.alt_m,
                h.gps_fix_age_s,
            ),
        )

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ -v` (from `services/scanner-spectrum/`)
Expected: all tests PASS (15 passed — 13 from Task 1 + 2 new).

- [ ] **Step 5: Commit**

```bash
git add services/scanner-spectrum/app/storage.py services/scanner-spectrum/tests/test_storage.py
git commit -m "$(cat <<'EOF'
Add SpectrumStorage for the spectrum_hits table

Same isolation_level=None/WAL pattern as the BLE scanners' storage.py,
in a separate table since a spectrum hit has no persistent MAC-like
identity to tag or track across sightings.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Copy `gps_client.py`

**Files:**
- Create: `services/scanner-spectrum/app/gps_client.py`

**Interfaces:**
- Produces: `Fix` (dataclass: `lat: float, lon: float, alt_m: float | None, received_at: float`), `GpsClient(host: str, port: int)` with `.latest_fix() -> Fix | None` and `async .run() -> None`

`scanner-spectrum` is its own top-level service directory (per the design doc), not nested under `services/scanner/`, so it can't import the existing `gps_client.py` as a sibling module the way `main_ubertooth.py` does. There's no shared-package infrastructure across service directories in this repo (each Dockerfile does a flat `COPY app/ .`), so this is a verbatim copy of already-working, already-proven code — no new logic, no new test needed.

- [ ] **Step 1: Copy the file exactly**

Create `services/scanner-spectrum/app/gps_client.py` with this exact content (identical to `services/scanner/app/gps_client.py`):

```python
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
```

- [ ] **Step 2: Verify it's byte-identical**

Run: `diff services/scanner/app/gps_client.py services/scanner-spectrum/app/gps_client.py`
Expected: no output (files identical).

- [ ] **Step 3: Commit**

```bash
git add services/scanner-spectrum/app/gps_client.py
git commit -m "$(cat <<'EOF'
Copy gps_client.py into the new scanner-spectrum service

Verbatim copy of the already-proven gpsd client -- scanner-spectrum is
its own top-level service directory (not nested under services/scanner/
like main_ubertooth.py), and there's no shared-package infrastructure
across service directories in this repo, so duplication is the
established way this codebase reuses code across independently-built
Docker images.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `hackrf_sweep` subprocess wrapper, calibration, and live sweep loop

**Files:**
- Modify: `services/scanner-spectrum/app/spectrum_source.py` (append)
- Modify: `services/scanner-spectrum/tests/test_spectrum_source.py` (append)

**Interfaces:**
- Consumes: `Band`, `DEFAULT_BANDS`, `SpectrumHitReading`, `_readings_from_line`, `average_power`, `detect_hits` (all from Task 1)
- Produces: `DEFAULT_MARGIN_DB: float`, `DEFAULT_CALIBRATION_S: float`, `DEFAULT_DWELL_S: float`, `async stream_hits(margin_db: float = DEFAULT_MARGIN_DB, calibration_s: float = DEFAULT_CALIBRATION_S, dwell_s: float = DEFAULT_DWELL_S, bands: list[Band] | None = None) -> AsyncIterator[SpectrumHitReading]`

This is the impure half of the module — subprocess management and the calibrate-then-sweep-forever orchestration. The line-parsing/detection logic it calls was already unit tested in Task 1; here, `_run_hackrf_sweep`'s subprocess plumbing is tested against a fake `hackrf_sweep` script on `PATH` (same technique real-hardware-free CI would use). `_sweep_band`'s retry-forever wrapper and `stream_hits`'s full calibration/loop orchestration are validated against real hardware in Task 7, matching how `ubertooth_source.stream_advertisements`'s outer retry loop isn't unit tested either — only its pure parsing half is.

- [ ] **Step 1: Write the failing test**

Append to `services/scanner-spectrum/tests/test_spectrum_source.py`:

```python
import asyncio
import os

import pytest


@pytest.fixture
def fake_hackrf_sweep(tmp_path, monkeypatch):
    script = tmp_path / "hackrf_sweep"
    script.write_text(
        "#!/bin/sh\n"
        "while true; do\n"
        '  echo "2026-08-05, 14:23:01.000000, 2400000000, 2405000000, 5000.00, 20, -54.32, -61.10"\n'
        "  sleep 0.01\n"
        "done\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return script


def test_run_hackrf_sweep_collects_readings(fake_hackrf_sweep):
    from spectrum_source import _run_hackrf_sweep

    readings = asyncio.run(_run_hackrf_sweep(2_400_000_000, 2_405_000_000, duration_s=0.1))
    assert (2400000500, -54.32) in readings
    assert (2400001500, -61.10) in readings


def test_run_hackrf_sweep_raises_when_binary_missing(tmp_path, monkeypatch):
    from spectrum_source import _run_hackrf_sweep

    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir, no hackrf_sweep on it
    with pytest.raises(FileNotFoundError):
        asyncio.run(_run_hackrf_sweep(2_400_000_000, 2_405_000_000, duration_s=0.1))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_spectrum_source.py -v` (from `services/scanner-spectrum/`)
Expected: `ImportError: cannot import name '_run_hackrf_sweep' from 'spectrum_source'`

- [ ] **Step 3: Write the minimal implementation**

Append to `services/scanner-spectrum/app/spectrum_source.py`:

```python
import asyncio
import logging
import math

logger = logging.getLogger(__name__)

DEFAULT_MARGIN_DB = 10.0
DEFAULT_CALIBRATION_S = 10.0
DEFAULT_DWELL_S = 5.0


async def _log_stderr(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    async for raw_line in stream:
        logger.warning("hackrf_sweep: %s", raw_line.decode(errors="replace").rstrip("\n"))


async def _run_hackrf_sweep(low_hz: int, high_hz: int, duration_s: float) -> list[tuple[int, float]]:
    """Run hackrf_sweep over [low_hz, high_hz) for duration_s seconds and
    return every (center_freq_hz, power_dbm) reading collected.

    hackrf_sweep takes its range as whole MHz; floor the low edge and
    ceil the high edge so the requested band is always fully covered
    even when its bounds aren't MHz-aligned (e.g. the ISM band's
    2483.5 MHz upper edge).
    """
    low_mhz = low_hz // 1_000_000
    high_mhz = math.ceil(high_hz / 1_000_000)
    args = ["hackrf_sweep", "-f", f"{low_mhz}:{high_mhz}"]

    readings: list[tuple[int, float]] = []
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stderr_task = asyncio.create_task(_log_stderr(proc.stderr))

    async def _read() -> None:
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            readings.extend(_readings_from_line(raw_line.decode(errors="replace")))

    try:
        await asyncio.wait_for(_read(), timeout=duration_s)
    except asyncio.TimeoutError:
        pass
    finally:
        stderr_task.cancel()
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()

    return readings


async def _sweep_band(band: Band, duration_s: float) -> list[tuple[int, float]]:
    """Run _run_hackrf_sweep for one band, retrying forever every 5s if the
    binary is missing or the sweep fails (e.g. device unplugged mid-run) --
    same reconnect philosophy as GpsClient.run().
    """
    while True:
        try:
            return await _run_hackrf_sweep(band.low_hz, band.high_hz, duration_s)
        except FileNotFoundError:
            logger.error("hackrf_sweep not found on PATH; is it installed?")
            await asyncio.sleep(5)
        except OSError as exc:
            logger.warning("hackrf_sweep failed for band %s (%s); retrying in 5s", band.name, exc)
            await asyncio.sleep(5)


async def stream_hits(
    margin_db: float = DEFAULT_MARGIN_DB,
    calibration_s: float = DEFAULT_CALIBRATION_S,
    dwell_s: float = DEFAULT_DWELL_S,
    bands: list[Band] | None = None,
):
    """Calibrate a baseline across all bands once, then sweep them forever,
    yielding SpectrumHitReading for every bin that exceeds its band's
    calibrated baseline + margin_db.
    """
    active_bands = bands if bands is not None else DEFAULT_BANDS

    baseline: dict[str, float] = {}
    logger.info("calibrating baseline for %d bands (%.0fs each)...", len(active_bands), calibration_s)
    for band in active_bands:
        readings = await _sweep_band(band, calibration_s)
        powers = [p for _, p in readings]
        baseline[band.name] = average_power(powers)
        logger.info(
            "baseline[%s] = %.1f dBm (%d samples)", band.name, baseline[band.name], len(powers)
        )

    while True:
        for band in active_bands:
            readings = await _sweep_band(band, dwell_s)
            for hit in detect_hits(readings, band.name, baseline[band.name], margin_db):
                yield hit
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ -v` (from `services/scanner-spectrum/`)
Expected: all tests PASS (17 passed).

- [ ] **Step 5: Commit**

```bash
git add services/scanner-spectrum/app/spectrum_source.py services/scanner-spectrum/tests/test_spectrum_source.py
git commit -m "$(cat <<'EOF'
Add hackrf_sweep subprocess wrapper and calibrate-then-sweep loop

_run_hackrf_sweep collects (freq, power) readings from one hackrf_sweep
invocation; _sweep_band wraps it with the retry-forever-every-5s
philosophy used everywhere else in this project so a missing/unplugged
HackRF never crashes the container. stream_hits calibrates a baseline
across all four default bands once at startup, then sweeps them forever,
yielding hits that exceed that band's baseline + margin.

Tested against a fake hackrf_sweep script on PATH rather than real
hardware; the full calibration/sweep loop is validated against a real
HackRF in a later task.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `main_spectrum.py` entrypoint

**Files:**
- Create: `services/scanner-spectrum/app/main_spectrum.py`

**Interfaces:**
- Consumes: `GpsClient` (Task 3), `stream_hits`, `DEFAULT_MARGIN_DB`, `DEFAULT_CALIBRATION_S`, `DEFAULT_DWELL_S` (Task 4), `SpectrumHit`, `SpectrumStorage` (Task 2)
- Produces: `run() -> None` (async), `main() -> None` — process entrypoint, no other module imports this

No new test — this is thin orchestration glue with no branching logic of its own beyond what's already tested (identical role to `main_ubertooth.py`, which also has no test in this codebase). Verified in Task 7's hardware bench test.

- [ ] **Step 1: Write the entrypoint**

Create `services/scanner-spectrum/app/main_spectrum.py`:

```python
"""Wide-area multi-band spectrum energy-detection logger.

Sweeps a fixed default list of frequency bands (cellular, 2.4GHz ISM,
sub-GHz keyfob) via hackrf_sweep, GPS-tags any reading that exceeds that
band's per-flight calibrated baseline, and appends it to the shared SQLite
log as a spectrum_hits row. Meant to run alongside the BLE scanners
(main.py, main_ubertooth.py), tagged with its own SOURCE_UNIT_ID -- a
spectrum hit has no persistent device identity like a BLE MAC, so it isn't
merged into the detections table. See
docs/superpowers/specs/2026-08-05-multiband-spectrum-scanner-design.md for
the full design.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from datetime import UTC, datetime

from gps_client import GpsClient
from spectrum_source import DEFAULT_CALIBRATION_S, DEFAULT_DWELL_S, DEFAULT_MARGIN_DB, stream_hits
from storage import SpectrumHit, SpectrumStorage

logger = logging.getLogger("scanner-spectrum")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


DB_PATH = _env("DB_PATH", "/data/detections.sqlite3")
GPSD_HOST = _env("GPSD_HOST", "127.0.0.1")
GPSD_PORT = int(_env("GPSD_PORT", "2947"))
SOURCE_UNIT_ID = _env("SOURCE_UNIT_ID", "ground-logger-spectrum")
SPECTRUM_CALIBRATION_S = float(_env("SPECTRUM_CALIBRATION_S", str(DEFAULT_CALIBRATION_S)))
SPECTRUM_DWELL_S = float(_env("SPECTRUM_DWELL_S", str(DEFAULT_DWELL_S)))
SPECTRUM_HIT_MARGIN_DB = float(_env("SPECTRUM_HIT_MARGIN_DB", str(DEFAULT_MARGIN_DB)))
LOG_LEVEL = _env("LOG_LEVEL", "INFO")

STALE_FIX_WARN_S = 30.0


async def run() -> None:
    logging.basicConfig(
        level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logger.info(
        "starting spectrum scanner: db=%s gpsd=%s:%s calibration=%.0fs dwell=%.0fs margin=%.0fdB unit=%s",
        DB_PATH,
        GPSD_HOST,
        GPSD_PORT,
        SPECTRUM_CALIBRATION_S,
        SPECTRUM_DWELL_S,
        SPECTRUM_HIT_MARGIN_DB,
        SOURCE_UNIT_ID,
    )

    storage = SpectrumStorage(DB_PATH)
    gps = GpsClient(GPSD_HOST, GPSD_PORT)
    gps_task = asyncio.create_task(gps.run())

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    async def consume() -> None:
        async for reading in stream_hits(
            margin_db=SPECTRUM_HIT_MARGIN_DB,
            calibration_s=SPECTRUM_CALIBRATION_S,
            dwell_s=SPECTRUM_DWELL_S,
        ):
            fix = gps.latest_fix()
            fix_age = time.monotonic() - fix.received_at if fix else None
            if fix_age is not None and fix_age > STALE_FIX_WARN_S:
                logger.warning("GPS fix is %.0fs old", fix_age)

            hit = SpectrumHit(
                timestamp_utc=datetime.now(UTC).isoformat(),
                source_unit_id=SOURCE_UNIT_ID,
                band=reading.band,
                freq_hz=reading.freq_hz,
                power_dbm=reading.power_dbm,
                baseline_dbm=reading.baseline_dbm,
                lat=fix.lat if fix else None,
                lon=fix.lon if fix else None,
                alt_m=fix.alt_m if fix else None,
                gps_fix_age_s=fix_age,
            )
            storage.insert_hit(hit)
            logger.info(
                "hit band=%s freq=%.3fMHz power=%.1fdBm baseline=%.1fdBm lat=%s lon=%s",
                hit.band,
                hit.freq_hz / 1_000_000,
                hit.power_dbm,
                hit.baseline_dbm,
                hit.lat,
                hit.lon,
            )

    consume_task = asyncio.create_task(consume())
    logger.info("sweeping...")
    await stop_event.wait()

    logger.info("shutting down")
    consume_task.cancel()
    gps_task.cancel()
    storage.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Sanity-check it imports cleanly**

Run: `cd services/scanner-spectrum && .venv/bin/python -c "import sys; sys.path.insert(0, 'app'); import main_spectrum"`
Expected: no output, exit code 0 (import succeeds; `run()` isn't invoked).

- [ ] **Step 3: Commit**

```bash
git add services/scanner-spectrum/app/main_spectrum.py
git commit -m "$(cat <<'EOF'
Add main_spectrum.py entrypoint

Drives stream_hits() through the same Storage/GpsClient/signal-handling
pattern main.py and main_ubertooth.py already use, writing into
spectrum_hits instead of detections.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Dockerfile and docker-compose.yml wiring

**Files:**
- Create: `services/scanner-spectrum/Dockerfile`
- Modify: `docker-compose.yml`

**Interfaces:**
- Consumes: `services/scanner-spectrum/app/main_spectrum.py` (Task 5) as the container entrypoint
- Produces: `ble-sar-df-scanner-spectrum:local` Docker image, `scanner-spectrum` compose service

- [ ] **Step 1: Write the Dockerfile**

Create `services/scanner-spectrum/Dockerfile`:

```dockerfile
FROM debian:trixie-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git pkg-config ca-certificates \
        libusb-1.0-0-dev libfftw3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Debian packages an older hackrf release; build current upstream instead
# so hackrf_sweep matches the device firmware, same rationale as
# services/scanner/Dockerfile.ubertooth.
RUN git clone --depth 1 https://github.com/greatscottgadgets/hackrf.git && \
    cmake -S hackrf/host -B hackrf/host/build \
        -DCMAKE_POLICY_VERSION_MINIMUM=3.5 && \
    cmake --build hackrf/host/build -j"$(nproc)" && \
    cmake --install hackrf/host/build && ldconfig

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libusb-1.0-0 libfftw3-3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local/lib/libhackrf* /usr/local/lib/
COPY --from=build /usr/local/bin/hackrf_sweep /usr/local/bin/hackrf_info /usr/local/bin/
RUN ldconfig

WORKDIR /app
COPY app/ .

ENTRYPOINT ["python", "main_spectrum.py"]
```

- [ ] **Step 2: Build it**

Run: `cd /home/seraph/Projects/ble-sar-df && docker build -t ble-sar-df-scanner-spectrum:local services/scanner-spectrum`
Expected: build succeeds (this compiles hackrf from source, may take a few minutes); final line `Successfully tagged ble-sar-df-scanner-spectrum:local` (or the buildkit equivalent success output).

- [ ] **Step 3: Add the compose service**

In `docker-compose.yml`, after the `scanner-ubertooth` service block (ends at the `LOG_LEVEL: INFO` line before `ground-station:`), insert:

```yaml
  # Third, independent capture unit -- a HackRF One doing wide-area energy
  # detection across cellular/WiFi/ISM/keyfob bands rather than decoding a
  # specific protocol. See
  # docs/superpowers/specs/2026-08-05-multiband-spectrum-scanner-design.md.
  # Writes into the same shared detections.sqlite3's spectrum_hits table,
  # tagged with its own SOURCE_UNIT_ID. Starts unconditionally like the
  # other two scanners -- if the HackRF isn't attached, spectrum_source.py
  # logs a warning and retries every 5s forever.
  scanner-spectrum:
    build: ./services/scanner-spectrum
    image: ble-sar-df-scanner-spectrum:local
    restart: unless-stopped
    network_mode: host
    privileged: true
    volumes:
      - ./data:/data
    environment:
      DB_PATH: /data/detections.sqlite3
      GPSD_HOST: 127.0.0.1
      GPSD_PORT: "2947"
      SOURCE_UNIT_ID: ground-logger-spectrum-01
      SPECTRUM_CALIBRATION_S: "10"
      SPECTRUM_DWELL_S: "5"
      SPECTRUM_HIT_MARGIN_DB: "10"
      LOG_LEVEL: INFO

```

- [ ] **Step 4: Add it to ground-station's `depends_on`**

In `docker-compose.yml`, find the `ground-station` service's `depends_on` block:

```yaml
    depends_on:
      - scanner
```

Change to:

```yaml
    depends_on:
      - scanner
      - scanner-spectrum
```

- [ ] **Step 5: Validate the compose file**

Run: `docker compose config --quiet`
Expected: no output, exit code 0 (valid YAML, no schema errors).

Run: `docker compose up --build scanner-spectrum`
Expected: image builds, container starts, logs show `starting spectrum scanner: ...` followed by either a successful calibration pass (if a HackRF is attached) or `hackrf_sweep not found on PATH` / a sweep failure warning every ~5s (if not) — either way, the container must stay up and not exit. Stop with Ctrl-C.

- [ ] **Step 6: Commit**

```bash
git add services/scanner-spectrum/Dockerfile docker-compose.yml
git commit -m "$(cat <<'EOF'
Wire scanner-spectrum into docker-compose.yml

Builds hackrf_sweep from source (matching Dockerfile.ubertooth's
build-from-upstream rationale) and starts unconditionally alongside the
two BLE scanners, retrying quietly if the HackRF isn't attached.
ground-station now depends_on it too, matching the existing scanner
dependency.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Validate against real HackRF hardware

**Files:** none (verification only — this task may adjust `services/scanner-spectrum/app/spectrum_source.py`'s `_LINE_RE` regex if real `hackrf_sweep` output doesn't match the format assumed in Task 1's tests)

This task needs the physical HackRF One and antenna, so it's one for you to run directly — I don't have hardware access.

- [ ] **Step 1: Confirm `hackrf_sweep`'s real output format**

With the HackRF plugged in, run on the host (not in Docker, for a quick manual check):
```bash
hackrf_sweep -f 2400:2483 -1
```
(`-1` does one sweep and exits.) Compare a sample line against the format assumed by `_LINE_RE` in `spectrum_source.py`:
```
date, time, hz_low, hz_high, hz_bin_width, num_samples, dB, dB, dB, ...
```
If the real output differs (field order, delimiter, extra columns), update `_LINE_RE` and the two format-dependent tests in `test_spectrum_source.py` (`test_parse_sweep_line_valid`, `test_readings_from_line_combines_parse_and_expand`) to match, then re-run `.venv/bin/pytest tests/ -v` and confirm they still pass.

- [ ] **Step 2: Bring the full stack up with the HackRF attached**

```bash
cd /home/seraph/Projects/ble-sar-df
docker compose up --build scanner-spectrum
```
Expected: logs show `calibrating baseline for 4 bands (10s each)...` followed by four `baseline[<band>] = ...` lines, then `sweeping...`.

- [ ] **Step 3: Trigger a real signal and confirm a hit is logged**

While it's sweeping the `keyfob` band (watch the logs to catch the right ~5s window, or just try a few times), press a car remote or garage door opener near the antenna. Expected: a log line like `hit band=keyfob freq=433.9...MHz power=...dBm baseline=...dBm`.

Confirm it landed in the database:
```bash
sqlite3 data/detections.sqlite3 "select band, freq_hz, power_dbm, baseline_dbm, timestamp_utc from spectrum_hits order by id desc limit 5;"
```
Expected: at least one `keyfob` row with a timestamp matching when you pressed the remote.

- [ ] **Step 4: If the regex needed adjusting, commit that fix**

```bash
git add services/scanner-spectrum/app/spectrum_source.py services/scanner-spectrum/tests/test_spectrum_source.py
git commit -m "$(cat <<'EOF'
Fix hackrf_sweep CSV format assumption after real-hardware validation

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
(Skip this step if the format already matched and nothing changed.)

---

### Task 8: Ground-station `db.py` — spectrum hit queries and reset

**Files:**
- Modify: `services/ground_station/app/db.py`
- Create: `services/ground_station/tests/test_db_spectrum.py`
- Create: `services/ground_station/pytest.ini`

**Interfaces:**
- Consumes: the `spectrum_hits` schema from Task 2 (column names: `id, timestamp_utc, source_unit_id, band, freq_hz, power_dbm, baseline_dbm, lat, lon, alt_m, gps_fix_age_s`)
- Produces (added to the existing `Database` class): `max_spectrum_hit_id() -> int`, `spectrum_hits_since(since_id: int) -> list[sqlite3.Row]`, `recent_spectrum_hits(limit: int = 2000) -> list[dict]`; modifies existing `reset()` to also clear `spectrum_hits`

`max_spectrum_hit_id`/`spectrum_hits_since`/`recent_spectrum_hits` must not crash if `spectrum_hits` doesn't exist yet — unlike the primary `scanner` service (which `ground-station` has always assumed is present), `scanner-spectrum` is genuinely optional hardware some deployments won't run at all, so a missing table is an expected, not exceptional, state.

- [ ] **Step 1: Write the failing tests**

Create `services/ground_station/pytest.ini`:

```ini
[pytest]
pythonpath = app
```

Create `services/ground_station/tests/test_db_spectrum.py`:

```python
"""Tests for Database's spectrum_hits query methods."""

from __future__ import annotations

import sqlite3

from db import Database

_SPECTRUM_SCHEMA = """
CREATE TABLE IF NOT EXISTS spectrum_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    source_unit_id TEXT NOT NULL,
    band TEXT NOT NULL,
    freq_hz INTEGER NOT NULL,
    power_dbm REAL NOT NULL,
    baseline_dbm REAL NOT NULL,
    lat REAL,
    lon REAL,
    alt_m REAL,
    gps_fix_age_s REAL
)
"""


def _seed_spectrum_hit(db_path, **overrides):
    conn = sqlite3.connect(db_path)
    conn.execute(_SPECTRUM_SCHEMA)
    row = {
        "timestamp_utc": "2026-08-05T14:23:01+00:00",
        "source_unit_id": "ground-logger-spectrum-01",
        "band": "ism_2_4ghz",
        "freq_hz": 2450000000,
        "power_dbm": -40.0,
        "baseline_dbm": -70.0,
        "lat": 45.1,
        "lon": -122.2,
        "alt_m": 350.0,
        "gps_fix_age_s": 1.2,
        **overrides,
    }
    conn.execute(
        """
        INSERT INTO spectrum_hits (
            timestamp_utc, source_unit_id, band, freq_hz, power_dbm,
            baseline_dbm, lat, lon, alt_m, gps_fix_age_s
        ) VALUES (:timestamp_utc, :source_unit_id, :band, :freq_hz, :power_dbm,
                   :baseline_dbm, :lat, :lon, :alt_m, :gps_fix_age_s)
        """,
        row,
    )
    conn.commit()
    conn.close()


def test_max_spectrum_hit_id_reflects_inserted_row(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    _seed_spectrum_hit(db_path)
    db = Database(str(db_path))
    assert db.max_spectrum_hit_id() == 1


def test_max_spectrum_hit_id_returns_zero_when_table_missing(tmp_path):
    db_path = tmp_path / "empty.sqlite3"
    db = Database(str(db_path))  # nothing seeded -- spectrum_hits never created
    assert db.max_spectrum_hit_id() == 0


def test_spectrum_hits_since_returns_new_rows_only(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    _seed_spectrum_hit(db_path, band="keyfob", freq_hz=433920000)
    _seed_spectrum_hit(db_path, band="cellular_low", freq_hz=850000000)
    db = Database(str(db_path))
    rows = db.spectrum_hits_since(1)
    assert len(rows) == 1
    assert rows[0]["band"] == "cellular_low"


def test_spectrum_hits_since_returns_empty_list_when_table_missing(tmp_path):
    db_path = tmp_path / "empty.sqlite3"
    db = Database(str(db_path))
    assert db.spectrum_hits_since(0) == []


def test_recent_spectrum_hits_shape(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    _seed_spectrum_hit(db_path)
    db = Database(str(db_path))
    hits = db.recent_spectrum_hits()
    assert len(hits) == 1
    assert hits[0]["band"] == "ism_2_4ghz"
    assert hits[0]["freq_hz"] == 2450000000
    assert hits[0]["lat"] == 45.1


def test_recent_spectrum_hits_returns_empty_list_when_table_missing(tmp_path):
    db_path = tmp_path / "empty.sqlite3"
    db = Database(str(db_path))
    assert db.recent_spectrum_hits() == []


def test_reset_clears_spectrum_hits(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    _seed_spectrum_hit(db_path)
    db = Database(str(db_path))
    assert db.max_spectrum_hit_id() == 1
    db.reset()
    assert db.recent_spectrum_hits() == []


def test_reset_does_not_crash_when_spectrum_hits_missing(tmp_path):
    db_path = tmp_path / "empty.sqlite3"
    db = Database(str(db_path))
    db.reset()  # must not raise even though spectrum_hits was never created
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/ground_station && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt pytest && .venv/bin/pytest tests/test_db_spectrum.py -v`
Expected: `AttributeError: 'Database' object has no attribute 'max_spectrum_hit_id'`

- [ ] **Step 3: Implement the additions**

In `services/ground_station/app/db.py`, add these methods to the `Database` class (e.g. after `heatmap()`, before `set_tag()`):

```python
    def max_spectrum_hit_id(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(id), 0) AS m FROM spectrum_hits"
            ).fetchone()
        except sqlite3.OperationalError:
            return 0  # scanner-spectrum hasn't run yet, or isn't deployed at all
        return row["m"]

    def spectrum_hits_since(self, since_id: int) -> list[sqlite3.Row]:
        try:
            return self._conn.execute(
                "SELECT * FROM spectrum_hits WHERE id > ? ORDER BY id", (since_id,)
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    def recent_spectrum_hits(self, limit: int = 2000) -> list[dict]:
        try:
            rows = self._conn.execute(
                """
                SELECT id, timestamp_utc, source_unit_id, band, freq_hz, power_dbm,
                       baseline_dbm, lat, lon, alt_m, gps_fix_age_s
                FROM spectrum_hits
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]
```

Then update the existing `reset()` method:

```python
    def reset(self) -> None:
        """Wipe all detections, spectrum hits, and tags. Deliberately leaves
        sqlite's autoincrement bookkeeping alone so new ids keep climbing
        rather than restarting at 1 -- that keeps the poll loop's "highest id
        seen so far" tracking valid with no extra coordination after a reset.
        """
        self._conn.execute("DELETE FROM detections")
        self._conn.execute("DELETE FROM device_tags")
        try:
            self._conn.execute("DELETE FROM spectrum_hits")
        except sqlite3.OperationalError:
            pass  # scanner-spectrum never deployed -- nothing to clear
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ -v` (from `services/ground_station/`)
Expected: all 8 new tests PASS.

- [ ] **Step 5: Commit**

```bash
git add services/ground_station/app/db.py services/ground_station/tests/test_db_spectrum.py services/ground_station/pytest.ini
git commit -m "$(cat <<'EOF'
Add spectrum_hits queries to Database; reset() now clears them too

max_spectrum_hit_id/spectrum_hits_since/recent_spectrum_hits mirror the
existing detections methods, but tolerate a missing spectrum_hits table
gracefully (return 0/[] instead of raising) -- unlike the primary
scanner, scanner-spectrum is genuinely optional hardware some
deployments won't run, so a missing table is an expected state, not an
exceptional one. "Reset Database" now also clears spectrum_hits so it
matches its own "ALL detections and tags" description.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Ground-station `main.py` — API endpoint and WebSocket broadcast

**Files:**
- Modify: `services/ground_station/app/main.py`

**Interfaces:**
- Consumes: `db.max_spectrum_hit_id()`, `db.spectrum_hits_since()`, `db.recent_spectrum_hits()` (Task 8)
- Produces: `GET /api/spectrum_hits`, WebSocket messages `{"type": "spectrum_hits", "hits": [...]}` (delta on poll, full snapshot on connect) and `{"type": "spectrum_hits", "hits": [], "reset": true}` (on `/api/reset`)

No dedicated test — `main.py`/`main_ubertooth.py`'s FastAPI/asyncio glue has no automated tests anywhere in this codebase either; verified manually below by actually running the server.

- [ ] **Step 1: Add the new endpoint**

In `services/ground_station/app/main.py`, after the existing `api_heatmap` endpoint (around line 102), add:

```python
@app.get("/api/spectrum_hits")
async def api_spectrum_hits() -> list[dict]:
    return db.recent_spectrum_hits()
```

- [ ] **Step 2: Extend `poll_loop` to broadcast new spectrum hits**

Replace the existing `poll_loop` function:

```python
async def poll_loop() -> None:
    last_id = db.max_detection_id()
    while True:
        await asyncio.sleep(POLL_INTERVAL_S)
        new_rows = db.detections_since(last_id)
        if new_rows:
            last_id = new_rows[-1]["id"]
            await manager.broadcast(
                {
                    "type": "devices",
                    "devices": db.device_summary(),
                    "new_count": len(new_rows),
                }
            )
```

with:

```python
async def poll_loop() -> None:
    last_id = db.max_detection_id()
    last_spectrum_id = db.max_spectrum_hit_id()
    while True:
        await asyncio.sleep(POLL_INTERVAL_S)
        new_rows = db.detections_since(last_id)
        if new_rows:
            last_id = new_rows[-1]["id"]
            await manager.broadcast(
                {
                    "type": "devices",
                    "devices": db.device_summary(),
                    "new_count": len(new_rows),
                }
            )
        new_spectrum_rows = db.spectrum_hits_since(last_spectrum_id)
        if new_spectrum_rows:
            last_spectrum_id = new_spectrum_rows[-1]["id"]
            await manager.broadcast(
                {
                    "type": "spectrum_hits",
                    "hits": [dict(r) for r in new_spectrum_rows],
                }
            )
```

- [ ] **Step 3: Send the initial spectrum snapshot on WebSocket connect**

In `ws_endpoint`, change:

```python
    try:
        await ws.send_json({"type": "devices", "devices": db.device_summary()})
        while True:
```

to:

```python
    try:
        await ws.send_json({"type": "devices", "devices": db.device_summary()})
        await ws.send_json({"type": "spectrum_hits", "hits": db.recent_spectrum_hits()})
        while True:
```

- [ ] **Step 4: Broadcast a spectrum reset signal from `/api/reset`**

Change:

```python
@app.post("/api/reset")
async def api_reset() -> dict:
    db.reset()
    await manager.broadcast({"type": "devices", "devices": db.device_summary()})
    return {"ok": True}
```

to:

```python
@app.post("/api/reset")
async def api_reset() -> dict:
    db.reset()
    await manager.broadcast({"type": "devices", "devices": db.device_summary()})
    await manager.broadcast({"type": "spectrum_hits", "hits": [], "reset": True})
    return {"ok": True}
```

- [ ] **Step 5: Manually verify the new endpoint**

```bash
cd services/ground_station
python3 -m venv .venv  # if not already created in Task 8
.venv/bin/pip install -r requirements.txt
mkdir -p /tmp/gs-spectrum-test
python3 -c "
import sqlite3
conn = sqlite3.connect('/tmp/gs-spectrum-test/detections.sqlite3')
conn.execute('''CREATE TABLE spectrum_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_utc TEXT, source_unit_id TEXT,
    band TEXT, freq_hz INTEGER, power_dbm REAL, baseline_dbm REAL,
    lat REAL, lon REAL, alt_m REAL, gps_fix_age_s REAL)''')
conn.execute('''INSERT INTO spectrum_hits (timestamp_utc, source_unit_id, band, freq_hz, power_dbm, baseline_dbm, lat, lon, alt_m, gps_fix_age_s)
    VALUES ('2026-08-05T14:00:00+00:00', 'ground-logger-spectrum-01', 'keyfob', 433920000, -45.0, -80.0, 45.1, -122.2, 350.0, 1.0)''')
conn.commit()
"
cd app
DB_PATH=/tmp/gs-spectrum-test/detections.sqlite3 ../.venv/bin/uvicorn main:app --port 8080 &
sleep 1
curl -s http://localhost:8080/api/spectrum_hits
kill %1
```
Expected: the `curl` prints a JSON array with one object where `"band":"keyfob"`, `"freq_hz":433920000`, `"lat":45.1`.

- [ ] **Step 6: Commit**

```bash
git add services/ground_station/app/main.py
git commit -m "$(cat <<'EOF'
Add /api/spectrum_hits and broadcast spectrum hits over the WebSocket

poll_loop now tracks spectrum_hits alongside detections and broadcasts
new rows as a separate {"type": "spectrum_hits"} message (a delta, not
a full snapshot -- the client is expected to append, matching how this
differs from the "devices" message which always resends the complete
current set). Initial WS connect and /api/reset both send/broadcast an
explicit spectrum_hits message too, so a fresh client and a post-reset
client both start from a correct empty-or-current state.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Ground-station UI — spectrum hits map layer

**Files:**
- Modify: `services/ground_station/app/static/index.html`

**Interfaces:**
- Consumes: `GET /api/spectrum_hits`, WebSocket `{"type": "spectrum_hits", "hits": [...], "reset"?: true}` (Task 9)
- Produces: a new "Spectrum hits" checkbox and Leaflet layer, visible client-side only

The WebSocket only ever sends *new* hits after the initial connect (a delta), not the full accumulated set — so unlike the `devices` array (which is always fully replaced on every message, since `device_summary()` always returns the complete current set), `spectrumHits` must be **appended to**, with the one exception of an explicit `reset: true` message, which replaces it. Getting this backwards (full-replace on every message) would silently drop every earlier hit each time a new one arrives.

- [ ] **Step 1: Add the checkbox**

In `services/ground_station/app/static/index.html`, change line 135:

```html
  <label><input type="checkbox" id="show-heatmap"> Hit density heatmap</label>
```

to:

```html
  <label><input type="checkbox" id="show-heatmap"> Hit density heatmap</label>
  <label><input type="checkbox" id="show-spectrum"> Spectrum hits</label>
```

- [ ] **Step 2: Add client-side state and band colors**

Change line 196 (`let frozenMac = null;`) to add a new state variable and a color map right after it:

```javascript
let frozenMac = null;
let hasFitBounds = false;
```

becomes:

```javascript
let frozenMac = null;
let hasFitBounds = false;
// Hits from the multi-band spectrum scanner (Component F). Unlike `devices`
// (always the full current set on every WS message), the server only ever
// sends *new* hits after the initial connect snapshot -- see the
// `msg.type === 'spectrum_hits'` handler below for why this gets appended
// to, not replaced, except on an explicit reset.
let spectrumHits = [];
const MAX_SPECTRUM_HITS = 5000; // client-side cap so a long mission doesn't grow this unbounded
const BAND_COLORS = {
  cellular_low: '#ffa500',
  cellular_mid: '#ffa500',
  ism_2_4ghz: '#b266ff',
  keyfob: '#ffd633',
};
```

(The original `let hasFitBounds = false;` line stays where it is — only the new block is inserted after it.)

- [ ] **Step 3: Add the Leaflet layer and its toggle**

Change:

```javascript
const markerLayer = L.layerGroup().addTo(map);
const heatLayer = L.layerGroup();
const missionPatternLayer = L.layerGroup().addTo(map);
```

to:

```javascript
const markerLayer = L.layerGroup().addTo(map);
const heatLayer = L.layerGroup();
const spectrumLayer = L.layerGroup();
const missionPatternLayer = L.layerGroup().addTo(map);
```

Change:

```javascript
document.getElementById('night-toggle').addEventListener('click', () => {
  document.documentElement.classList.toggle('night');
});
filterNew.addEventListener('change', render);
showHeatmap.addEventListener('change', () => {
  if (showHeatmap.checked) heatLayer.addTo(map); else map.removeLayer(heatLayer);
  render();
});
```

to:

```javascript
document.getElementById('night-toggle').addEventListener('click', () => {
  document.documentElement.classList.toggle('night');
});
filterNew.addEventListener('change', render);
showHeatmap.addEventListener('change', () => {
  if (showHeatmap.checked) heatLayer.addTo(map); else map.removeLayer(heatLayer);
  render();
});
const showSpectrum = document.getElementById('show-spectrum');
showSpectrum.addEventListener('change', () => {
  if (showSpectrum.checked) spectrumLayer.addTo(map); else map.removeLayer(spectrumLayer);
  renderSpectrumHits();
});
```

- [ ] **Step 4: Add the `renderSpectrumHits` function**

After the existing `updateHeatmap` function (ends around line 508, right before `function metersPerDegLon`), add:

```javascript
function renderSpectrumHits() {
  spectrumLayer.clearLayers();
  if (!showSpectrum.checked) return;
  for (const h of spectrumHits) {
    if (h.lat == null || h.lon == null) continue;
    const color = BAND_COLORS[h.band] || '#ffffff';
    L.circleMarker([h.lat, h.lon], {
      radius: 5,
      color,
      fillColor: color,
      fillOpacity: 0.7,
      weight: 1,
    }).bindTooltip(
      `${h.band} @ ${(h.freq_hz / 1e6).toFixed(1)} MHz\n${h.power_dbm.toFixed(1)} dBm (baseline ${h.baseline_dbm.toFixed(1)})`
    ).addTo(spectrumLayer);
  }
}
```

- [ ] **Step 5: Handle the WebSocket message**

Change the `ws.onmessage` handler:

```javascript
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'devices') {
      devices = msg.devices;
      if (msg.devices.length === 0) {
        // Only happens right after a reset -- nothing left to stay frozen on.
        hasFitBounds = false;
        setFrozen(null);
      }
      if (frozenMac !== null) {
        // state is current in `devices` already; the DOM just isn't
        // repainted until the label field blurs, so in-progress typing
        // doesn't get disturbed.
      } else if (hasTableSelection()) {
        pendingRender = true;
      } else {
        render();
      }
    }
  };
```

to:

```javascript
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'devices') {
      devices = msg.devices;
      if (msg.devices.length === 0) {
        // Only happens right after a reset -- nothing left to stay frozen on.
        hasFitBounds = false;
        setFrozen(null);
      }
      if (frozenMac !== null) {
        // state is current in `devices` already; the DOM just isn't
        // repainted until the label field blurs, so in-progress typing
        // doesn't get disturbed.
      } else if (hasTableSelection()) {
        pendingRender = true;
      } else {
        render();
      }
    } else if (msg.type === 'spectrum_hits') {
      // The initial connect snapshot and every later poll-loop delta both
      // arrive as this same message type -- reset:true (only sent by
      // /api/reset) is the one case that replaces instead of appends.
      spectrumHits = msg.reset ? msg.hits : spectrumHits.concat(msg.hits);
      if (spectrumHits.length > MAX_SPECTRUM_HITS) {
        spectrumHits = spectrumHits.slice(spectrumHits.length - MAX_SPECTRUM_HITS);
      }
      renderSpectrumHits();
    }
  };
```

- [ ] **Step 6: Update the reset confirmation copy**

Change:

```javascript
  if (!confirm('This permanently deletes ALL detections and tags. Reset the database?')) return;
```

to:

```javascript
  if (!confirm('This permanently deletes ALL detections, spectrum hits, and tags. Reset the database?')) return;
```

- [ ] **Step 7: Verify in a real browser**

Start the stack (or just the ground-station against the same test DB from Task 9's Step 5), open `http://localhost:8080`, and confirm:
- A "Spectrum hits" checkbox appears next to "Hit density heatmap".
- With the seeded test row still in the DB (from Task 9's Step 5) and the checkbox checked, a colored dot appears on the map near lat 45.1 / lon -122.2, and hovering it shows a tooltip with `keyfob @ 433.9 MHz`.
- Unchecking the box hides the dot; rechecking shows it again.
- Clicking "Reset Database" and confirming clears it.

If browser automation tools are available in this environment, use them to drive this check end-to-end instead of asking the user to do it by hand; otherwise ask the user to confirm the four bullet points above.

- [ ] **Step 8: Commit**

```bash
git add services/ground_station/app/static/index.html
git commit -m "$(cat <<'EOF'
Add spectrum hits map layer to the ground-station UI

New "Spectrum hits" toggle, off by default, colored by band (cellular
orange, 2.4GHz ISM purple, keyfob yellow). Kept as its own layer rather
than merged into the device table/map markers -- a hit has no MAC-like
identity to tag or search the way a BLE device does.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: Update README.md and docs/ARCHITECTURE.md

**Files:**
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`

**Interfaces:** none (documentation only)

- [ ] **Step 1: Update the Supported hardware table**

In `README.md`, change:

```markdown
| Role | Hardware | Status |
|---|---|---|
| Primary BLE scanner | Sena UD100, or any BlueZ-recognized adapter | Validated end-to-end |
| Secondary BLE scanner | Ubertooth One | Validated end-to-end, including independent CRC-24 verification (see [Ubertooth notes](#ubertooth-notes)) |
| GPS | Any NMEA-capable USB/serial module, via gpsd | Validated end-to-end |

The two scanners are independent and additive, not either/or: run one, the
other, or both at once, and every detection lands in the same shared log
regardless of which radio saw it. Neither is required to be physically
present for the stack to start — see [Running](#running).
```

to:

```markdown
| Role | Hardware | Status |
|---|---|---|
| Primary BLE scanner | Sena UD100, or any BlueZ-recognized adapter | Validated end-to-end |
| Secondary BLE scanner | Ubertooth One | Validated end-to-end, including independent CRC-24 verification (see [Ubertooth notes](#ubertooth-notes)) |
| Multi-band spectrum scanner | HackRF One | Validated end-to-end (see [Spectrum scanner notes](#spectrum-scanner-notes)) |
| GPS | Any NMEA-capable USB/serial module, via gpsd | Validated end-to-end |

The three scanners are independent and additive, not either/or: run any
subset of them at once, and every detection/hit lands in the same shared
log regardless of which radio saw it. None is required to be physically
present for the stack to start — see [Running](#running).
```

- [ ] **Step 2: Update the Contents list**

Change:

```markdown
- [Host prerequisites](#host-prerequisites)
- [Running](#running)
- [Ground-station UI](#ground-station-ui)
- [Configuration](#configuration)
- [Ubertooth notes](#ubertooth-notes)
- [Raspberry Pi deployment](#raspberry-pi-deployment)
- [Known limitations / not yet built](#known-limitations--not-yet-built)
```

to:

```markdown
- [Host prerequisites](#host-prerequisites)
- [Running](#running)
- [Ground-station UI](#ground-station-ui)
- [Configuration](#configuration)
- [Ubertooth notes](#ubertooth-notes)
- [Spectrum scanner notes](#spectrum-scanner-notes)
- [Raspberry Pi deployment](#raspberry-pi-deployment)
- [Known limitations / not yet built](#known-limitations--not-yet-built)
```

- [ ] **Step 3: Add a host prerequisite entry**

After item 4 (Ubertooth One, ending `...ad-hoc testing.)`), add:

```markdown
5. **HackRF One (optional)** — plug it in; confirm with
   `lsusb | grep -i hackrf` (vendor:product `1d50:6089`). Like the
   Ubertooth, it needs no host-side driver setup for the Docker path:
   `scanner-spectrum` runs `privileged: true` for raw USB access, and
   `hackrf_sweep` is built into that container's own image.
```

- [ ] **Step 4: Update the Running section**

Change:

```markdown
This always starts all three services, regardless of which adapters are
physically present — `scanner` and `scanner-ubertooth` each retry quietly
in the background rather than blocking anything if their radio is missing
(see [Known limitations](#known-limitations--not-yet-built)). One command
works unattended whether the host has the UD100, the Ubertooth, both, or
neither yet — the point of that design is a field SBC that boots this stack
with no one there to pass a flag.
```

to:

```markdown
This always starts all four services, regardless of which adapters are
physically present — `scanner`, `scanner-ubertooth`, and `scanner-spectrum`
each retry quietly in the background rather than blocking anything if their
radio is missing (see [Known limitations](#known-limitations--not-yet-built)).
One command works unattended whether the host has any, all, or none of the
UD100/Ubertooth/HackRF yet — the point of that design is a field SBC that
boots this stack with no one there to pass a flag.
```

- [ ] **Step 5: Add a Ground-station UI bullet**

After the existing "Night mode" bullet (last one in the list), add:

```markdown
- **Spectrum hits** layer (off by default) plots hits from the multi-band
  spectrum scanner, colored by band (cellular orange, 2.4GHz ISM purple,
  keyfob yellow). A hit is energy above that band's per-flight calibrated
  baseline in a frequency range commercial devices use — not a decoded,
  identified device the way a BLE detection is, so it's an investigatory
  lead, not proof: worth a quick look by the ground team, same as an
  unidentified BLE device is today. See [Spectrum scanner
  notes](#spectrum-scanner-notes).
```

- [ ] **Step 6: Add a Configuration table for scanner-spectrum**

After the `scanner-ubertooth` table (ends with the `UBERTOOTH_DEVICE_INDEX` row), add:

```markdown
**scanner-spectrum**

| Var | Default | Meaning |
|---|---|---|
| `DB_PATH` | `/data/detections.sqlite3` | Same SQLite file as the other scanners (shared via the `./data` volume) |
| `GPSD_HOST` / `GPSD_PORT` | `127.0.0.1` / `2947` | Same as the other scanners |
| `SOURCE_UNIT_ID` | `ground-logger-spectrum-01` | Kept distinct so this unit's hits are separately attributable once merged |
| `SPECTRUM_CALIBRATION_S` | `10` | Seconds to sample each band at startup when establishing that flight's baseline |
| `SPECTRUM_DWELL_S` | `5` | Seconds to sweep each band per pass once calibration is done |
| `SPECTRUM_HIT_MARGIN_DB` | `10` | How far above a band's calibrated baseline a reading must be to count as a hit |
```

- [ ] **Step 7: Add a "Spectrum scanner notes" section**

After the "## Ubertooth notes" section (ends `...verified against the packet's raw on-air bytes rather than the tool's own flag.`) and before "## Raspberry Pi deployment", add:

```markdown
## Spectrum scanner notes

`scanner-spectrum` doesn't decode a protocol the way the two BLE scanners
do — it wraps `hackrf_sweep` (built from source in the container, same
build-from-upstream rationale as the Ubertooth tools) and does simple
energy-threshold detection across a fixed default list of bands: cellular
(698–960 MHz and 1710–2200 MHz — a phone with no signal periodically
transmits high-power bursts searching for a tower, often the single
strongest signature available), the shared 2.4 GHz ISM band (WiFi/BLE/
AirTag), and sub-GHz keyfobs (300–450 MHz, covering both 315 and
433.92 MHz remotes). 5 GHz WiFi is intentionally left out of the default
list — personal hotspots generally favor 2.4 GHz for range.

On startup it samples each band for `SPECTRUM_CALIBRATION_S` seconds to
establish that flight's own ambient-noise baseline before any drone motion
is assumed to have started, then sweeps the bands forever, flagging any
reading `SPECTRUM_HIT_MARGIN_DB` above its band's baseline as a hit. This
is a per-flight baseline, not a continuously adapting one, by design — see
`docs/superpowers/specs/2026-08-05-multiband-spectrum-scanner-design.md`
for the full rationale, including why a single wideband panel antenna and
simple threshold detection (no burst-shape classification) were chosen
over the alternatives.

A hit is not a decoded, identified device — it's "energy above baseline in
a band commercial devices use," logged with band/frequency/power/GPS into
its own `spectrum_hits` table (not merged into `detections`, since a hit
has no persistent MAC-like identity to tag or track across sightings the
way a BLE device does).
```

- [ ] **Step 8: Update the privileged-mode limitation**

Change:

```markdown
- `privileged: true` on both scanners is broader than strictly necessary; it
  can be narrowed once the host's D-Bus BlueZ policy is tuned (for
  `scanner`) and the exact USB caps are pinned down (for
  `scanner-ubertooth`). The ground-station service needs neither.
```

to:

```markdown
- `privileged: true` on all three scanners is broader than strictly
  necessary; it can be narrowed once the host's D-Bus BlueZ policy is tuned
  (for `scanner`) and the exact USB caps are pinned down (for
  `scanner-ubertooth` and `scanner-spectrum`). The ground-station service
  needs neither.
```

Also change:

```markdown
- `scanner` and `scanner-ubertooth` both retry every 5s rather than exit if
  their adapter is missing or disappears mid-run, which is what makes
  unattended boot safe (see [Running](#running)) — but neither currently
  distinguishes "adapter missing" from other startup failures in its retry
  log line, so if scanning silently never starts, check the container logs
  for what the underlying error actually is rather than assuming it's just
  a missing adapter.
```

to:

```markdown
- All three scanners retry every 5s rather than exit if their adapter is
  missing or disappears mid-run, which is what makes unattended boot safe
  (see [Running](#running)) — but none currently distinguishes "adapter
  missing" from other startup failures in its retry log line, so if
  scanning silently never starts, check the container logs for what the
  underlying error actually is rather than assuming it's just a missing
  adapter.
- The spectrum scanner's per-band frequency ranges are US-centric defaults
  (e.g. keyfob covers 315/433.92 MHz, cellular covers US LTE low/mid
  bands) — deployments elsewhere may need different ranges for their local
  spectrum allocation.
```

- [ ] **Step 9: Add Component F to ARCHITECTURE.md**

In `docs/ARCHITECTURE.md`, after Component E's "Coverage reality check" table and its closing paragraph (ends `...treat the full-box sweep as a fallback, not the default plan.`) and before "## 3. Data Model", add:

```markdown
### F. Multi-Band Spectrum Scanner (HackRF, Phase 1 supplement)

A different, additive idea from Component E above: rather than trying to
see the whole BLE-shaped RF picture in one wideband capture, this sweeps a
fixed list of narrower, disjoint bands that *other* commercial device
categories use — cellular, WiFi, the shared 2.4 GHz ISM band, and sub-GHz
keyfobs — accepting sequential retuning as a deliberate tradeoff instead of
requiring ≥80 MHz instantaneous bandwidth hardware. Built with a HackRF One
(20 MHz instantaneous bandwidth, already in the BOM) running `hackrf_sweep`,
a single wideband downward-facing panel antenna, and simple
energy-threshold detection against a per-flight calibrated baseline — no
burst-shape classification. A hit is an investigatory lead ("something's
transmitting here"), not an identified device, feeding the same Phase 2
cueing step as Components A/B/D. Implemented as its own service
(`services/scanner-spectrum/`); see
`docs/superpowers/specs/2026-08-05-multiband-spectrum-scanner-design.md`
for the full design, including the default band list and why 5 GHz WiFi
and burst-shape classification were left out for now.
```

- [ ] **Step 10: Update the Data Model section**

Change:

```markdown
## 3. Data Model

Every detection is one record, same schema across all units so logs merge
cleanly at the ground station:

```
timestamp_utc, lat, lon, alt_m, source_unit_id, mac_or_uuid,
device_name, rssi_dbm, tx_power_dbm (if advertised), adv_raw_hex
```

Component E's unclassified RF hits use the same table with `mac_or_uuid`,
`device_name`, and `adv_raw_hex` left null and `rssi_dbm` populated from the
energy-detector's power estimate — they cluster into the Phase 2 heatmap
identically to tagged/unknown device hits, just with nothing to show in the
name/MAC columns.
```

to:

```markdown
## 3. Data Model

Every BLE detection (Components A/B/D) is one record, same schema across
all units so logs merge cleanly at the ground station:

```
timestamp_utc, lat, lon, alt_m, source_unit_id, mac_or_uuid,
device_name, rssi_dbm, tx_power_dbm (if advertised), adv_raw_hex
```

Component F's spectrum hits are **not** stored in this table — as built,
they live in a separate `spectrum_hits` table (`timestamp_utc,
source_unit_id, band, freq_hz, power_dbm, baseline_dbm, lat, lon, alt_m,
gps_fix_age_s`), since a hit has no MAC/device-name identity to fill those
columns with and forcing it into the device-shaped schema would just
produce null-heavy rows. This diverges from this section's original sketch
(which assumed Component E's unclassified hits would reuse the `detections`
table with `mac_or_uuid`/`device_name` left null) — the ground-station UI
renders them as an independent map layer instead of folding them into the
same device table/heatmap. If Component E (wideband simultaneous-capture
energy detection) is ever built, revisit whether it shares Component F's
`spectrum_hits` table or needs its own.
```

- [ ] **Step 11: Update the Hardware Sketch table**

In the Hardware Sketch table, after the "SDR, wide-area energy detector (E, optional v2)" row, add:

```markdown
| SDR, multi-band spectrum scanner (F) | HackRF One | Built and validated — see `services/scanner-spectrum/`. 20 MHz instantaneous bandwidth is enough here since it sweeps disjoint narrow bands sequentially rather than capturing the whole ISM band at once |
```

- [ ] **Step 12: Update the Suggested Build Order**

After item 5 ("Wide-area RF energy detector (E)..."), add:

```markdown
6. Multi-band spectrum scanner (F) — independent of (E), built using the
   HackRF already in hand. See
   `docs/superpowers/plans/2026-08-05-multiband-spectrum-scanner.md` for
   the implementation plan.
```

- [ ] **Step 13: Commit**

```bash
git add README.md docs/ARCHITECTURE.md
git commit -m "$(cat <<'EOF'
Document Component F (multi-band spectrum scanner) in README/ARCHITECTURE

Adds HackRF to Supported hardware/Configuration/host-prereqs, a new
"Spectrum scanner notes" section mirroring the existing Ubertooth one,
and an ARCHITECTURE.md Component F writeup distinguishing it from
Component E's original wideband-simultaneous-capture sketch (which
remains unbuilt) -- including the Data Model divergence now that
spectrum hits are their own table rather than reusing `detections`.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
