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
*not* advertise over BLE by default. This system only works if the missing
person is carrying something that transmits BLE advertisements continuously —
e.g., a fitness tracker, smartwatch, medical-alert pendant, hearing aid, AirTag/
Find-My-network tag, or (best case) a dedicated low-cost BLE beacon that your
SAR team pre-issues to hikers/campers/kayakers before they go out (this is the
most reliable option and is worth treating as a parallel deliverable —
a $3 BLE beacon epoxied into a keychain is trivial to build and guarantees a
target signal). Confirm which of these you're actually designing against, since
it changes expected range and duty cycle assumptions throughout this doc.

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

## 3. Data Model

Every detection is one record, same schema across all units so logs merge
cleanly at the ground station:

```
timestamp_utc, lat, lon, alt_m, source_unit_id, mac_or_uuid,
device_name, rssi_dbm, tx_power_dbm (if advertised), adv_raw_hex
```

## 4. Hardware Sketch (to be firmed up into a BOM)

| Part | Candidate | Notes |
|---|---|---|
| MCU (drone + ground logger + DF unit) | ESP32 or nRF52840 | nRF52833/5340 also support BLE 5.1 AoA (Constant Tone Extension) if you ever want true multi-antenna direction finding instead of mechanical Yagi sweeps — flag as a v2 stretch goal, not v1 |
| LNA | 2.4 GHz, SAW-filtered (e.g. Mini-Circuits PSA4-5043+ class) | Filtering matters more than raw gain — you're fighting WiFi/video-link noise on the same drone |
| Omni antenna | 2.4 GHz, 5–9 dBi | Watch for the overhead null — a pure vertical whip has poor gain straight down, which matters directly under the drone during a grid search |
| Yagi antenna | 2.4 GHz, 12–16 element | Close-in bearing |
| SDR (optional, v2) | HackRF One or RTL-SDR v3/v4 | Only if BLE-native RSSI-peak DF proves insufficient |
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

1. **Target device assumption** (see Phase 0 note above) — drives everything
   about expected range and advertisement interval.
2. Drone payload weight/power budget — determines whether the search unit is
   MCU-only or needs an SBC.
3. Does the drone already have a telemetry/companion-computer link the
   detection stream can ride on, or does this project need its own?
4. Search altitude/speed vs. free-space path loss at 2.4 GHz — sets how tight
   the grid spacing needs to be to guarantee overlap in detection radius.

## 7. Suggested Build Order

1. Ground Logger (Phase 0/A) — smallest scope, validates the BLE scan +
   GPS + logging pipeline end-to-end on the bench and on foot.
2. Ground Station ingestion + map (C) — build against logs from (1) before
   any drone flight is needed.
3. Drone payload (B) — reuses firmware from (1), adds LNA/omni and the
   telemetry or SD-log path into (C).
4. Close-in DF unit (D) — independent hardware track, can be built in
   parallel with 1–3.
