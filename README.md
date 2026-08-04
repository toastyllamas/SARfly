# BLE SAR Direction-Finding Tool

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full system design.

Three services, meant to run unattended (e.g. on a field SBC at boot, no
user interaction):

- **scanner** (Phase 0/A ground logger): continuously scans for BLE
  advertisements via a Sena UD100 (or any BlueZ-recognized adapter) and logs
  each detection with a GPS-tagged timestamp to a local SQLite file.
  Validated end-to-end on a Sena UD100 + USB GPS.
- **scanner-ubertooth**: a second, independent advertisement-scanning unit
  backed by an Ubertooth One instead of the UD100, writing into the same
  detections log. Starts unconditionally alongside `scanner` — if no
  Ubertooth is attached, it just idles retrying every 5s rather than
  blocking anything else, so the same `docker compose up` works whether one,
  both, or neither adapter is present. Validated end-to-end against real
  hardware, including both scanners writing concurrently to the shared
  database.
- **ground-station**: a live web UI for tagging devices you already know
  about (so new/unknown ones stand out), a real-time map, and on-demand KMZ
  export for handoff to Google Earth/ATAK. Shows detections from both
  scanners together, distinguishable by `source_unit_id`.

## Host prerequisites

BlueZ and gpsd need direct access to hardware (the USB adapter's kernel
driver, and the GPS's serial device) and are tightly coupled to the host's
D-Bus/systemd/udev setup, so **they run on the host, not in the container.**
The container only talks to them as clients. This applies identically on the
dev laptop and on the Raspberry Pi target.

1. **Bluetooth adapter — Sena UD100**
   Plug it in. It uses the in-kernel `btusb` driver; no vendor driver needed
   on Linux. Confirm it's recognized:
   ```
   lsusb | grep -i sena      # or: look for a CSR8510-based Class 1 adapter
   hciconfig -a               # should list the new hciX interface
   ```
   Make sure `bluetoothd` (BlueZ) is installed and running:
   ```
   systemctl status bluetooth
   ```

2. **GPS — via gpsd**
   Any NMEA-capable USB/serial GPS module works; gpsd abstracts the specific
   hardware away from the app.
   ```
   sudo pacman -S gpsd            # or: apt install gpsd gpsd-clients
   sudo sed -i 's|^DEVICES=.*|DEVICES="/dev/ttyUSB0"|' /etc/default/gpsd   # adjust device path
   sudo systemctl enable --now gpsd.socket gpsd.service
   cgps -s                        # confirm it's producing a fix
   ```
   If `/dev/ttyUSB0` (or `/dev/ttyACM0`) never appears after plugging the
   adapter in, check `dmesg` for the usb-serial driver (`pl2303`, `cp210x`,
   `ftdi_sio`, etc.) actually loading — on rolling-release distros a pending
   kernel update whose modules haven't been picked up by a reboot yet can
   silently block `modprobe`, which will also break Docker's own networking
   (`iptables`/`nf_nat`) the same way. If both GPS and `docker.service` are
   acting up at once, that's usually the tell; reboot and recheck.

3. **Docker + Compose plugin** installed on the host (laptop or Pi), with
   your user in the `docker` group (`sudo usermod -aG docker $USER`, then
   re-login).

4. **Ubertooth One (optional)** — `scanner-ubertooth` starts either way (see
   Configuration below), so this step is only needed if you actually want it
   capturing. Just plug it in; confirm with `lsusb | grep -i ubertooth`
   (vendor:product `1d50:6002`). Unlike the Sena UD100, it doesn't need
   BlueZ, udev rules, or any host-side driver setup for the Docker path —
   `scanner-ubertooth` runs `privileged: true`, which gives the container raw
   USB access directly, and the Ubertooth host tools are built into that
   container's image. (Host udev rules only matter if you want to run
   `ubertooth-btle` directly on the host outside Docker, e.g. for ad-hoc
   testing.)

## Running

```
cd ble-sar-df
docker compose up --build
```

This starts all three services regardless of which adapters are physically
present — `scanner` and `scanner-ubertooth` each independently no-op/retry
if their adapter is missing (see Configuration below), so the same command
works unattended on an SBC that might have the UD100, the Ubertooth, both,
or (temporarily) neither.

Detections accumulate in `./data/detections.sqlite3` on the host (bind-mounted,
so the log survives container restarts/rebuilds). Inspect with:

```
sqlite3 data/detections.sqlite3 'select * from detections order by id desc limit 20;'
```

Open **http://localhost:8080** for the ground-station UI:

- A live table of every device seen, sorted by most recent activity. New/
  unknown devices are highlighted; tag each as **known** (e.g. rescuer
  phones, base camp gear), **unknown** (default — treat as a lead), or
  **ignore** (noisy neighbor devices etc.), with an optional free-text label.
  Tagging is instant and shared across every connected browser (it's a
  WebSocket broadcast, not a per-client filter).
- A live **Leaflet map**, colored by tag status. Leaflet itself is vendored
  into the image at build time so the UI loads with zero internet at
  runtime; the OpenStreetMap tile layer it requests needs live connectivity
  to actually show terrain/streets, but markers stay correctly positioned
  either way — with no signal you just get them over a blank background.
  Export to KMZ for guaranteed offline terrain/imagery in Google Earth/ATAK.
  Check "Show only untagged" to focus on new leads. This is the wide-area
  cueing step (Phase 2 in the architecture doc).
- **Search** (MAC / name / label) filters both the table and the map live.
  Narrowing to a single positioned device pans the map to it — a quick way
  to check where a specific already-known MAC has been showing up.
- **Hit density heatmap**: bins all positioned detections into a coarse grid
  (~11m cells by default) and colors each cell blue→red by hit count.
  Combine with search to see where one specific MAC's hits cluster most —
  a rough proximity gradient you can eyeball before sending the Yagi team
  out (Phase 3), not a replacement for it.
- **Vendor fingerprint guess**: every device gets checked against a small,
  research-backed Garmin BLE fingerprint (Bluetooth SIG company ID `0x0087`
  in manufacturer data, Garmin's proprietary `6a4e****-667b-11e3-949a-
  0800200c9a66` GATT service UUID family, and known product-line names like
  Forerunner/fenix/Venu/Instinct/etc.) — chosen because a continuously
  BLE-advertising GPS watch is the most probable RF signature a lost
  hiker/camper/kayaker is actually wearing. A match shows as a `Vendor?`
  badge next to the device name (high confidence = manufacturer ID or
  service UUID match, medium = name-only match), is searchable, and is
  included in KMZ descriptions. This is a device-type guess, not a target
  identification — a rescuer's own Garmin matches identically, so still tag
  known devices as usual during the pre-flight baseline step. The
  fingerprint logic lives in `services/ground_station/app/vendor_id.py` as
  an extensible list, so adding Apple Watch/Fitbit/Suunto/Coros/etc. later
  is a small addition, not a rewrite. WiFi-based identification was
  considered and rejected for now: only a handful of high-end Garmin models
  have WiFi at all, it only activates near a known saved network rather
  than continuously in the field, and it would need an entirely separate
  monitor-mode capture pipeline for a much weaker identification signal.
- Clicking a row (or focusing its label field) **locks the view** — a
  banner confirms it's frozen. Live updates keep arriving in the background
  but stop repainting the table/map until you click the row again to
  release it, so the list can't reorder or blow away in-progress typing.
  Status-button clicks and bulk actions still show immediately regardless.
- **Reset Database** permanently deletes all detections and tags (confirms
  first) — for starting a completely clean log between missions without
  restarting the containers.
- **Mark all known / Ignore all** bulk-tags every currently visible device
  (respecting the search box and "Show only untagged" filter) in one click,
  with a confirmation prompt first. Meant for pre-flight: clear the whole
  baseline of ambient devices before launching the drone, so anything that
  shows up as "unknown" afterward is a genuinely new lead. Existing labels
  are preserved; only status changes.
- **Export KMZ** downloads the current device set as a KMZ, color-coded the
  same way as the map, with MAC/RSSI/first-seen/last-seen/count in each
  placemark's description.
- **Night mode** swaps to a red-on-black theme for dark-adapted/NVG use.

## Configuration

Environment variables (set in `docker-compose.yml`):

**scanner**

| Var | Default | Meaning |
|---|---|---|
| `DB_PATH` | `/data/detections.sqlite3` | SQLite log path inside the container |
| `GPSD_HOST` / `GPSD_PORT` | `127.0.0.1` / `2947` | Where gpsd is listening on the host |
| `SOURCE_UNIT_ID` | `ground-logger-01` | Tag distinguishing this unit's records once multiple units' logs get merged |
| `BLE_ADAPTER` | unset (auto-detect) | Explicit override (e.g. `hci0`). By default the Sena UD100 is found by USB vendor:product ID rather than a hardcoded `hciN` index, since that index isn't stable across reboots/replugs. |
| `BLE_ADAPTER_USB_VID` / `_PID` | `0a12` / `0001` | Sena UD100 (CSR8510) USB ID used for the auto-detect above |

**scanner-ubertooth**

An independent second capture unit for an Ubertooth One, run alongside the
primary scanner rather than instead of it. Starts unconditionally with
`docker compose up`; if no Ubertooth is attached it just retries every 5s
without crashing (see "Known limitations" for the same behavior on the
primary `scanner` if the UD100 is missing). Ubertooth doesn't implement HCI
and BlueZ can't drive it as an adapter, so this doesn't go through
`BLE_ADAPTER`/bleak at all — it's a separate entrypoint
(`main_ubertooth.py`) that runs `ubertooth-btle -n` directly and parses its
output into the same detection schema, writing into the same shared
`detections.sqlite3`. Advertisement-only for now (matches what the primary
scanner does); BLE connection-following is a separate, not-yet-built
capability.

| Var | Default | Meaning |
|---|---|---|
| `DB_PATH` | `/data/detections.sqlite3` | Same SQLite file as the primary scanner (shared via the `./data` volume) |
| `GPSD_HOST` / `GPSD_PORT` | `127.0.0.1` / `2947` | Same as the primary scanner |
| `SOURCE_UNIT_ID` | `ground-logger-ubertooth-01` | Kept distinct from the primary scanner's so both units' detections are separately attributable once merged |
| `UBERTOOTH_DEVICE_INDEX` | unset (default device) | Only needed with more than one Ubertooth attached to the same host (`-U<n>`) |

**ground-station**

| Var | Default | Meaning |
|---|---|---|
| `DB_PATH` | `/data/detections.sqlite3` | Same SQLite file as the scanner (shared via the `./data` volume) |
| `POLL_INTERVAL_S` | `1.0` | How often to check for new detections and broadcast updates over the WebSocket |
| `GRID_PRECISION` | `4` | Decimal places used to bin lat/lon for the heatmap (4 ≈ 11m cells; lower = coarser/faster, higher = finer-grained) |

## Raspberry Pi deployment

The image is built from `python:3.12-slim`, which publishes multi-arch
manifests (amd64/arm64), so the same `docker-compose.yml` builds and runs
unmodified on a Pi. Copy the project over and run the same
`docker compose up --build` — no compose file changes needed unless the Pi's
GPS is on a different serial device (that's a gpsd/host config change, not a
compose change).

## Known limitations / not yet built

- The ground-station UI has no authentication — fine bound to `localhost` or
  a trusted field LAN, not fine exposed to an untrusted network.
- Map tiles require live internet (OpenStreetMap); markers/heatmap still
  render correctly without it, just with no basemap underneath. Use KMZ
  export for guaranteed offline terrain/imagery context.
- No device-track history on the map yet — it only plots each device's
  latest position, not its path over time. The heatmap shows historical
  density but not direction of travel.
- `privileged: true` on the scanner is broader than strictly necessary; it
  can be narrowed once the host's D-Bus BlueZ policy is tuned for the
  specific caps needed. The ground-station service does not need it.
- Not yet deployed/tested on a Raspberry Pi — validated so far on an x86_64
  dev laptop only.
- `scanner` and `scanner-ubertooth` both retry (every 5s) rather than exit
  if their adapter is missing at startup or disappears later, so
  `docker compose up` is safe to run unattended (e.g. at SBC boot) whether
  one, both, or neither adapter is physically present yet. Neither one
  currently distinguishes "adapter missing" from other startup failures in
  its retry log line, so if scanning silently never starts, check the
  container logs for what the underlying error actually is rather than
  assuming it's just a missing adapter.
