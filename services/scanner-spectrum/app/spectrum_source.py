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
