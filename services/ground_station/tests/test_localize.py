"""Tests for localize.estimate().

Synthetic validation places an emitter at a KNOWN location, generates noisy
RSSI samples along a path from a log-distance model, and checks the
power-weighted-centroid estimate lands near the truth when the path passes
over/around it -- and correctly refuses (or reports low confidence) when it
doesn't. The choice of estimator itself was validated separately against real
logged BLE (see the module docstring); these tests guard the properties that
must hold.
"""

from __future__ import annotations

import math
import random

from localize import Sample, _to_latlon, _to_local, estimate

_LAT0, _LON0 = 44.5, -110.5
_M_PER_DEG_LAT = 110_540.0


def _offset(lat0, lon0, east_m, north_m):
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(lat0))
    return lat0 + north_m / _M_PER_DEG_LAT, lon0 + east_m / m_per_deg_lon


def _synth_samples(emitter_e, emitter_n, path, *, p0=-40.0, n=3.0, sigma=5.0, seed=1):
    rng = random.Random(seed)
    samples = []
    for east, north in path:
        d = max(math.hypot(east - emitter_e, north - emitter_n), 1.0)
        rssi = p0 - 10.0 * n * math.log10(d) + rng.gauss(0.0, sigma)
        lat, lon = _offset(_LAT0, _LON0, east, north)
        samples.append(Sample(lat=lat, lon=lon, rssi_dbm=rssi))
    return samples


def _lawnmower(width=160, height=160, rows=8, step=4):
    path = []
    for r in range(rows):
        y = -height / 2 + r * (height / (rows - 1))
        xs = range(-width // 2, width // 2 + 1, step)
        path.extend((x, y) for x in (xs if r % 2 == 0 else reversed(list(xs))))
    return path


def _err_m(res, emitter_e, emitter_n):
    east = (res.lon - _LON0) * 111_320.0 * math.cos(math.radians(_LAT0))
    north = (res.lat - _LAT0) * _M_PER_DEG_LAT
    return math.hypot(east - emitter_e, north - emitter_n)


def test_recovers_emitter_the_path_passed_over():
    emitter_e, emitter_n = 15.0, -20.0
    res = estimate(_synth_samples(emitter_e, emitter_n, _lawnmower(), sigma=5.0))
    assert res is not None
    assert _err_m(res, emitter_e, emitter_n) < 20.0
    assert res.confidence > 0.3


def test_more_confident_when_passed_over_than_when_skirted_far():
    over = estimate(_synth_samples(0.0, 0.0, _lawnmower(), sigma=5.0))
    far = estimate(_synth_samples(400.0, 400.0, _lawnmower(), sigma=5.0))
    assert over is not None and over.confidence > 0.3
    # A distant emitter is heard weakly and near-uniformly: either refused
    # outright (too little RSSI span) or reported with low confidence.
    assert far is None or far.confidence < over.confidence


def test_flat_rssi_is_not_localizable():
    # So far away every sample sits at essentially the same weak RSSI -> no
    # positional information -> refuse to guess.
    samples = _synth_samples(50_000.0, 50_000.0, _lawnmower(), sigma=1.0)
    res = estimate(samples)
    assert res is None or res.confidence < 0.2


def test_stronger_pass_gives_tighter_ellipse():
    # Concentrated strong hits (a device the path repeatedly closed on) should
    # yield a tighter ellipse than one only ever heard faintly at the edge.
    close = estimate(_synth_samples(0.0, 0.0, _lawnmower(), sigma=4.0))
    edge = estimate(_synth_samples(120.0, 0.0, _lawnmower(), sigma=4.0))
    assert close is not None and edge is not None
    assert close.semi_major_m < edge.semi_major_m


def test_ellipse_floored_at_gps_accuracy():
    # A device heard strongly from essentially one spot must not report a
    # degenerate (near-zero) ellipse -- no fix beats the receiver's own GPS.
    path = [(0.1 * i, 0.0) for i in range(40)]  # tiny spatial spread
    res = estimate(_synth_samples(0.0, 0.0, path, sigma=3.0), min_semi_axis_m=3.0)
    assert res is not None
    assert res.semi_minor_m >= 3.0
    assert res.semi_major_m >= 3.0


def test_too_few_samples_returns_none():
    samples = _synth_samples(0.0, 0.0, [(0, 0), (10, 10), (20, 20)])
    assert estimate(samples, min_samples=8) is None


def test_ignores_unpositioned_samples_below_minimum():
    samples = [Sample(lat=None, lon=None, rssi_dbm=-50.0) for _ in range(20)]
    assert estimate(samples) is None


def test_local_projection_round_trips():
    lat, lon = _offset(_LAT0, _LON0, 123.0, -87.0)
    xs, ys, lat0, lon0 = _to_local([Sample(lat, lon, 0.0), Sample(_LAT0, _LON0, 0.0)])
    back_lat, back_lon = _to_latlon(xs[0], ys[0], lat0, lon0)
    assert abs(back_lat - lat) < 1e-9
    assert abs(back_lon - lon) < 1e-9


def test_result_is_deterministic():
    samples = _synth_samples(15.0, 15.0, _lawnmower(), sigma=5.0)
    a = estimate(samples)
    b = estimate(samples)
    assert (a.lat, a.lon, a.semi_major_m, a.confidence) == (
        b.lat, b.lon, b.semi_major_m, b.confidence
    )
