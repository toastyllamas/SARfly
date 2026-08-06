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
