# Component F: Multi-Band Spectrum Scanner — Design

## 1. Motivation

The tool's BLE-native components (A/B/D) and the Ubertooth connection-follower
only ever detect a lost person's gear if it happens to be advertising or
connecting over BLE. A phone with no signal, a car keyfob, or a WiFi hotspot
searching for a network are all plausible RF emissions from someone lost in
the search area, and none of them are BLE.

This component doesn't try to identify a specific device the way the BLE
scanners do. It sweeps the frequency bands commercial devices commonly use
(cellular, WiFi, the shared 2.4 GHz ISM band, sub-GHz keyfobs), flags energy
above the ambient baseline, and logs a GPS-tagged hit. A hit is not proof of
a person — it's an investigatory lead: something worth a quick look by the
ground team, the same way an unidentified BLE device is today. This is
additive: it's a new detection layer feeding the existing Phase 2
cueing/clustering step, not a replacement for the BLE tooling.

## 2. Placement in the existing architecture

Per `docs/ARCHITECTURE.md`, this is a new **Component F**, deployed in
**Phase 1 (wide-area search)** alongside the existing Component B (BLE-native
drone scanner). Both fly the same grid and feed the same Phase 2
cueing/clustering step; a spectrum hit and a BLE detection are two
independent kinds of evidence pointing a ground team at the same candidate
area.

## 3. Hardware

- **SDR**: HackRF One (already in hand). 1 MHz–6 GHz tuning range, 20 MHz
  instantaneous bandwidth. Chosen over the Ettus B200 mini for this
  component specifically because it's lighter/cheaper and already
  available — the tradeoff (8-bit ADC, less sensitive than the B200 mini's
  12-bit) is acceptable for energy-threshold detection, which doesn't need
  the dynamic range a demodulation task would.
- **Antenna**: single wideband panel antenna, downward-facing, drone-mounted.
  Gain will vary non-uniformly across the ~300 MHz–5.8 GHz span this
  component sweeps — accepted tradeoff for zero switching hardware/weight.
- **Mount**: same drone payload as Component B; no MAVLink/GPS integration
  needed beyond what's already used elsewhere (GPS comes from the same
  `GpsClient` pattern the other scanners already use).

## 4. Default band list

Fixed order, not user-configurable per mission (may become configurable
later if a specific deployment needs it — YAGNI for now):

| Order | Band | Range | Rationale |
|---|---|---|---|
| 1 | Cellular low | 698–960 MHz | A phone with no signal periodically transmits high-power bursts searching for a tower — often the single strongest, most distinctive signature available |
| 2 | Cellular mid | 1710–2200 MHz | AWS/PCS bands |
| 3 | 2.4 GHz ISM | 2400–2483.5 MHz | Shared by WiFi, BLE, and AirTag/Find-My — one sweep covers all three |
| 4 | Keyfob | 300–450 MHz | Covers both 315 MHz (US) and 433.92 MHz (international) remotes |

5 GHz WiFi is intentionally out of the default list (personal hotspots
generally favor 2.4 GHz for range) — noted in Section 9 as easy to add.

## 5. Sweep & calibration algorithm

`hackrf_sweep` (ships with the HackRF host tools) natively retunes across a
given frequency range and streams power-per-bin readings — this component
wraps it as a subprocess and parses its output, the same pattern
`ubertooth_source.py` already uses for `ubertooth-btle`, rather than hand-
rolling IQ capture and FFT in Python.

Each `hackrf_sweep` output line is a CSV row:
`date, time, hz_low, hz_high, hz_bin_width, num_samples, dB, dB, dB, ...`
— one row per swept segment, with one dB reading per bin across that
segment. The parser buckets each bin's center frequency into whichever
default band it falls in.

**Startup calibration pass**: on scanner startup, before flight motion is
assumed to have started, cycle through all four bands once, dwelling on
each for `SPECTRUM_CALIBRATION_S` (default 10s) and recording the average
power per band as `baseline[band]`. This runs once per scanner process
lifetime, not per sweep cycle — it's a per-flight baseline, not a
continuously adapting one, per the earlier design decision to keep this
simple and mission-scoped.

**Live sweep loop**: cycle through the four bands in fixed order forever,
each for `SPECTRUM_DWELL_S` (default 5s) — start `hackrf_sweep -f
<low>:<high>`, parse output, compare every bin against
`baseline[band] + SPECTRUM_HIT_MARGIN_DB` (default 10 dB), then terminate
that subprocess and move to the next band. A bin exceeding baseline+margin
logs a hit.

All three constants (`SPECTRUM_CALIBRATION_S`, `SPECTRUM_DWELL_S`,
`SPECTRUM_HIT_MARGIN_DB`) are environment variables, following the existing
convention (`BLE_ADAPTER_USB_VID` etc.) — tunable per deployment without a
code change.

## 6. Data model

New table in the same shared `detections.sqlite3` (via a migration in
`storage.py`, same file the existing `detections` table lives in):

```sql
CREATE TABLE IF NOT EXISTS spectrum_hits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT NOT NULL,
    source_unit_id TEXT NOT NULL,
    band TEXT NOT NULL,          -- 'cellular_low' | 'cellular_mid' | 'ism_2_4ghz' | 'keyfob'
    freq_hz INTEGER NOT NULL,     -- center frequency of the bin that triggered
    power_dbm REAL NOT NULL,
    baseline_dbm REAL NOT NULL,
    lat REAL,
    lon REAL,
    alt_m REAL,
    gps_fix_age_s REAL
);
```

Deliberately a separate table from `detections`, not a shared one with
nullable MAC/name columns — a spectrum hit has no persistent identity to
tag/label/track across sightings the way a BLE MAC does, so forcing it into
the device-shaped table would just produce a pile of null columns and
complicate every existing query against `detections`.

## 7. Service architecture

New `scanner-spectrum` service, structured like `services/scanner/` but as
its own directory (`services/scanner-spectrum/`) since it has a different
runtime dependency (`hackrf_sweep` from the HackRF host tools, not
BlueZ/Ubertooth):

- `app/spectrum_source.py` — subprocess-wraps `hackrf_sweep`, parses CSV
  output, does the calibration-then-sweep loop from Section 5, yields hits.
- `app/main_spectrum.py` — drives `spectrum_source` through the same
  `Storage`/`GpsClient` pattern `main_ubertooth.py` already uses, writing
  into `spectrum_hits` instead of `detections`.
- `Dockerfile` — builds HackRF host tools from source (same rationale as
  `Dockerfile.ubertooth`: stay current with the device's firmware rather
  than a stale distro package).

**docker-compose.yml**: new `scanner-spectrum` service, same
`network_mode: host` + `privileged: true` + retry-forever-if-device-missing
pattern as `scanner-ubertooth`, writing into the same bind-mounted
`./data:/data`, with its own `SOURCE_UNIT_ID` (e.g.
`ground-logger-spectrum-01`). Starts unconditionally, no profile gate, so
an unattended SBC boot doesn't need a flag — if the HackRF isn't attached
it idles and retries every 5s, same as the other two scanners.

## 8. Error handling

- **HackRF not attached / `hackrf_sweep` fails to start**: log a warning,
  wait 5s, retry — same resilience pattern as the Ubertooth and BLE
  scanners, so one missing device never crashes the container or needs
  Docker's restart policy to recover it.
- **GPS fix missing or stale**: log the hit anyway with `gps_fix_age_s`
  reflecting the staleness, same as the existing `STALE_FIX_WARN_S` pattern
  in `main.py` — a stale-but-present fix is still useful, silently dropping
  the hit is not.
- **`hackrf_sweep` output parse failure on a line**: skip that line, log at
  debug, continue — a single malformed CSV row shouldn't take down a
  multi-hour flight.

## 9. Ground-station integration

- New `GET /api/spectrum_hits` endpoint (mirrors `/api/heatmap`'s shape:
  recent hits with band/freq/power/gps).
- WebSocket: extend the existing live-update path to also push new
  spectrum hits, or add a lightweight second message type — implementation
  detail for the plan, not the spec.
- New **map layer**, off by default, toggled independently from the
  existing device markers and heatmap (per Section "UI integration"
  decision — a hit isn't a taggable device, so it doesn't belong in the
  device table). Markers colored by band:
  - Cellular (low + mid): orange
  - 2.4 GHz ISM: purple
  - Keyfob: yellow

## 10. Testing / validation plan

- **Unit**: fixed sample `hackrf_sweep` CSV output fixtures → confirm bins
  bucket into the correct band, and threshold logic fires/doesn't fire at
  the right margin boundary.
- **Bench**: HackRF + antenna on the bench, no drone. Key a car remote near
  the antenna, confirm a `keyfob`-band hit logs with plausible freq/power.
  Toggle a phone's WiFi hotspot on, confirm an `ism_2_4ghz` hit.
- **Field**: same validate-against-real-hardware approach used for the
  Ubertooth CRC-24 fix — spot-check captured hits against a known,
  deliberately-triggered device (phone in airplane-mode-off with no signal,
  keyfob press) to confirm band/timestamp/GPS line up with ground truth.
- **Ground station**: confirm the new layer toggles independently, hits
  render with correct band coloring, and live WS updates flow the same way
  existing detections do.

## 11. Out of scope (future work, not this design)

- 5 GHz WiFi band.
- Burst-shape classification to reduce false positives (rejected earlier in
  favor of simple energy threshold — revisit if false-positive rate proves
  too high in the field).
- Multiple band-specific antennas + RF switch (rejected in favor of a
  single wideband panel).
- Decoding AirTag/Find-My BLE advertisements specifically — this component
  only sees "something's transmitting in the 2.4 GHz ISM band," not
  content; actual AirTag fingerprinting would extend the existing BLE
  scanners (Components A/B), not this one.
- Per-mission configurable band list/order — fixed default only, for now.
