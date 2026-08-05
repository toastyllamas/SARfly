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
