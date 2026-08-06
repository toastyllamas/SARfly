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


# rtl_power CSV line: date, time, hz_low, hz_high, hz_bin_width,
# num_samples, dB, dB, dB, ... (one dB reading per bin across the segment) --
# verified identical in shape to hackrf_sweep's own CSV format (Component
# F's spectrum_source.py), which is why this regex was copyable verbatim.
_LINE_RE = re.compile(
    r"^[\d-]+,\s*[\d:.]+,\s*(?P<low>\d+),\s*(?P<high>\d+),\s*"
    r"(?P<binw>[\d.]+),\s*(?P<n>\d+),\s*(?P<rest>.+)$"
)


def parse_sweep_line(line: str) -> tuple[int, int, float, list[float]] | None:
    """Parse one rtl_power CSV output line into
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

    Unlike hackrf_sweep (which routinely over-scans past a band's declared
    edge -- see Component F's identical-named filter and its docstring),
    rtl_power was verified NOT to do this for this module's two bands:
    real sweeps produced exactly 150 bins for the 300-450 MHz keyfob range
    and exactly 262 bins for the 698-960 MHz cellular_low range, matching
    each band's declared width with no overshoot (Task 7). This filter is
    kept anyway as a harmless safety net -- copied verbatim from Component
    F along with the rest of the pure-logic half of this module -- rather
    than removed on the assumption that no future rtl_power version or
    argument combination will ever over-scan.
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
    normally happen since rtl_power's bin layout is deterministic for a
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
# Verified against real hardware (Task 7): rtl_power routinely overshoots
# the requested -i interval -- a real cellular_low sweep (262-283 1MHz
# hops) took 18.5-21.1s real time against -i 8/-i 15 alike, not the
# requested duration. These defaults are sized with real margin above
# that observed worst case, not the nominal "-i" value, so a normal,
# healthy sweep never nears the SIGTERM escalation path in
# _run_rtl_power (which is safe either way, but wastes a whole sweep's
# worth of data if triggered on a healthy-but-slow process -- see that
# function's docstring).
DEFAULT_CALIBRATION_S = 25.0
DEFAULT_DWELL_S = 20.0


async def _log_stderr(stream: asyncio.StreamReader | None, captured: list[str]) -> None:
    if stream is None:
        return
    async for raw_line in stream:
        text = raw_line.decode(errors="replace").rstrip("\n")
        captured.append(text)
        logger.warning("rtl_power: %s", text)


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

    rtl_power's real -i flag rounds to an integer (verified against
    hardware) -- round duration_s rather than passing it through verbatim,
    so a fractional or sub-1-second duration_s doesn't silently collapse
    to "-i 0".

    Two Critical bugs were found (and fixed here) in an earlier version of
    this function that collected output via a single `await
    proc.communicate()` call:

    1. Readings not surviving cancellation: `communicate()` is atomic --
       if the surrounding `asyncio.wait_for` times out and cancels it,
       everything communicate() had already read from the pipe is thrown
       away, not returned. rtl_power was verified to delay honoring
       SIGTERM until it finishes its current scan pass, so hitting the
       escalation path is routine, not rare -- meaning a normal,
       slightly-slow-to-exit sweep could come back with zero readings
       instead of the ones it actually produced. Fixed by appending each
       parsed line to `readings` (an accumulator outside the read
       coroutine) as it's read via `async for raw_line in proc.stdout`,
       the same shape _run_hackrf_sweep uses -- only whatever is still
       unread in the OS pipe buffer at the moment of cancellation is ever
       lost, which is unavoidable regardless of read strategy. rtl_power
       batching all its output right before exiting (rather than
       streaming progressively) doesn't require a different reading
       mechanism; `async for` over the stream works the same either way.

    2. Orphaned subprocess on outer cancellation: there was no
       try/finally wrapping the process lifecycle, so if the coroutine
       running this function was itself cancelled (e.g. service
       shutdown), the child rtl_power process was never terminated --
       and worse, the orphan keeps the RTL-SDR USB device claimed, so a
       restarted scanner fails to reopen it. Fixed by putting the
       terminate/kill escalation inside the `finally` block itself
       (guarded by `proc.returncode is None`, i.e. still running) rather
       than inside `except asyncio.TimeoutError` -- a genuine outer
       cancellation raises CancelledError, not TimeoutError, so escalation
       logic living only in the except clause would never run on outer
       cancellation and the process would still leak. Python guarantees
       `finally` runs regardless of which exception (or none) triggered
       it, so putting cleanup there closes that gap for both the
       "rtl_power itself is slow to exit" case and the "our own caller
       got cancelled" case.

    Two more were found in review of the fix above:

    3. The exit code used for the failure check must be captured the
       moment the read loop ends, before we potentially terminate/kill a
       still-running (healthy, just slow) process below -- otherwise our
       own SIGTERM/SIGKILL (-15/-9) becomes the "failure" signal, on a
       device that was never actually unavailable. Matches
       _run_hackrf_sweep's identical `exited_returncode` capture.

    4. A second cancellation arriving while we're waiting out the SIGTERM
       grace period should not skip straight past `proc.kill()` -- that
       would leave a SIGTERM-ignoring process still holding the USB
       device, the exact harm bug 2 above was about. A best-effort guard
       is in place below (catch CancelledError here too, kill, then
       re-raise), but empirically this specific double-cancellation race
       is NOT fully closed by it: asyncio's subprocess transport can end
       up in a state, after the first cancelled wait_for(proc.wait()),
       where a subsequent proc.kill() silently does nothing even though
       the real OS process is still alive. This is a narrow, low-probability
       edge case (needs two cancellations landing within the same ~5s+
       window during shutdown) inherited from the same underlying shape
       in Component F's _run_hackrf_sweep, not something unique to this
       function -- tracked as a known limitation rather than chased
       further here; see README's Known limitations.
    """
    args = [
        "rtl_power", "-f", f"{low_hz}:{high_hz}:{bin_hz}",
        "-i", str(max(1, round(duration_s))), "-1", "-",
    ]

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

    start = time.monotonic()
    # rtl_power was verified to delay exiting after SIGTERM until it
    # finishes its current scan pass ("Signal caught, finishing scan
    # pass."), so this grace period is sized off duration_s itself rather
    # than a small fixed constant -- a short fixed grace would routinely
    # SIGKILL a healthy process still finishing its pass.
    #
    # Real-hardware timing (Task 7) showed rtl_power's actual runtime does
    # NOT track the requested -i value linearly or predictably across
    # bands: keyfob measured ~10.9s at -i 8 and ~21.1s at -i 15 (roughly
    # linear, ~1.46s of real time per requested second), while
    # cellular_low measured ~18.5s at BOTH -i 8 and -i 15 (a
    # hardware/USB-bus-imposed floor, independent of -i). The original
    # `duration_s + max(duration_s, 5.0)` -- exactly 2x duration_s -- was
    # found to leave only ~30% real margin at the shipped 25s/20s
    # defaults once keyfob's linear trend is extrapolated (~35.7s/28.4s
    # respectively), not the "comfortable margin" originally intended.
    # `2*duration_s + 10` gives real margin at both the shipped defaults
    # (50-60s budget vs. ~28-36s extrapolated worst case) AND stays cheap
    # for the small duration_s values this module's own fast unit tests
    # use (e.g. 0.1s -> 10.2s budget, not a large fixed floor that would
    # make every escalation-path test slow regardless of duration_s).
    # This is deliberately decoupled from duration_s scaling rather than
    # just raising duration_s itself: the budget is a pure safety net
    # (free on the happy path), whereas duration_s controls both
    # calibration accuracy and the dwell loop's per-cycle band-revisit
    # rate, which matters for a moving DF unit and shouldn't be inflated
    # just to buy timeout margin.
    budget_s = duration_s * 2 + 10.0
    try:
        await asyncio.wait_for(_read(), timeout=budget_s)
    except asyncio.TimeoutError:
        pass
    finally:
        stderr_task.cancel()
        # Capture the exit code as it stood the moment the read loop
        # ended, before we potentially force-kill a still-running
        # (healthy, just slow) process below -- terminate()/kill() always
        # leave a non-zero/negative returncode (-15/-9), which must not be
        # mistaken for a failure signal on a device that was never
        # actually unavailable. Matches _run_hackrf_sweep's identical
        # capture, for the identical reason.
        exited_returncode = proc.returncode
        # Escalation lives in `finally`, not in the `except
        # asyncio.TimeoutError` clause above, so it still runs if THIS
        # coroutine is the one being cancelled (outer shutdown) rather
        # than rtl_power merely running long -- CancelledError is not a
        # TimeoutError, so escalation logic in the except clause alone
        # would silently skip cleanup and orphan the process (Critical 2).
        # Guarding on proc.returncode is None means a process that already
        # exited on its own (the normal happy path) is left alone here.
        if proc.returncode is None:
            proc.terminate()
            try:
                # proc.wait() waits for the actual process exit status,
                # not for the stdout pipe to reach EOF -- unlike
                # communicate(), it can't misdiagnose a process that
                # already died but has a grandchild still holding the
                # write end of the pipe open.
                await asyncio.wait_for(proc.wait(), timeout=duration_s + 5.0)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    raise OSError(
                        f"rtl_power (pid={proc.pid}) did not exit even after SIGKILL "
                        f"while sweeping {low_hz}:{high_hz} Hz"
                    ) from None
            except asyncio.CancelledError:
                # A second cancellation landing while we're waiting out
                # the SIGTERM grace period must not skip straight past
                # proc.kill() and orphan a still-SIGTERM-ignoring process
                # (rtl_power's documented, routine behavior) -- kill it
                # before letting the cancellation propagate.
                proc.kill()
                raise

    elapsed = time.monotonic() - start

    # A real device-gone failure was verified to fail in ~58ms; a real
    # sweep, even a short one, takes a meaningful fraction of duration_s.
    # 50% margin distinguishes "fast-failed" from "legitimately quick."
    # A late failure (past that 50% mark) that still produced usable
    # readings is NOT treated as a failure -- better to use partial real
    # data than discard it and retry from scratch -- but a late failure
    # that produced zero in-range readings is no different from an early
    # one, so it must still raise rather than being silently treated as a
    # successful empty sweep (which would otherwise spin stream_hits's
    # "zero in-band readings" calibration guard forever, or leave the
    # dwell loop silently deaf).
    if exited_returncode not in (0, None) and (elapsed < duration_s * 0.5 or not readings):
        stderr_text = " | ".join(stderr_lines) or "(no stderr output captured)"
        raise OSError(
            f"rtl_power exited early (returncode={exited_returncode}, after "
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
