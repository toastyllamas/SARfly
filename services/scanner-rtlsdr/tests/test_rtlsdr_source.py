"""Tests for pure spectrum-sweep parsing/band/detection logic."""

from __future__ import annotations

from rtlsdr_source import (
    DEFAULT_BANDS,
    Band,
    SpectrumHitReading,
    average_power,
    average_power_by_freq,
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
    from rtlsdr_source import _readings_from_line

    line = "2026-08-05, 14:23:01.123456, 2400000000, 2405000000, 1000.00, 2, -50.0, -60.0"
    assert _readings_from_line(line) == [(2400000500, -50.0), (2400001500, -60.0)]


def test_readings_from_line_malformed_returns_empty_list():
    from rtlsdr_source import _readings_from_line

    assert _readings_from_line("garbage") == []


def test_band_for_freq_matches_keyfob():
    assert band_for_freq(350_000_000) == "keyfob"


def test_band_for_freq_no_match_outside_any_default_band():
    assert band_for_freq(2_450_000_000) is None


def test_band_for_freq_boundary_is_inclusive_low_exclusive_high():
    keyfob = next(b for b in DEFAULT_BANDS if b.name == "keyfob")
    assert band_for_freq(keyfob.low_hz) == "keyfob"
    assert band_for_freq(keyfob.high_hz) != "keyfob"


def test_average_power_empty_returns_zero():
    assert average_power([]) == 0.0


def test_average_power_computes_mean():
    assert average_power([-50.0, -60.0, -40.0]) == -50.0


def test_detect_hits_above_threshold():
    readings = [(2450000000, -40.0), (2451000000, -70.0)]
    baseline_by_freq = {2450000000: -70.0, 2451000000: -70.0}
    hits = detect_hits(
        readings, "test_band", baseline_by_freq, margin_db=10.0, fallback_baseline_dbm=-70.0
    )
    assert hits == [
        SpectrumHitReading(band="test_band", freq_hz=2450000000, power_dbm=-40.0, baseline_dbm=-70.0)
    ]


def test_detect_hits_none_above_threshold():
    readings = [(2450000000, -75.0)]
    baseline_by_freq = {2450000000: -70.0}
    assert (
        detect_hits(readings, "test_band", baseline_by_freq, margin_db=10.0, fallback_baseline_dbm=-70.0)
        == []
    )


def test_detect_hits_uses_fallback_baseline_for_unseen_frequency():
    """A swept frequency that wasn't present in calibration (shouldn't
    normally happen, but guard anyway) should fall back to the band's
    overall calibrated mean rather than KeyError or being silently
    skipped.
    """
    readings = [(999_999_999, -40.0)]
    hits = detect_hits(readings, "test_band", {}, margin_db=10.0, fallback_baseline_dbm=-70.0)
    assert hits == [
        SpectrumHitReading(band="test_band", freq_hz=999_999_999, power_dbm=-40.0, baseline_dbm=-70.0)
    ]


def test_average_power_by_freq_groups_and_averages_per_bin():
    readings = [(100, -40.0), (100, -42.0), (200, -70.0)]
    assert average_power_by_freq(readings) == {100: -41.0, 200: -70.0}


def test_average_power_by_freq_empty_returns_empty_dict():
    assert average_power_by_freq([]) == {}


def test_per_bin_baseline_flags_local_anomaly_and_ignores_noisy_bins_own_level():
    """The core Fix 2 scenario: a bin permanently sitting on a carrier
    (-40dBm) must NOT hit forever just because it matches its own history,
    while a genuinely quiet bin (-70dBm baseline) that jumps 15dB above ITS
    OWN baseline must be flagged -- even though -55dBm is well below the
    noisy bin's level and would have been swallowed whole by the old
    per-band-average scheme (avg(-40,-70) = -55, so neither reading would
    have crossed a -55+10=-45dBm band-wide threshold).
    """
    calibration_readings = [
        (100_000_000, -40.0),
        (100_000_000, -40.0),
        (100_000_000, -40.0),
        (200_000_000, -70.0),
        (200_000_000, -70.0),
        (200_000_000, -70.0),
    ]
    baseline_by_freq = average_power_by_freq(calibration_readings)
    fallback = average_power([p for _, p in calibration_readings])

    sweep_readings = [(100_000_000, -40.0), (200_000_000, -55.0)]
    hits = detect_hits(
        sweep_readings, "test_band", baseline_by_freq, margin_db=10.0, fallback_baseline_dbm=fallback
    )

    assert hits == [
        SpectrumHitReading(band="test_band", freq_hz=200_000_000, power_dbm=-55.0, baseline_dbm=-70.0)
    ]


def test_readings_in_band_filters_out_of_range_frequencies():
    from rtlsdr_source import _readings_in_band

    band = Band("keyfob", 300_000_000, 450_000_000)
    readings = [
        (310_000_000, -50.0),  # in range
        (459_500_000, -60.0),  # out of range -- the reviewer's live-DB example
        (449_999_999, -55.0),  # in range, right at the edge
        (450_000_000, -55.0),  # out of range -- high edge is exclusive
    ]
    assert _readings_in_band(readings, band) == [
        (310_000_000, -50.0),
        (449_999_999, -55.0),
    ]


import asyncio
import os
import time

import pytest


@pytest.fixture
def fake_rtl_power(tmp_path, monkeypatch):
    """Fakes rtl_power's *actual* verified behavior: batches all output at
    once right before exiting (not streamed), and exits successfully on its
    own after roughly its -i interval -- unlike hackrf_sweep, which never
    exits on its own while sweeping.
    """
    script = tmp_path / "rtl_power"
    script.write_text(
        "#!/bin/sh\n"
        "sleep 0.05\n"
        'echo "2026-08-05, 20:11:31, 433000000, 434000000, 7812.50, 71488, -23.61, -24.44"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return script


@pytest.fixture
def fake_rtl_power_device_gone(tmp_path, monkeypatch):
    """Real rtl_power was verified to fail this way when the device index
    doesn't exist: exit code 1, near-instant (tens of ms), zero stdout,
    a "No matching devices found." stderr line.
    """
    script = tmp_path / "rtl_power"
    script.write_text(
        "#!/bin/sh\n"
        "echo 'No matching devices found.' >&2\n"
        "exit 1\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return script


def test_run_rtl_power_collects_readings(fake_rtl_power):
    from rtlsdr_source import _run_rtl_power

    readings = asyncio.run(_run_rtl_power(433_000_000, 434_000_000, duration_s=1.0))
    assert (433003906, -23.61) in readings or any(f == 433003906 for f, _ in readings)


def test_run_rtl_power_rounds_fractional_duration_for_the_dash_i_flag(tmp_path, monkeypatch):
    """rtl_power's real -i flag rounds to an integer (verified against
    hardware) -- a fractional or sub-1-second duration_s must not silently
    collapse to "-i 0". Fakes argv capture by having the fake script dump
    its own arguments to a file rather than acting like rtl_power at all.
    """
    from rtlsdr_source import _run_rtl_power

    argv_file = tmp_path / "argv"
    script = tmp_path / "rtl_power"
    script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argv_file}"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    asyncio.run(_run_rtl_power(433_000_000, 434_000_000, duration_s=0.3))
    argv = argv_file.read_text().splitlines()
    assert "-i" in argv
    assert argv[argv.index("-i") + 1] == "1"  # max(1, round(0.3)) == 1, never "0" or "0.3"

    asyncio.run(_run_rtl_power(433_000_000, 434_000_000, duration_s=2.6))
    argv = argv_file.read_text().splitlines()
    assert argv[argv.index("-i") + 1] == "3"  # round(2.6) == 3, not "2.6"


def test_run_rtl_power_raises_when_binary_missing(tmp_path, monkeypatch):
    from rtlsdr_source import _run_rtl_power

    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        asyncio.run(_run_rtl_power(433_000_000, 434_000_000, duration_s=1.0))


def test_run_rtl_power_raises_when_device_gone(fake_rtl_power_device_gone):
    from rtlsdr_source import _run_rtl_power

    with pytest.raises(OSError, match="device likely unavailable"):
        asyncio.run(_run_rtl_power(433_000_000, 434_000_000, duration_s=2.0))


def test_run_rtl_power_does_not_misflag_a_real_full_duration_sweep(tmp_path, monkeypatch):
    """A real sweep that legitimately takes close to the full requested
    duration (not the fast device-gone path) must not be treated as a
    failure just because it exited "before an external timeout" -- that
    was hackrf_sweep's failure signal, not rtl_power's.
    """
    from rtlsdr_source import _run_rtl_power

    script = tmp_path / "rtl_power"
    script.write_text(
        "#!/bin/sh\n"
        "sleep 0.3\n"
        'echo "2026-08-05, 20:11:31, 433000000, 434000000, 7812.50, 1, -20.0"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    readings = asyncio.run(_run_rtl_power(433_000_000, 434_000_000, duration_s=0.3))
    assert readings  # did not raise, and collected the reading


@pytest.fixture
def fake_rtl_power_emits_then_hangs_ignoring_sigterm(tmp_path, monkeypatch):
    """One valid CSV line flushed immediately, then hangs forever ignoring
    SIGTERM -- same technique as Component F's
    fake_hackrf_sweep_ignores_sigterm fixture (see
    test_run_hackrf_sweep_kills_process_that_ignores_sigterm in
    services/scanner-spectrum/tests/test_spectrum_source.py), adapted so a
    line is emitted right away to stand in for output rtl_power had
    already flushed to the pipe before wedging. Only SIGKILL stops it.
    """
    script = tmp_path / "rtl_power"
    script.write_text(
        "#!/bin/sh\n"
        "trap '' TERM\n"
        'echo "2026-08-05, 20:11:31, 433000000, 434000000, 7812.50, 1, -20.0"\n'
        "while true; do sleep 0.05; done\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return script


def test_run_rtl_power_readings_survive_escalation_to_sigkill(
    fake_rtl_power_emits_then_hangs_ignoring_sigterm,
):
    """Regression test for Critical 1: a fake rtl_power that emits one
    valid CSV line immediately, then hangs forever ignoring SIGTERM (only
    SIGKILL stops it), must still return that reading -- not discard it --
    even though escalation all the way to SIGKILL is required to reap it.

    Under the old proc.communicate()-based design this reproduced exactly
    as the review described: 0 readings instead of the 1 line the process
    actually emitted, because communicate()'s accumulated result is thrown
    away when the surrounding asyncio.wait_for cancels it on timeout.
    Verified empirically: temporarily reverting _run_rtl_power to the old
    communicate()-based implementation makes this test fail (readings ==
    [] instead of non-empty).

    Wrapped in a generous but finite outer timeout so a regression
    (unbounded hang) fails this test loudly instead of hanging the suite.
    """
    from rtlsdr_source import _run_rtl_power

    async def run():
        return await _run_rtl_power(433_000_000, 434_000_000, duration_s=0.1)

    readings = asyncio.run(asyncio.wait_for(run(), timeout=20))
    assert readings  # the line survived despite requiring SIGKILL


def test_run_rtl_power_terminates_process_on_outer_cancellation(tmp_path, monkeypatch):
    """Regression test for Critical 2: cancelling the coroutine running
    _run_rtl_power (e.g. service shutdown) must not orphan the child
    rtl_power process -- an orphan keeps the RTL-SDR USB device claimed,
    so a restarted scanner fails to reopen it.

    The fake script `exec`s into `sleep 30` (replacing itself, same pid)
    so that proc.terminate() (SIGTERM) reaches a process with no custom
    signal handler and dies on the spot -- isolating "was cleanup
    triggered at all by outer cancellation" from the separate
    ignores-SIGTERM/needs-SIGKILL escalation path already covered by
    test_run_rtl_power_readings_survive_escalation_to_sigkill.
    """
    from rtlsdr_source import _run_rtl_power

    pidfile = tmp_path / "child.pid"
    script = tmp_path / "rtl_power"
    script.write_text(
        "#!/bin/sh\n"
        f'echo $$ > "{pidfile}"\n'
        "exec sleep 30\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    async def run_and_cancel():
        task = asyncio.ensure_future(_run_rtl_power(433_000_000, 434_000_000, duration_s=10.0))
        for _ in range(200):
            if pidfile.exists():
                break
            await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(asyncio.wait_for(run_and_cancel(), timeout=10))

    pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 3.0
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.05)
    assert not alive, f"rtl_power (pid={pid}) was left running after outer cancellation -- orphaned"


@pytest.fixture
def fake_rtl_power_ignores_sigterm_no_output(tmp_path, monkeypatch):
    """Ignores SIGTERM and never emits any output -- models a process
    that is simply slow (not failed) and gets killed by our own timeout
    before it ever produces a reading, as distinct from
    fake_rtl_power_late_failure_no_readings (which models a real device
    failure: exits on its own, nonzero, without our intervention).
    """
    script = tmp_path / "rtl_power"
    script.write_text(
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "while true; do sleep 0.05; done\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return script


def test_run_rtl_power_does_not_blame_device_when_we_killed_a_slow_process(
    fake_rtl_power_ignores_sigterm_no_output,
):
    """Regression test for a bug found in review: using proc.returncode
    (read AFTER our own terminate()/kill()) instead of the exit code
    captured the moment the read loop ended meant our own SIGTERM/SIGKILL
    (-15/-9) could be mistaken for a device failure signal, producing a
    self-contradictory "device likely unavailable" exception on a process
    that was simply still running (None) when our own timeout fired --
    not something rtl_power itself ever reported as a failure.

    With the exit code captured before escalation (matching
    _run_hackrf_sweep's identical pattern), a process that's still
    running (None) at that capture point is not attributable to a device
    failure -- it's genuinely ambiguous whether it was failing or just
    slow -- so this returns quietly with an empty reading list rather
    than raising. stream_hits's own calibration-retry-on-empty-readings
    guard, and the dwell loop's "no hits from an empty reading list"
    behavior, already handle an empty result correctly without needing
    an exception raised here.

    Verified empirically: reverting the exited_returncode capture (using
    proc.returncode read AFTER escalation instead) makes this test fail
    with a raised OSError mentioning a returncode of -9.
    """
    from rtlsdr_source import _run_rtl_power

    async def run():
        return await _run_rtl_power(433_000_000, 434_000_000, duration_s=0.1)

    readings = asyncio.run(asyncio.wait_for(run(), timeout=20))
    assert readings == []


@pytest.fixture
def fake_rtl_power_late_failure_no_readings(tmp_path, monkeypatch):
    """Runs close to the full requested duration (not the fast
    device-gone path), writes an error to stderr, then exits nonzero
    without ever emitting a CSV line -- e.g. a USB read error partway
    through a scan pass with nothing usable produced.
    """
    script = tmp_path / "rtl_power"
    script.write_text(
        "#!/bin/sh\n"
        "sleep 0.28\n"
        "echo 'usb_read_async failed' >&2\n"
        "exit 1\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return script


def test_run_rtl_power_raises_on_late_failure_with_zero_readings(
    fake_rtl_power_late_failure_no_readings,
):
    """Regression test for Important 3: a nonzero exit late in the sweep
    window (not just an early one) must still raise if it produced zero
    usable readings -- otherwise this is silently treated as a successful
    empty sweep, which spins stream_hits's "zero in-band readings"
    calibration guard forever or leaves the dwell loop silently deaf.
    """
    from rtlsdr_source import _run_rtl_power

    with pytest.raises(OSError):
        asyncio.run(_run_rtl_power(433_000_000, 434_000_000, duration_s=0.3))


@pytest.fixture
def fake_rtl_power_late_failure_with_readings(tmp_path, monkeypatch):
    """Emits one valid CSV line, then later (near the full requested
    duration) writes an error to stderr and exits nonzero.
    """
    script = tmp_path / "rtl_power"
    script.write_text(
        "#!/bin/sh\n"
        'echo "2026-08-05, 20:11:31, 433000000, 434000000, 7812.50, 1, -20.0"\n'
        "sleep 0.28\n"
        "echo 'usb_read_async failed' >&2\n"
        "exit 1\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return script


def test_run_rtl_power_does_not_raise_on_late_failure_with_readings(
    fake_rtl_power_late_failure_with_readings,
):
    """Documents the deliberate design choice: a late failure that still
    produced usable readings must NOT raise -- better to use partial real
    data than discard it and retry the whole band from scratch.
    """
    from rtlsdr_source import _run_rtl_power

    readings = asyncio.run(_run_rtl_power(433_000_000, 434_000_000, duration_s=0.3))
    assert readings


@pytest.fixture
def fake_rtl_power_emits_then_exits_on_sigterm(tmp_path, monkeypatch):
    """Emits one valid CSV line immediately, then hangs well past a short
    duration_s's budget -- forcing this function's own SIGTERM escalation
    to fire -- but (unlike the ignores-sigterm fixture) actually honors
    SIGTERM and exits cleanly as soon as it's sent, rather than requiring
    SIGKILL.
    """
    script = tmp_path / "rtl_power"
    script.write_text(
        "#!/bin/sh\n"
        'echo "2026-08-05, 20:11:31, 433000000, 434000000, 7812.50, 1, -20.0"\n'
        "trap 'exit 0' TERM\n"
        "while true; do sleep 0.05; done\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return script


def test_run_rtl_power_escalation_terminated_reading_survives(
    fake_rtl_power_emits_then_exits_on_sigterm,
):
    """Closes the mutation-testing gap the review flagged against
    test_run_rtl_power_does_not_misflag_a_real_full_duration_sweep (whose
    sleep-vs-duration_s numbers happened to produce the same outcome under
    both the old and new read strategies, so it couldn't tell them apart).

    Here the process emits a line, then outlives this function's own
    budget_s (forcing escalation to SIGTERM), but honors SIGTERM and exits
    cleanly during the grace period rather than needing SIGKILL. Under the
    OLD proc.communicate()-based design this specific scenario silently
    returns an EMPTY reading list with no exception at all: the first
    communicate() call reads the line into its internal buffer, times out,
    and gets cancelled (discarding the buffered line per Critical 1); the
    process then exits 0 in response to SIGTERM, so the second
    communicate() call (issued after terminate()) succeeds normally but
    finds nothing left in the already-drained pipe, and returncode==0
    means the old failure check never fires either -- so the bug is
    invisible unless something actually asserts on the readings.
    Verified empirically via a scratch revert: the old implementation
    passes with readings == [] here, while the new implementation returns
    the line.
    """
    from rtlsdr_source import _run_rtl_power

    async def run():
        return await _run_rtl_power(433_000_000, 434_000_000, duration_s=0.1)

    readings = asyncio.run(asyncio.wait_for(run(), timeout=20))
    assert readings


def test_sweep_band_retries_on_early_exit(fake_rtl_power_device_gone, monkeypatch):
    """_sweep_band should treat the failure-as-OSError case the same way
    it already treats FileNotFoundError: log + sleep 5s + retry forever.
    Same shape as Component F's proven test_sweep_band_retries_on_early_exit.
    """
    import rtlsdr_source
    from rtlsdr_source import DEFAULT_BANDS, _sweep_band

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise RuntimeError("stop-test-after-two-retries")

    monkeypatch.setattr(rtlsdr_source.asyncio, "sleep", fake_sleep)

    band = DEFAULT_BANDS[0]
    with pytest.raises(RuntimeError, match="stop-test-after-two-retries"):
        asyncio.run(_sweep_band(band, duration_s=0.2))

    assert sleep_calls == [5, 5]


def test_stream_hits_retries_calibration_on_zero_readings(monkeypatch):
    """average_power([]) == 0.0 must never be accepted as a real baseline:
    if a band's calibration pass yields zero in-band readings, stream_hits
    should sleep 5s and retry that band's calibration rather than
    proceeding with a fake 0.0 dBm baseline. Same shape as Component F's
    proven test_stream_hits_retries_calibration_on_zero_readings.
    """
    import rtlsdr_source
    from rtlsdr_source import Band, stream_hits

    band = Band("test_band", 433_000_000, 434_000_000)
    calls = {"n": 0}

    async def fake_sweep_band(b, duration_s):
        # Yield to the event loop -- without a real await point this
        # "while True" loop would spin synchronously forever.
        await asyncio.sleep(0)
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # first calibration attempt: nothing usable
        return [(433_000_500, -70.0)]

    real_sleep = asyncio.sleep
    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(rtlsdr_source, "_sweep_band", fake_sweep_band)
    monkeypatch.setattr(rtlsdr_source.asyncio, "sleep", fake_sleep)

    async def collect_one():
        async for hit in stream_hits(margin_db=10.0, calibration_s=0.01, dwell_s=0.01, bands=[band]):
            return hit
        return None

    async def run_bounded():
        try:
            return await asyncio.wait_for(collect_one(), timeout=1.0)
        except asyncio.TimeoutError:
            return None

    asyncio.run(run_bounded())

    assert calls["n"] >= 2  # calibration was retried, not accepted empty on the first try
    assert 5 in sleep_calls  # used the standard 5s retry-forever backoff
