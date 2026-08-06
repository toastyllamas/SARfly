# BLE SAR Direction-Finding Tool — System Architecture

## 1. Concept of Operations (CONOPS)

This mirrors the doctrine already used for RF-DF search (PLB/ELT homing, wildlife
telemetry): go from **wide-area cueing** to **close-in bearing** in stages, each
stage narrowing the search area for the next.

| Phase | Goal | Unit | Antenna | Output |
|---|---|---|---|---|
| 0 — Baseline | Log every BLE device already known to be in the search area (rescuer phones, base camp gear, nearby permanent structures) so they can be filtered out later | Ground logger | Omni, small | Known-device whitelist |
| 1 — Wide-area search | Fly a grid/lawnmower pattern over the search area, log every BLE detection with GPS+time+RSSI | Drone payload | Omni + LNA | Time/geotagged detection log |
| 2 — Cueing | Cluster new (non-whitelisted) detections into 1+ candidate areas | Ground station software | — | Target list (waypoints) handed to ground team |
| 3 — Close-in DF | Walk/drive toward a candidate area, sweep a directional antenna to get a bearing, walk the RSSI gradient to a fix | Handheld unit | Yagi | Precise location for the ground team |
| 4 — Confirmation | Ground team visually/physically locates the person | — | — | — |

**Critical open assumption to validate before building anything:** phones do
*not* advertise over BLE by default, and this system goes in blind — no known
device ID or MAC to search for, only "is there BLE-consistent RF coming from
this direction." That splits the target model into three categories, each
needing a different detection strategy from Section 2 below:

1. **Free-running advertisers** — fitness tracker, smartwatch, medical-alert
   pendant, hearing aid, AirTag/Find-My tag, or (best case) a dedicated
   low-cost BLE beacon pre-issued to hikers/campers/kayakers before they go
   out (worth treating as a parallel deliverable — a $3 beacon epoxied into
   a keychain guarantees a target signal). Caught by passive advertisement
   scanning (Components A/B as built).
2. **Connected-but-silent accessories** — e.g. a smartwatch already paired to
   a phone. These stop advertising once connected, but leak their address
   once at connection establishment (`CONNECT_IND`) and can be followed by
   RF/hop pattern afterward without decoding anything further. Only catchable
   at the moment a *new* connection forms, though — not for a connection
   that's already been running for hours before the search unit arrives — so
   treat detections of this type as lower-confidence/one-shot compared to
   advertisement hits. Worth noting: a worn accessory like this typically
   outlasts the phone it's paired to (days of battery vs. a phone burning
   through its charge hunting for cell signal off-grid), so it may be the
   more durable signal to search for, just a harder one to catch mid-sweep.
3. **Unidentified RF energy** — anything BLE-shaped (2.4GHz ISM, short
   hopping bursts) whether or not it can be decoded at all, including
   connections that have been running the entire time. Only catchable by
   wideband energy detection (Component E), not by a BLE-native chipset.

Confirm which of these the mission is actually designing against each time,
since it changes expected range, duty cycle, and false-positive handling
throughout this doc. Because category 3 trades identification for coverage,
it's meant to feed the same Phase 2 cueing/clustering step as 1 and 2, not
replace it — an unclassified RF hotspot is a waypoint for a thermal/visual
pass or a ground team, same as a tagged/unknown device hit is today.

## 2. System Components

### A. Ground Logger (Phase 0)
- BLE-native chipset (ESP32 or nRF52840) + GPS module, small omni antenna.
- Runs a continuous BLE scan, writes `(timestamp, lat, lon, MAC, name, RSSI,
  adv_data)` to local storage.
- Output feeds the whitelist filter used in Phase 2.

### B. Drone-Mounted Wide-Area Search Unit (Phase 1)
- BLE-native chipset (ESP32 or nRF52840) — chosen over SDR here for weight,
  power draw, and because you need decoded advertisements (MAC/name/RSSI),
  not raw RF energy.
- Omni antenna + 2.4 GHz LNA (SAW-filtered, to avoid WiFi/video-link desense —
  the drone's own WiFi/video/telemetry radios are the biggest noise source
  this unit will fight).
- GPS: either its own module, or pull position from the flight controller via
  MAVLink if the companion computer has that link — avoids paying the
  weight/power cost of a second GPS.
- Logs locally (SD card) as the source of truth; optionally streams detection
  events (not raw scan data — just hits) down a telemetry link in near-real-time
  so the ground station can watch the grid search live instead of waiting for
  landing.

### C. Ground Station (Raspberry Pi)
- Ingests detections either live (serial/telemetry) or by pulling the SD log
  after landing.
- Filters out Phase-0 whitelisted devices.
- Clusters remaining detections (naive first pass: group by MAC, weight by
  RSSI and dwell time) into candidate target areas.
- Simple map UI (e.g., a local Flask app with Leaflet) showing the search
  grid actually flown, detection heatmap, and candidate markers.
- Exports a target list as GPX/KML waypoints the ground team can load into
  a handheld GPS or phone.

### D. Close-in DF Unit (Phase 3)
- Yagi antenna (12–16 element, ~14–18 dBi) for directionality.
- Receiver: BLE-native chipset is enough if you just need RSSI-vs-bearing
  (classic "spin and peak" DF); an SDR (HackRF/RTL-SDR) is only worth the
  extra complexity if you want something beyond amplitude comparison
  (e.g., later adding phase-based techniques). Recommend starting with the
  simpler BLE-native + Yagi RSSI-peak approach and only reaching for the SDR
  if that proves insufficient in the field.
- Live RSSI readout (audio tone and/or numeric display) so the operator can
  sweep the antenna by hand and identify the bearing of peak signal without
  staring at a screen.

### E. Wide-Area RF Energy Detector (SDR, Phase 1 alternate/supplement)
Targets category 3 from Section 1 (and catches category 2's connection-
establishment moments as a side effect) — detects BLE-shaped RF energy
without decoding any protocol, trading identification for the ability to see
connections that never advertise and are already running when the search
unit arrives.

- **Wideband option**: SDR with ≥80 MHz instantaneous bandwidth (LimeSDR
  Mini 2.0, bladeRF 2.0 micro — HackRF's 20 MHz and RTL-SDR's ~2.4 MHz are
  both too narrow to see the full 2400–2483.5 MHz ISM band, where BLE's 37
  data channels hop, in a single capture) running an onboard energy
  detector: threshold on power spectral density, then classify hits by burst
  shape (~2 MHz-wide, sub-millisecond bursts hopping across channels) to
  reject WiFi (20/40 MHz wide, longer/continuous-ish frames) sharing the same
  band. This is the only approach that sees an already-established,
  otherwise-silent connection.
- **Cheap/near-term option**: skip the SDR entirely — run the existing
  BLE-native chipset (nRF52840, already in the BOM) in promiscuous
  connection-follow mode (à la `ubertooth-btle`/nRF Sniffer follow mode)
  alongside its normal advertisement scan. Near-zero extra weight/power/cost,
  and it catches category 2's `CONNECT_IND` moments — just not
  already-running silent connections. Reasonable default; only reach for the
  wideband SDR if silent-connection coverage proves necessary in the field,
  same philosophy as Component D's SDR note above.
- Self-interference: the drone's own WiFi/video/telemetry radios are a much
  bigger problem for a wideband energy detector than for the narrowband
  BLE-channel scanning Components A/B already do, since a SAW filter can't
  front-end-reject WiFi when WiFi itself is inside the band you're trying to
  see — this has to be handled in the burst-shape classifier instead (flag
  it, don't try to filter it out in hardware).
- Compute: real-time FFT/energy-detection across 80+ MHz needs an SBC
  (Raspberry Pi 4/5-class) on the drone, not just an MCU — a heavier payload
  than Component B's MCU-only design.
- Output feeds the same detection log/heatmap as Components A/B, tagged as an
  unclassified hit (no MAC/name) rather than a device record.

**Coverage reality check** (8mi × 8mi = 64 sq mi search box, swath = 1.6×
detection radius for overlap margin):

| Detection radius | Parallel tracks | Total track length | Flight time @ quad (10 m/s) | Flight time @ fixed-wing (15 m/s) |
|---|---|---|---|---|
| 150 m | 54 | 691 km / 429 mi | 19.2 hr | 12.8 hr |
| 250 m | 32 | 414 km / 257 mi | 11.5 hr | 7.7 hr |
| 400 m | 20 | 259 km / 161 mi | 7.2 hr | 4.8 hr |

A single quadcopter (typically ~25 min/battery) cannot cover the full 64 sq
mi in one mission at any realistic detection radius — even the optimistic
400 m case is 7+ hours of flight, i.e. ~17 battery swaps. A long-endurance
fixed-wing/VTOL (~90 min/flight) gets it down to 3-6 sorties depending on
radius, which is the more realistic platform if the full box must be covered
in one operational period. The more practical answer, though, is usually
**don't cover the full box uniformly** — use whatever narrowed the search
area to 8×8 mi in the first place (last known position, trail network,
drainage/terrain funneling) to prioritize sub-grids, and treat the full-box
sweep as a fallback, not the default plan.

### F. Multi-Band Spectrum Scanner (HackRF, Phase 1 supplement)

A different, additive idea from Component E above: rather than trying to
see the whole BLE-shaped RF picture in one wideband capture, this sweeps a
fixed list of narrower, disjoint bands that *other* commercial device
categories use — cellular, WiFi, the shared 2.4 GHz ISM band, and sub-GHz
keyfobs — accepting sequential retuning as a deliberate tradeoff instead of
requiring ≥80 MHz instantaneous bandwidth hardware. Built with a HackRF One
(20 MHz instantaneous bandwidth, already in the BOM) running `hackrf_sweep`,
a single wideband downward-facing panel antenna, and simple
energy-threshold detection against a per-flight calibrated baseline — no
burst-shape classification. A hit is an investigatory lead ("something's
transmitting here"), not an identified device, feeding the same Phase 2
cueing step as Components A/B/D. Implemented as its own service
(`services/scanner-spectrum/`); see
`docs/superpowers/specs/2026-08-05-multiband-spectrum-scanner-design.md`
for the full design, including the default band list and why 5 GHz WiFi
and burst-shape classification were left out for now.

### G. RTL-SDR Spectrum Scanner (cheap-hardware variant of F)

Same detection philosophy as Component F (per-bin calibrated baseline,
GPS-tagged energy-threshold hits into the shared `spectrum_hits` table),
built on an RTL-SDR dongle instead of a HackRF One -- a ~$25-30 part a lot
of SAR volunteers already own, versus the HackRF's ~$300. The real
tradeoff, verified against hardware rather than assumed: the common
R820T/R820T2 tuner these dongles use only reaches ~24 MHz-1766 MHz, so its
default band list (`keyfob`, `cellular_low`) is a subset of Component F's
four bands -- it cannot reach the 2.4 GHz ISM band at all. Implemented as
its own service (`services/scanner-rtlsdr/`), reusing Component F's
already-hardened pure detection logic by duplication (this repo's
established cross-service reuse pattern) and writing into the *same*
`spectrum_hits` table with its own `SOURCE_UNIT_ID` -- no new
ground-station code was needed. See
`docs/superpowers/specs/2026-08-06-rtlsdr-spectrum-scanner-design.md` for
the full design, including the verified differences between `rtl_power`
and `hackrf_sweep`'s process lifecycle that its subprocess wrapper
accounts for.

## 3. Data Model

Every BLE detection (Components A/B/D) is one record, same schema across
all units so logs merge cleanly at the ground station:

```
timestamp_utc, lat, lon, alt_m, source_unit_id, mac_or_uuid,
device_name, rssi_dbm, tx_power_dbm (if advertised), adv_raw_hex
```

Component F's spectrum hits are **not** stored in this table — as built,
they live in a separate `spectrum_hits` table (`timestamp_utc,
source_unit_id, band, freq_hz, power_dbm, baseline_dbm, lat, lon, alt_m,
gps_fix_age_s`), since a hit has no MAC/device-name identity to fill those
columns with and forcing it into the device-shaped schema would just
produce null-heavy rows. This diverges from this section's original sketch
(which assumed Component E's unclassified hits would reuse the `detections`
table with `mac_or_uuid`/`device_name` left null) — the ground-station UI
renders them as an independent map layer instead of folding them into the
same device table/heatmap. If Component E (wideband simultaneous-capture
energy detection) is ever built, revisit whether it shares Component F's
`spectrum_hits` table or needs its own.

## 4. Hardware Sketch (to be firmed up into a BOM)

| Part | Candidate | Notes |
|---|---|---|
| MCU (drone + ground logger + DF unit) | ESP32 or nRF52840 | nRF52833/5340 also support BLE 5.1 AoA (Constant Tone Extension) if you ever want true multi-antenna direction finding instead of mechanical Yagi sweeps — flag as a v2 stretch goal, not v1 |
| LNA | 2.4 GHz, SAW-filtered (e.g. Mini-Circuits PSA4-5043+ class) | Filtering matters more than raw gain — you're fighting WiFi/video-link noise on the same drone |
| Omni antenna | 2.4 GHz, 5–9 dBi | Watch for the overhead null — a pure vertical whip has poor gain straight down, which matters directly under the drone during a grid search |
| Yagi antenna | 2.4 GHz, 12–16 element | Close-in bearing |
| SDR, close-in DF (D, optional v2) | HackRF One or RTL-SDR v3/v4 | Single-channel RSSI is enough here; only if BLE-native RSSI-peak DF proves insufficient |
| SDR, wide-area energy detector (E, optional v2) | LimeSDR Mini 2.0 or bladeRF 2.0 micro | Needs ≥80 MHz instantaneous bandwidth to see the full ISM band in one capture — HackRF/RTL-SDR are too narrow for this role |
| SDR, multi-band spectrum scanner (F) | HackRF One | Built and validated — see `services/scanner-spectrum/`. 20 MHz instantaneous bandwidth is enough here since it sweeps disjoint narrow bands sequentially rather than capturing the whole ISM band at once |
| SDR, RTL-SDR variant of the spectrum scanner (G) | Any RTL2832U + R820T/R820T2 dongle | Built and validated — see `services/scanner-rtlsdr/`. Cheaper, more widely-owned alternative to Component F's HackRF, at the cost of tuner range (~24MHz-1766MHz) |
| GPS | u-blox NEO-M8N/M9N | Or reuse flight controller GPS via MAVLink on the drone unit |
| SBC | Raspberry Pi 4/5 (ground station), Pi Zero 2 W if a drone-side SBC is ever needed | MCU-only is preferred on the drone for weight/power; add an SBC there only if you need onboard clustering/mapping mid-flight |
| Telemetry downlink | Reuse existing drone telemetry/companion-computer link if present; otherwise LoRa for range at low bandwidth (fine — detection events are tiny) | Avoid a second WiFi radio on the drone; it's just more self-interference |

## 5. Regulatory Note

All phases described here are **receive-only** (passive BLE scanning). No
transmit license or Part 15 certification concern — you're not radiating
anything beyond standard BLE scan behavior already built into off-the-shelf
chipsets. Worth re-confirming later only if a future version adds active
interrogation/paging rather than passive advertisement scanning.

## 6. Open Questions to Resolve Before Building

1. **Target device assumption** (see the three-category breakdown in Section 1
   above) — drives everything about expected range and advertisement interval.
2. Drone payload weight/power budget — determines whether the search unit is
   MCU-only or needs an SBC.
3. Does the drone already have a telemetry/companion-computer link the
   detection stream can ride on, or does this project need its own?
4. Search altitude/speed vs. free-space path loss at 2.4 GHz — sets how tight
   the grid spacing needs to be to guarantee overlap in detection radius.
5. Which target category from Section 1 is the mission actually designing
   against? Drives whether Component E is in scope at all — it's the
   heaviest/most complex addition (SBC + wideband SDR + burst classifier vs.
   the MCU-only design everywhere else), so don't build it speculatively.
6. If Component E is in scope: does the wideband-SDR + SBC payload fit the
   drone's weight/power budget, or does it push toward a second/larger
   aircraft dedicated to the energy-detection role?
7. If the full 8×8 mi box genuinely has to be covered (not just prioritized
   sub-grids per the coverage reality check in Component E) — is the
   platform a quad doing many battery swaps, or is a longer-endurance
   fixed-wing/VTOL worth acquiring for this role?

## 7. Suggested Build Order

1. Ground Logger (Phase 0/A) — smallest scope, validates the BLE scan +
   GPS + logging pipeline end-to-end on the bench and on foot.
2. Ground Station ingestion + map (C) — build against logs from (1) before
   any drone flight is needed.
3. Drone payload (B) — reuses firmware from (1), adds LNA/omni and the
   telemetry or SD-log path into (C).
4. Close-in DF unit (D) — independent hardware track, can be built in
   parallel with 1–3.
5. Wide-area RF energy detector (E) — separate track, only after Open
   Questions 5–6 above confirm it's needed; validate the wideband-SDR +
   burst classifier against a known BLE connection on the bench (e.g. a
   phone/watch pair) before ever putting it on the drone.
6. Multi-band spectrum scanner (F) — independent of (E), built using the
   HackRF already in hand. See
   `docs/superpowers/plans/2026-08-05-multiband-spectrum-scanner.md` for
   the implementation plan.
7. RTL-SDR spectrum scanner (G) -- independent of (F), same detection
   logic on cheaper hardware. See
   `docs/superpowers/plans/2026-08-06-rtlsdr-spectrum-scanner.md`.
