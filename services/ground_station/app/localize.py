"""Single-emitter localization from (position, rssi) samples collected along
a moving receiver's path -- a person walking with the scanner, or a drone
flying a lawnmower/orbit pattern.

Pure logic, no I/O. Fed a list of Samples for one device, it returns an
estimated emitter location with an uncertainty ellipse and a confidence
score, or None when the samples carry too little information to localize.

Method: a received-power-weighted centroid. Each sample's position is
weighted by its received power (10^(rssi/10), i.e. RSSI converted from dB to
a linear scale), so positions where the device was heard loudly dominate and
the weak long tail barely counts. The estimate is that weighted mean; the
power-weighted covariance of the sample positions becomes the uncertainty
ellipse, and how concentrated that strong-signal region is drives the
confidence.

Why not a path-loss particle filter / trilateration: an earlier version of
this module did exactly that -- fit a log-distance path-loss model and invert
it to triangulate position. It recovered synthetic emitters to within metres,
but validated against real logged BLE from a walk test it was 30-1900 m off,
repeatedly placing the estimate hundreds of metres from where every strong
detection actually was. The reason is physical: a model-inversion approach
leans on the RSSI-vs-distance *shape*, and real multipath, body/foliage
shadowing and antenna-pattern effects corrupt that shape badly. The one thing
that survives all that corruption is that, on average, the signal is loudest
near the emitter -- which is all a power-weighted centroid uses. On the same
real data the centroid landed within 0-13 m of every strong-hit cluster.

The tradeoff: a centroid can only place the emitter *within* the area
actually swept (it is pulled toward where you sampled and cannot extrapolate
to an emitter off to one side). For the intended use -- flying a lawnmower
pattern *over* a search rectangle, so the target is under the path -- that is
exactly the right behaviour; "loudest here" is the answer you want, and an
emitter outside the box is handled by extending the box, not by trusting a
multipath-corrupted decay curve.

Assumes a roughly constant receiver altitude (a level walk, or a lawnmower
flown at fixed height), so only horizontal position is estimated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# WGS84-ish local-tangent-plane conversion constants (metres per degree).
# Good to well under 1% over the <1 km spans a single search covers, far
# finer than RSSI localization can resolve anyway.
_M_PER_DEG_LAT = 110_540.0
_M_PER_DEG_LON_EQ = 111_320.0


@dataclass(frozen=True)
class Sample:
    lat: float
    lon: float
    rssi_dbm: float


@dataclass(frozen=True)
class LocalizationResult:
    lat: float
    lon: float
    # 1-sigma uncertainty ellipse (the strong-signal region), in metres.
    semi_major_m: float
    semi_minor_m: float
    orientation_deg: float  # bearing of the semi-major axis, degrees from north
    confidence: float  # 0..1, higher = tighter, better-constrained fix
    n_samples: int  # positioned samples used
    rssi_span_db: float  # max-min RSSI of the input; the raw localizability signal


def _to_local(samples: list[Sample]) -> tuple[list[float], list[float], float, float]:
    """Project lat/lon onto a local east/north metre plane centred on the
    sample centroid. Returns (xs, ys, lat0, lon0)."""
    lat0 = sum(s.lat for s in samples) / len(samples)
    lon0 = sum(s.lon for s in samples) / len(samples)
    m_per_deg_lon = _M_PER_DEG_LON_EQ * math.cos(math.radians(lat0))
    xs = [(s.lon - lon0) * m_per_deg_lon for s in samples]
    ys = [(s.lat - lat0) * _M_PER_DEG_LAT for s in samples]
    return xs, ys, lat0, lon0


def _to_latlon(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    m_per_deg_lon = _M_PER_DEG_LON_EQ * math.cos(math.radians(lat0))
    return lat0 + y / _M_PER_DEG_LAT, lon0 + x / m_per_deg_lon


def _weighted_mean_cov(px: list[float], py: list[float], w: list[float]):
    """Normalized-weighted mean and 2x2 covariance of a point cloud."""
    total = sum(w)
    wn = [wi / total for wi in w]
    mx = sum(p * wi for p, wi in zip(px, wn))
    my = sum(p * wi for p, wi in zip(py, wn))
    cxx = cyy = cxy = 0.0
    for x, y, wi in zip(px, py, wn):
        dx, dy = x - mx, y - my
        cxx += wi * dx * dx
        cyy += wi * dy * dy
        cxy += wi * dx * dy
    return mx, my, cxx, cyy, cxy


def _ellipse_from_cov(cxx: float, cyy: float, cxy: float) -> tuple[float, float, float]:
    """1-sigma semi-axes (metres) and semi-major bearing (deg from north) for
    a 2x2 covariance, via the closed-form eigen-decomposition of a symmetric
    2x2 matrix."""
    tr = cxx + cyy
    det = cxx * cyy - cxy * cxy
    disc = max(tr * tr / 4.0 - det, 0.0)
    root = math.sqrt(disc)
    lam1 = tr / 2.0 + root  # larger eigenvalue
    lam2 = max(tr / 2.0 - root, 0.0)
    semi_major = math.sqrt(max(lam1, 0.0))
    semi_minor = math.sqrt(lam2)
    # Eigenvector for the larger eigenvalue, then its bearing from north
    # (north = +y); atan2(east, north) gives a compass-style bearing.
    if abs(cxy) < 1e-9:
        vx, vy = (1.0, 0.0) if cxx >= cyy else (0.0, 1.0)
    else:
        vx, vy = lam1 - cyy, cxy
    bearing = math.degrees(math.atan2(vx, vy)) % 180.0
    return semi_major, semi_minor, bearing


def _confidence(semi_major_m: float, rssi_span_db: float) -> float:
    """Map a fix to a 0..1 score. A tight strong-signal region only earns a
    high score when the RSSI also spanned a wide range: a small ellipse from
    flat RSSI means the device was heard weakly and uniformly (never passed
    close), which is not a real fix, so the RSSI span gates the tightness."""
    # Tightness: full credit at <=15 m 1-sigma, none by 60 m -- matching the
    # observed split on real data between concentrated (~11 m) strong-hit
    # clusters and smeared (~65 m) moving/multipath devices.
    tightness = max(0.0, min(1.0, (60.0 - semi_major_m) / 45.0))
    # Evidence: RSSI must vary for proximity to have been observed at all.
    evidence = max(0.0, min(1.0, (rssi_span_db - 6.0) / 24.0))
    return round(tightness * evidence, 3)


def estimate(
    samples: list[Sample],
    *,
    weight_softness_db: float = 10.0,
    min_samples: int = 8,
    min_rssi_span_db: float = 6.0,
    min_semi_axis_m: float = 3.0,
) -> LocalizationResult | None:
    """Estimate one emitter's location from its (position, rssi) samples.

    Returns None when there is too little to work with -- fewer than
    ``min_samples`` positioned samples, or an RSSI span below
    ``min_rssi_span_db`` (a device heard at essentially constant strength was
    never approached, so nothing says where along the path it sits).

    ``weight_softness_db`` sets how sharply louder samples dominate: each
    sample's weight is 10^(rssi / weight_softness_db). The default 10 is
    literal received power (dB->linear); a larger value softens it toward a
    plain centroid, a smaller value sharpens it toward "strongest sample only".
    """
    positioned = [s for s in samples if s.lat is not None and s.lon is not None]
    if len(positioned) < min_samples:
        return None

    rssi_values = [s.rssi_dbm for s in positioned]
    rssi_span = max(rssi_values) - min(rssi_values)
    if rssi_span < min_rssi_span_db:
        return None

    xs, ys, lat0, lon0 = _to_local(positioned)
    # Weight by received power relative to the strongest sample (subtracting
    # the peak first keeps 10^(rssi/soft) from underflowing for weak RSSI;
    # it is a constant factor and cancels in the normalization).
    peak = max(rssi_values)
    w = [10.0 ** ((r - peak) / weight_softness_db) for r in rssi_values]

    mx, my, cxx, cyy, cxy = _weighted_mean_cov(xs, ys, w)
    semi_major, semi_minor, bearing = _ellipse_from_cov(cxx, cyy, cxy)
    # No fix can be tighter than the receiver's own position error: floor both
    # axes at GPS accuracy so a device heard strongly from one spot reports an
    # honest few-metre ellipse rather than a degenerate (and invisible) zero.
    semi_major = max(semi_major, min_semi_axis_m)
    semi_minor = max(semi_minor, min_semi_axis_m)
    lat, lon = _to_latlon(mx, my, lat0, lon0)
    return LocalizationResult(
        lat=lat,
        lon=lon,
        semi_major_m=round(semi_major, 1),
        semi_minor_m=round(semi_minor, 1),
        orientation_deg=round(bearing, 1),
        confidence=_confidence(semi_major, rssi_span),
        n_samples=len(positioned),
        rssi_span_db=round(rssi_span, 1),
    )
