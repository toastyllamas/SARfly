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
import math

logger = logging.getLogger(__name__)

DEFAULT_MARGIN_DB = 10.0
DEFAULT_CALIBRATION_S = 10.0
DEFAULT_DWELL_S = 5.0


async def _log_stderr(stream: asyncio.StreamReader | None, captured: list[str]) -> None:
    if stream is None:
        return
    async for raw_line in stream:
        text = raw_line.decode(errors="replace").rstrip("\n")
        captured.append(text)
        logger.warning("hackrf_sweep: %s", text)


async def _run_hackrf_sweep(low_hz: int, high_hz: int, duration_s: float) -> list[tuple[int, float]]:
    """Run hackrf_sweep over [low_hz, high_hz) for duration_s seconds and
    return every (center_freq_hz, power_dbm) reading collected.

    hackrf_sweep takes its range as whole MHz; floor the low edge and
    ceil the high edge so the requested band is always fully covered
    even when its bounds aren't MHz-aligned (e.g. the ISM band's
    2483.5 MHz upper edge).

    hackrf_sweep never exits on its own while sweeping a range -- it runs
    until killed. So if stdout hits EOF (the read loop completes) before
    duration_s elapses -- i.e. asyncio.wait_for below does NOT raise
    TimeoutError -- that is itself a failure signal (device not attached,
    unplugged mid-flight, USB reset, etc.), regardless of the process's
    exit code. Without this check, that failure mode looked identical to a
    clean, complete, empty sweep: _sweep_band's retry-forever backoff (which
    only catches FileNotFoundError/OSError) would never trigger, and
    stream_hits would calibrate a bogus 0.0 dBm baseline from the empty
    reading list, leaving the scanner permanently deaf even if the hardware
    later recovers.
    """
    low_mhz = low_hz // 1_000_000
    high_mhz = math.ceil(high_hz / 1_000_000)
    args = ["hackrf_sweep", "-f", f"{low_mhz}:{high_mhz}"]

    readings: list[tuple[int, float]] = []
    stderr_lines: list[str] = []
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stderr_task = asyncio.create_task(_log_stderr(proc.stderr, stderr_lines))

    async def _read() -> None:
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            readings.extend(_readings_from_line(raw_line.decode(errors="replace")))

    timed_out = False
    try:
        await asyncio.wait_for(_read(), timeout=duration_s)
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        stderr_task.cancel()
        # Capture the exit code as it stood the moment the read loop ended,
        # before we potentially force-kill a still-running (healthy) process
        # below -- terminate() always leaves a non-zero/negative returncode,
        # which must not be mistaken for a failure signal on the happy path.
        exited_returncode = proc.returncode
        if proc.returncode is None:
            proc.terminate()
            await proc.wait()

    if not timed_out or exited_returncode not in (None, 0):
        stderr_context = " | ".join(stderr_lines) or "(no stderr output captured)"
        raise OSError(
            f"hackrf_sweep exited early (returncode={exited_returncode}) while sweeping "
            f"{low_mhz}:{high_mhz} MHz -- device likely unavailable: {stderr_context}"
        )

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
    """Calibrate a per-frequency-bin baseline across all bands once, then
    sweep them forever, yielding SpectrumHitReading for every bin that
    exceeds ITS OWN calibrated baseline + margin_db (see detect_hits /
    average_power_by_freq for why a per-band scalar baseline is the wrong
    comparison -- real spectrum isn't flat).

    Readings are filtered to each band's own declared [low_hz, high_hz)
    range before being used for calibration or hit detection, since
    hackrf_sweep over-scans past a band's declared edge (see
    _readings_in_band) and those readings must not be mislabeled into this
    band.
    """
    active_bands = bands if bands is not None else DEFAULT_BANDS

    baseline_by_freq: dict[str, dict[int, float]] = {}
    fallback_baseline: dict[str, float] = {}
    logger.info("calibrating baseline for %d bands (%.0fs each)...", len(active_bands), calibration_s)
    for band in active_bands:
        while True:
            readings = _readings_in_band(await _sweep_band(band, calibration_s), band)
            powers = [p for _, p in readings]
            if powers:
                break
            # A device that never recovers means calibration never
            # completes -- that's correct: better to never start sweeping
            # than to sweep with a fake 0.0 dBm baseline that would make
            # the scanner permanently deaf even after the hardware
            # recovers (calibration only ever runs once, up front).
            logger.warning(
                "calibration for band %s yielded zero in-band readings; "
                "retrying in 5s rather than accepting a bogus 0.0 dBm baseline",
                band.name,
            )
            await asyncio.sleep(5)

        baseline_by_freq[band.name] = average_power_by_freq(readings)
        fallback_baseline[band.name] = average_power(powers)
        logger.info(
            "baseline[%s] = %.1f dBm avg across %d bins (%d samples)",
            band.name,
            fallback_baseline[band.name],
            len(baseline_by_freq[band.name]),
            len(powers),
        )

    while True:
        for band in active_bands:
            readings = _readings_in_band(await _sweep_band(band, dwell_s), band)
            for hit in detect_hits(
                readings, band.name, baseline_by_freq[band.name], margin_db, fallback_baseline[band.name]
            ):
                yield hit
