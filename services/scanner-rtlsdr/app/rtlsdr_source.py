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


import asyncio
import logging
import time

logger = logging.getLogger(__name__)

DEFAULT_MARGIN_DB = 10.0
DEFAULT_CALIBRATION_S = 15.0
DEFAULT_DWELL_S = 8.0


async def _run_rtl_power(
    low_hz: int, high_hz: int, duration_s: float, bin_hz: int = 1_000_000
) -> list[tuple[int, float]]:
    """Run one rtl_power single-shot sweep over [low_hz, high_hz) for one
    ~duration_s reporting interval and return every (freq_hz, power_dbm)
    reading collected.

    Unlike hackrf_sweep (which never exits on its own while sweeping),
    rtl_power's -1 (single-shot) mode is *designed* to exit on its own
    after one -i-second interval -- verified against real hardware. A
    genuine device failure was verified to fail almost instantly (tens of
    ms, exit code 1, zero stdout) by contrast, so "exited far sooner than
    duration_s could plausibly allow" is this function's failure signal,
    not "exited at all."

    rtl_power does not stream output incrementally -- verified: stdout
    stays empty until the whole pass completes -- so this collects
    everything via proc.communicate() rather than reading line-by-line the
    way hackrf_sweep's wrapper does.
    """
    args = ["rtl_power", "-f", f"{low_hz}:{high_hz}:{bin_hz}", "-i", str(duration_s), "-1", "-"]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    start = time.monotonic()
    # rtl_power was verified to delay exiting after SIGTERM until it
    # finishes its current scan pass ("Signal caught, finishing scan
    # pass."), so this grace period is sized off duration_s itself rather
    # than a small fixed constant -- a short fixed grace would routinely
    # SIGKILL a healthy process still finishing its pass.
    budget_s = duration_s + max(duration_s, 5.0)
    try:
        stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=budget_s)
    except asyncio.TimeoutError:
        proc.terminate()
        try:
            stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=duration_s + 5.0)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            except asyncio.TimeoutError:
                raise OSError(
                    f"rtl_power (pid={proc.pid}) did not exit even after SIGKILL "
                    f"while sweeping {low_hz}:{high_hz} Hz"
                ) from None

    elapsed = time.monotonic() - start
    readings: list[tuple[int, float]] = []
    for raw_line in stdout_data.decode(errors="replace").splitlines():
        readings.extend(_readings_from_line(raw_line))

    # A real device-gone failure was verified to fail in ~58ms; a real
    # sweep, even a short one, takes a meaningful fraction of duration_s.
    # 50% margin distinguishes "fast-failed" from "legitimately quick."
    if proc.returncode not in (0, None) and elapsed < duration_s * 0.5:
        stderr_text = stderr_data.decode(errors="replace").strip() or "(no stderr output captured)"
        raise OSError(
            f"rtl_power exited early (returncode={proc.returncode}, after "
            f"{elapsed:.2f}s of a {duration_s:.0f}s window) while sweeping "
            f"{low_hz}:{high_hz} Hz -- device likely unavailable: {stderr_text}"
        )

    return readings


async def _sweep_band(band: Band, duration_s: float) -> list[tuple[int, float]]:
    """Run _run_rtl_power for one band, retrying forever every 5s if the
    binary is missing or the sweep fails -- same reconnect philosophy as
    GpsClient.run() and Component F's _sweep_band.
    """
    while True:
        try:
            return await _run_rtl_power(band.low_hz, band.high_hz, duration_s)
        except FileNotFoundError:
            logger.error("rtl_power not found on PATH; is the rtl-sdr package installed?")
            await asyncio.sleep(5)
        except OSError as exc:
            logger.warning("rtl_power failed for band %s (%s); retrying in 5s", band.name, exc)
            await asyncio.sleep(5)


async def stream_hits(
    margin_db: float = DEFAULT_MARGIN_DB,
    calibration_s: float = DEFAULT_CALIBRATION_S,
    dwell_s: float = DEFAULT_DWELL_S,
    bands: list[Band] | None = None,
):
    """Calibrate a per-frequency-bin baseline across all bands once, then
    sweep them forever, yielding SpectrumHitReading for every in-band
    reading that exceeds its own bin's calibrated baseline + margin_db.

    Mirrors Component F's already-hardened stream_hits (per-bin baseline,
    out-of-band filtering, retry-forever on zero-reading calibration) --
    see docs/superpowers/specs/2026-08-06-rtlsdr-spectrum-scanner-design.md
    Section 5.
    """
    active_bands = bands if bands is not None else DEFAULT_BANDS

    baseline_by_freq: dict[str, dict[int, float]] = {}
    fallback_baseline: dict[str, float] = {}
    logger.info("calibrating baseline for %d bands (%.0fs each)...", len(active_bands), calibration_s)
    for band in active_bands:
        while True:
            readings = _readings_in_band(await _sweep_band(band, calibration_s), band)
            if readings:
                break
            logger.warning("zero in-band readings calibrating %s; retrying in 5s", band.name)
            await asyncio.sleep(5)
        powers = [p for _, p in readings]
        baseline_by_freq[band.name] = average_power_by_freq(readings)
        fallback_baseline[band.name] = average_power(powers)
        logger.info(
            "baseline[%s] = %.1f dBm avg across %d bins (%d samples)",
            band.name, fallback_baseline[band.name], len(baseline_by_freq[band.name]), len(powers),
        )

    while True:
        for band in active_bands:
            readings = _readings_in_band(await _sweep_band(band, dwell_s), band)
            for hit in detect_hits(
                readings, band.name, baseline_by_freq[band.name], margin_db, fallback_baseline[band.name]
            ):
                yield hit
