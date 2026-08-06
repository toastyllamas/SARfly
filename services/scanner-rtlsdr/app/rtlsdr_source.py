"""Multi-band spectrum energy-detection source, backed by rtl_power.

rtl_power (ships with rtl-sdr host tools) natively retunes across a given
frequency range and streams power-per-bin readings as CSV lines -- this module
wraps it as a subprocess (see the calibration/sweep functions appended in a
later change) rather than hand-rolling IQ capture and FFT in Python, the same
"wrap the purpose-built CLI tool" pattern ubertooth_source.py uses for
ubertooth-btle.

This half of the module is pure logic (line parsing, band bucketing, threshold
detection) with no I/O, kept separate from the subprocess/asyncio plumbing so
it can be unit tested directly. See
docs/superpowers/specs/2026-08-06-rtlsdr-spectrum-scanner-design.md for the
full design and band-list rationale.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Band:
    name: str
    low_hz: int
    high_hz: int


# Scoped to what the RTL-SDR's R820T tuner can actually and fully cover
# (~24 MHz-1766 MHz, verified against real hardware) -- see the design
# doc Section 3 for why cellular_mid and ism_2_4ghz are intentionally
# absent here rather than truncated to a same-named-but-different range.
DEFAULT_BANDS: list[Band] = [
    Band("keyfob", 300_000_000, 450_000_000),
    Band("cellular_low", 698_000_000, 960_000_000),
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


def _readings_in_band(readings: list[tuple[int, float]], band: Band) -> list[tuple[int, float]]:
    """Keep only readings whose frequency actually falls within this band's
    declared [low_hz, high_hz) range.

    hackrf_sweep tunes in whole-MHz steps and _run_hackrf_sweep ceils the
    high edge so a band's requested range is always fully covered (see its
    docstring) -- which means it routinely returns readings past a band's
    declared edge (e.g. the keyfob band's 450.0 MHz upper edge sweeping out
    to ~459.5 MHz). Those out-of-range readings must be discarded here, not
    folded into this band's baseline or hit detection under this band's
    name.
    """
    return [(freq, power) for freq, power in readings if band.low_hz <= freq < band.high_hz]


def average_power(powers: list[float]) -> float:
    return sum(powers) / len(powers) if powers else 0.0


def average_power_by_freq(readings: list[tuple[int, float]]) -> dict[int, float]:
    """Group calibration readings by their exact frequency bin and average
    each bin's readings separately, producing a per-bin baseline rather
    than one scalar per band.

    Real spectrum isn't flat: a bin sitting on a permanent carrier (a cell
    tower, a WiFi AP) is reliably far above its band's *average* power, so
    comparing every bin in the band against one band-wide scalar baseline
    makes that bin hit forever while masking a genuine anomaly elsewhere in
    the band that never rises above the noisy average.
    """
    by_freq: dict[int, list[float]] = {}
    for freq, power in readings:
        by_freq.setdefault(freq, []).append(power)
    return {freq: average_power(powers) for freq, powers in by_freq.items()}


def detect_hits(
    readings: list[tuple[int, float]],
    band_name: str,
    baseline_by_freq: dict[int, float],
    margin_db: float,
    fallback_baseline_dbm: float,
) -> list[SpectrumHitReading]:
    """Flag readings that exceed THEIR OWN frequency bin's calibrated
    baseline + margin_db (not a single per-band scalar baseline).

    fallback_baseline_dbm (that band's overall calibrated mean) is used
    only if a swept frequency wasn't seen during calibration -- shouldn't
    normally happen since hackrf_sweep's bin layout is deterministic for a
    fixed frequency-range, but this is a safety net rather than a KeyError.
    """
    hits = []
    for freq, power in readings:
        baseline_dbm = baseline_by_freq.get(freq, fallback_baseline_dbm)
        if power > baseline_dbm + margin_db:
            hits.append(
                SpectrumHitReading(band=band_name, freq_hz=freq, power_dbm=power, baseline_dbm=baseline_dbm)
            )
    return hits
