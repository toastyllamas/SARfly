"""Tests for pure spectrum-sweep parsing/band/detection logic."""

from __future__ import annotations

from spectrum_source import (
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
    baseline_by_freq = {2450000000: -70.0, 2451000000: -70.0}
    hits = detect_hits(
        readings, "ism_2_4ghz", baseline_by_freq, margin_db=10.0, fallback_baseline_dbm=-70.0
    )
    assert hits == [
        SpectrumHitReading(band="ism_2_4ghz", freq_hz=2450000000, power_dbm=-40.0, baseline_dbm=-70.0)
    ]


def test_detect_hits_none_above_threshold():
    readings = [(2450000000, -75.0)]
    baseline_by_freq = {2450000000: -70.0}
    assert (
        detect_hits(readings, "ism_2_4ghz", baseline_by_freq, margin_db=10.0, fallback_baseline_dbm=-70.0)
        == []
    )


def test_detect_hits_uses_fallback_baseline_for_unseen_frequency():
    """A swept frequency that wasn't present in calibration (shouldn't
    normally happen, but guard anyway) should fall back to the band's
    overall calibrated mean rather than KeyError or being silently
    skipped.
    """
    readings = [(999_999_999, -40.0)]
    hits = detect_hits(readings, "ism_2_4ghz", {}, margin_db=10.0, fallback_baseline_dbm=-70.0)
    assert hits == [
        SpectrumHitReading(band="ism_2_4ghz", freq_hz=999_999_999, power_dbm=-40.0, baseline_dbm=-70.0)
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
    from spectrum_source import _readings_in_band

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

import pytest


@pytest.fixture
def fake_hackrf_sweep(tmp_path, monkeypatch):
    script = tmp_path / "hackrf_sweep"
    script.write_text(
        "#!/bin/sh\n"
        "while true; do\n"
        '  echo "2026-08-05, 14:23:01.000000, 2400000000, 2405000000, 1000.00, 20, -54.32, -61.10"\n'
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


@pytest.fixture
def fake_hackrf_sweep_device_gone(tmp_path, monkeypatch):
    """A fake hackrf_sweep that mimics the real "device not attached" field
    failure: it exits immediately (non-zero) instead of streaming forever,
    same as a real hackrf_sweep with no HackRF plugged in.
    """
    script = tmp_path / "hackrf_sweep"
    script.write_text(
        "#!/bin/sh\necho 'hackrf_open() failed: -6' >&2\nexit 1\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return script


def test_run_hackrf_sweep_raises_when_process_exits_early(fake_hackrf_sweep_device_gone):
    """hackrf_sweep never exits on its own mid-sweep -- if the read loop
    hits EOF before duration_s elapses (i.e. wait_for did NOT time out),
    that's a failure (device gone / open failed), not a clean empty sweep.
    This must raise OSError so _sweep_band's retry-forever backoff catches
    it, rather than returning [] and being silently treated as a
    successful-but-empty sweep.
    """
    from spectrum_source import _run_hackrf_sweep

    with pytest.raises(OSError):
        asyncio.run(_run_hackrf_sweep(2_400_000_000, 2_405_000_000, duration_s=0.2))


def test_sweep_band_retries_on_early_exit(fake_hackrf_sweep_device_gone, monkeypatch):
    """_sweep_band should treat the early-exit-as-OSError failure the same
    way it already treats FileNotFoundError: log + sleep 5s + retry
    forever. Monkeypatch asyncio.sleep in the module so the test doesn't
    actually block for 5 real seconds, and stop after a couple of retries
    by raising from the patched sleep once we've proven the retry loop ran.
    """
    import spectrum_source
    from spectrum_source import DEFAULT_BANDS, _sweep_band

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise RuntimeError("stop-test-after-two-retries")

    monkeypatch.setattr(spectrum_source.asyncio, "sleep", fake_sleep)

    band = DEFAULT_BANDS[0]
    with pytest.raises(RuntimeError, match="stop-test-after-two-retries"):
        asyncio.run(_sweep_band(band, duration_s=0.2))

    assert sleep_calls == [5, 5]


def test_stream_hits_retries_calibration_on_zero_readings(monkeypatch):
    """average_power([]) == 0.0 must never be accepted as a real baseline:
    if a band's calibration pass yields zero readings, stream_hits should
    sleep 5s and retry that band's calibration rather than proceeding to
    the sweep loop with a fake 0.0 dBm baseline (which would make the
    scanner permanently deaf even if the device later recovers, since
    calibration only runs once).
    """
    import spectrum_source
    from spectrum_source import Band, stream_hits

    band = Band("test_band", 2_400_000_000, 2_400_002_000)
    calls = {"n": 0}

    async def fake_sweep_band(b, duration_s):
        # Yield to the event loop -- without a real await point, this
        # "while True" sweep loop would spin synchronously forever and the
        # outer asyncio.wait_for's timeout callback would never get a
        # chance to fire (a genuine hang, not just a slow test).
        await asyncio.sleep(0)
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # first calibration attempt: nothing usable
        return [(2_400_000_500, -70.0)]

    sleep_calls = []
    # spectrum_source.asyncio IS the real asyncio module (same object as our
    # own `import asyncio` above) -- patching its .sleep attribute replaces
    # asyncio.sleep everywhere, including inside fake_sweep_band's own
    # `await asyncio.sleep(0)` yield point above. Capture the real
    # implementation first and delegate to it (with 0 delay) so that yield
    # point still genuinely suspends instead of also becoming a no-op that
    # would spin the event loop forever.
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(spectrum_source, "_sweep_band", fake_sweep_band)
    monkeypatch.setattr(spectrum_source.asyncio, "sleep", fake_sleep)

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


def test_stream_hits_filters_out_of_band_readings(monkeypatch):
    """A reading _run_hackrf_sweep returns outside the target band's
    declared [low_hz, high_hz) range (hackrf_sweep over-scans past a band's
    edge) must not affect that band's calibrated baseline and must never be
    labeled/yielded as a hit for that band.

    Regression check: under the old, unfiltered code, this exact fixture
    WOULD produce a false hit -- the per-band scalar baseline
    avg(-70, -70, -40) = -60 gives a threshold of -50, and the out-of-range
    bin's -40dBm clears it. With filtering in place, that bin never enters
    calibration or sweep detection for this band at all, so no hit is ever
    produced.
    """
    import spectrum_source
    from spectrum_source import Band, stream_hits

    band = Band("test_band", 2_400_000_000, 2_400_002_000)
    fixed_readings = [
        (2_400_000_500, -70.0),  # in range
        (2_400_001_500, -70.0),  # in range
        (2_400_002_500, -40.0),  # out of range: >= high_hz
    ]

    async def fake_run_hackrf_sweep(low_hz, high_hz, duration_s):
        await asyncio.sleep(0)  # see comment in the retry-calibration test above
        return list(fixed_readings)

    monkeypatch.setattr(spectrum_source, "_run_hackrf_sweep", fake_run_hackrf_sweep)

    async def collect():
        hits = []
        async for hit in stream_hits(margin_db=10.0, calibration_s=0.01, dwell_s=0.01, bands=[band]):
            hits.append(hit)
            if len(hits) >= 3:
                break
        return hits

    async def run_bounded():
        try:
            return await asyncio.wait_for(collect(), timeout=0.5)
        except asyncio.TimeoutError:
            return []

    hits = asyncio.run(run_bounded())

    # In-range bins sit exactly at their own calibrated baseline (no
    # margin exceeded), and the out-of-range bin is discarded entirely --
    # so no hits should ever be produced from this fixture.
    assert hits == []
    assert all(h.freq_hz != 2_400_002_500 for h in hits)
