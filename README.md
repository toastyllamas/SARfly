# BLE SAR Direction-Finding Tool

Passive BLE direction-finding for search and rescue: log GPS-tagged
Bluetooth Low Energy detections from a ground unit or a drone-mounted
sweep, cluster them into candidate leads on a live map, and hand a
narrowed search area off to the ground team — all receive-only, no
transmit license or Part 15 concern. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full phased system
design (baseline → wide-area sweep → cueing → close-in DF → confirmation).

## Supported hardware

| Role | Hardware | Status |
|---|---|---|
| Primary BLE scanner | Sena UD100, or any BlueZ-recognized adapter | Validated end-to-end |
| Secondary BLE scanner | Ubertooth One | Validated end-to-end, including independent CRC-24 verification (see [Ubertooth notes](#ubertooth-notes)) |
| Multi-band spectrum scanner | HackRF One | Validated end-to-end (see [Spectrum scanner notes](#spectrum-scanner-notes)) |
| RTL-SDR spectrum scanner | Any RTL2832U-based RTL-SDR dongle (R820T/R820T2 tuner) | Validated through real-hardware calibration on a Raspberry Pi (device detection, tuner ID, two-band baseline); a live-signal-to-map round trip specific to this backend hasn't been separately re-verified (see [RTL-SDR notes](#rtl-sdr-notes)) |
| GPS | Any NMEA-capable USB/serial module, via gpsd | Validated end-to-end |

The four scanners are independent and additive, not either/or: run any
subset of them at once, and every detection/hit lands in the same shared
log regardless of which radio saw it. None is required to be physically
present for the stack to start — see [Running](#running).

## Contents

- [Host prerequisites](#host-prerequisites)
- [Running](#running)
- [Ground-station UI](#ground-station-ui)
- [Configuration](#configuration)
- [Ubertooth notes](#ubertooth-notes)
- [Spectrum scanner notes](#spectrum-scanner-notes)
- [RTL-SDR notes](#rtl-sdr-notes)
- [Raspberry Pi deployment](#raspberry-pi-deployment)
- [Known limitations / not yet built](#known-limitations--not-yet-built)

## Host prerequisites

BlueZ and gpsd need direct access to hardware (the USB adapter's kernel
driver, and the GPS's serial device) and are tightly coupled to the host's
D-Bus/systemd/udev setup, so **they run on the host, not in the container.**
The container only talks to them as clients. This applies identically on the
dev laptop and on the Raspberry Pi target.

1. **Bluetooth adapter — Sena UD100**
   Plug it in. It uses the in-kernel `btusb` driver; no vendor driver needed
   on Linux. Confirm it's recognized:
   ```bash
   lsusb | grep -i sena       # or: look for a CSR8510-based Class 1 adapter
   hciconfig -a                # should list the new hciX interface
   ```
   Make sure `bluetoothd` (BlueZ) is installed and running:
   ```bash
   systemctl status bluetooth
   ```

2. **GPS — via gpsd**
   Any NMEA-capable USB/serial GPS module works; gpsd abstracts the specific
   hardware away from the app.
   ```bash
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

4. **Ubertooth One (optional)** — plug it in; confirm with
   `lsusb | grep -i ubertooth` (vendor:product `1d50:6002`). Unlike the
   UD100, it needs no BlueZ/udev/host-side driver setup at all for the
   Docker path: `scanner-ubertooth` runs `privileged: true` for raw USB
   access, and its host tools are built into that container's own image.
   (Host udev rules only matter for running `ubertooth-btle` directly on the
   host, outside Docker — e.g. ad-hoc testing.)

5. **HackRF One (optional)** — plug it in; confirm with
   `lsusb | grep -i hackrf` (vendor:product `1d50:6089`). Like the
   Ubertooth, it needs no host-side driver setup for the Docker path:
   `scanner-spectrum` runs `privileged: true` for raw USB access, and
   `hackrf_sweep` is built into that container's own image.

6. **RTL-SDR dongle (optional)** — plug it in; confirm with `lsusb` (any
   Realtek RTL2832U-based device, e.g. `0bda:2838`). Like the others, no
   host-side driver setup needed for the Docker path: `scanner-rtlsdr` runs
   `privileged: true` for raw USB access, and unlike Ubertooth/HackRF, its
   `rtl-sdr` package comes straight from Debian (no from-source build).

## Running

```bash
cd ble-sar-df
docker compose up --build
```

This always starts all five services, regardless of which adapters are
physically present — `scanner`, `scanner-ubertooth`, `scanner-spectrum`, and
`scanner-rtlsdr` each retry quietly in the background rather than blocking
anything if their radio is missing (see [Known limitations](#known-limitations--not-yet-built)).
One command works unattended whether the host has any, all, or none of the
UD100/Ubertooth/HackRF/RTL-SDR yet — the point of that design is a field SBC that
boots this stack with no one there to pass a flag.

Detections accumulate in `./data/detections.sqlite3` on the host (bind-mounted,
so the log survives container restarts/rebuilds). Inspect with:

```bash
sqlite3 data/detections.sqlite3 'select * from detections order by id desc limit 20;'
```

Open **http://localhost:8080** for the ground-station UI.

## Ground-station UI

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
  is a small addition, not a rewrite.
- **OUI vendor lookup (low confidence)**: when nothing above matches, the
  MAC's first three octets are checked against IEEE's public OUI registry
  (bundled at `services/ground_station/app/data/oui.csv`, ~40k assignments,
  so this works fully offline) to at least name a hardware manufacturer --
  "Espressif Inc." rather than nothing. Skipped entirely for locally
  administered (randomized) MACs, since those prefixes were never actually
  IEEE-assigned and a lookup would risk a coincidentally-real-looking but
  meaningless name; see `services/ground_station/app/oui_lookup.py`. This
  is the weakest of the three confidence tiers (shown more faintly in the
  UI) and applies to WiFi station detections (see below) as well as BLE
  ones, since it only needs a MAC, not protocol-specific advertisement
  data. To refresh the bundled registry later: `curl -o oui_raw.csv
  https://standards-oui.ieee.org/oui/oui.csv`, then re-derive the two-column
  `oui_prefix,vendor` CSV from its `Assignment`/`Organization Name` fields.
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
- **Spectrum hits** layer (off by default) plots hits from the multi-band
  spectrum scanner, colored by band (cellular orange, 2.4GHz ISM purple,
  keyfob yellow). A hit is energy above that band's per-flight calibrated
  baseline in a frequency range commercial devices use — not a decoded,
  identified device the way a BLE detection is, so it's an investigatory
  lead, not proof: worth a quick look by the ground team, same as an
  unidentified BLE device is today. See [Spectrum scanner
  notes](#spectrum-scanner-notes).

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

| Var | Default | Meaning |
|---|---|---|
| `DB_PATH` | `/data/detections.sqlite3` | Same SQLite file as the primary scanner (shared via the `./data` volume) |
| `GPSD_HOST` / `GPSD_PORT` | `127.0.0.1` / `2947` | Same as the primary scanner |
| `SOURCE_UNIT_ID` | `ground-logger-ubertooth-01` | Kept distinct from the primary scanner's so both units' detections are separately attributable once merged |
| `UBERTOOTH_DEVICE_INDEX` | unset (default device) | Only needed with more than one Ubertooth attached to the same host (`-U<n>`) |

**scanner-spectrum**

| Var | Default | Meaning |
|---|---|---|
| `DB_PATH` | `/data/detections.sqlite3` | Same SQLite file as the other scanners (shared via the `./data` volume) |
| `GPSD_HOST` / `GPSD_PORT` | `127.0.0.1` / `2947` | Same as the other scanners |
| `SOURCE_UNIT_ID` | `ground-logger-spectrum-01` | Kept distinct so this unit's hits are separately attributable once merged |
| `SPECTRUM_CALIBRATION_S` | `10` | Seconds to sample each band at startup when establishing that flight's baseline |
| `SPECTRUM_DWELL_S` | `5` | Seconds to sweep each band per pass once calibration is done |
| `SPECTRUM_HIT_MARGIN_DB` | `10` | How far above a reading's own frequency bin's calibrated baseline it must be to count as a hit |

**scanner-rtlsdr**

| Var | Default | Meaning |
|---|---|---|
| `DB_PATH` | `/data/detections.sqlite3` | Same SQLite file as the other scanners |
| `GPSD_HOST` / `GPSD_PORT` | `127.0.0.1` / `2947` | Same as the other scanners |
| `SOURCE_UNIT_ID` | `ground-logger-rtlsdr-01` | Kept distinct so this unit's hits are separately attributable |
| `RTLSDR_CALIBRATION_S` | `25` | Seconds to sample each band at startup when establishing that flight's baseline (tuned from real-hardware timing in Task 7) |
| `RTLSDR_DWELL_S` | `20` | Seconds to sweep each band per pass once calibration is done |
| `RTLSDR_HIT_MARGIN_DB` | `10` | How far above a reading's own frequency bin's calibrated baseline it must be to count as a hit |

**ground-station**

| Var | Default | Meaning |
|---|---|---|
| `DB_PATH` | `/data/detections.sqlite3` | Same SQLite file as the scanner (shared via the `./data` volume) |
| `POLL_INTERVAL_S` | `1.0` | How often to check for new detections and broadcast updates over the WebSocket |
| `GRID_PRECISION` | `4` | Decimal places used to bin lat/lon for the heatmap (4 ≈ 11m cells; lower = coarser/faster, higher = finer-grained) |

## Ubertooth notes

`scanner-ubertooth` is a genuinely separate capture path, not the UD100
scanner pointed at different hardware — Ubertooth doesn't implement HCI and
never shows up under `/sys/class/bluetooth`, so BlueZ/`bleak` has no way to
drive it as an adapter. Instead it's its own entrypoint
(`main_ubertooth.py`) that runs `ubertooth-btle -n` as a subprocess, parses
its text output, and writes into the same detection schema the primary
scanner uses. Advertisement-only for now, matching what the UD100 path
does; BLE connection-following (catching a device that's paired to a phone
and no longer advertising) is a separate, not-yet-built capability — see
Component E in `docs/ARCHITECTURE.md`.

One non-obvious fix worth knowing about if you're reading the source:
`ubertooth-btle`'s own `(valid)`/`(invalid)` tag on decoded packets does
**not** mean the CRC checked out — it only reflects whether the access
address matched (confirmed against `libbtbb`'s source), and nothing in this
capture path verifies CRC-24 by itself. Left unchecked, that showed up as
device names with single/few corrupted bytes (a real device seen during
testing: `WHOOP 5AG0085293` intermittently coming through as
`WH_OP 1AG0085293` and similar). `ble_crc.py` ports the real CRC-24
generator from the Ubertooth firmware source and `ubertooth_source.py` uses
it as the actual validity gate, verified against the packet's raw on-air
bytes rather than the tool's own flag.

## Spectrum scanner notes

`scanner-spectrum` doesn't decode a protocol the way the two BLE scanners
do — it wraps `hackrf_sweep` (built from source in the container, same
build-from-upstream rationale as the Ubertooth tools) and does simple
energy-threshold detection across a fixed default list of bands: cellular
(698–960 MHz and 1710–2200 MHz — a phone with no signal periodically
transmits high-power bursts searching for a tower, often the single
strongest signature available), the shared 2.4 GHz ISM band (WiFi/BLE/
AirTag), and sub-GHz keyfobs (300–450 MHz, covering both 315 and
433.92 MHz remotes). 5 GHz WiFi is intentionally left out of the default
list — personal hotspots generally favor 2.4 GHz for range.

On startup it samples each band for `SPECTRUM_CALIBRATION_S` seconds to
establish that flight's own ambient-noise baseline before any drone motion
is assumed to have started, then sweeps the bands forever, flagging any
reading `SPECTRUM_HIT_MARGIN_DB` above **its own frequency bin's** calibrated
baseline as a hit — the baseline is per-bin, not a single average across the
whole band, so a bin that's always occupied (a nearby WiFi AP, a cell
carrier) doesn't push every other bin's threshold up with it. This
is a per-flight baseline, not a continuously adapting one, by design — see
`docs/superpowers/specs/2026-08-05-multiband-spectrum-scanner-design.md`
for the full rationale, including why a single wideband panel antenna and
simple threshold detection (no burst-shape classification) were chosen
over the alternatives.

A hit is not a decoded, identified device — it's "energy above baseline in
a band commercial devices use," logged with band/frequency/power/GPS into
its own `spectrum_hits` table (not merged into `detections`, since a hit
has no persistent MAC-like identity to tag or track across sightings the
way a BLE device does).

## RTL-SDR notes

`scanner-rtlsdr` is Component F's detection philosophy (per-bin calibrated
baseline, threshold detection, GPS-tagged hits into the shared
`spectrum_hits` table) on cheaper, far more widely-owned hardware: an
RTL-SDR dongle costs roughly a tenth of a HackRF One and a lot of people
already have one from another hobby. The tradeoff is coverage — the common
R820T/R820T2 tuner these dongles use only reaches ~24 MHz-1766 MHz
(verified against the actual dongle used to build this), so it cannot see
the 2.4 GHz ISM band at all and only reaches the bottom sliver of Component
F's cellular-mid range. Rather than ship a same-named `cellular_mid`/
`ism_2_4ghz` band covering a different, smaller range than Component F's
own bands of the same name, `scanner-rtlsdr`'s default band list is scoped
to the two bands (`keyfob`, `cellular_low`) this hardware can fully cover.

It uses `rtl_power` (packaged directly by Debian, unlike Ubertooth/HackRF's
from-source builds) rather than `hackrf_sweep`, which behaves differently
in ways this service's code specifically accounts for: `rtl_power` exits
on its own after each sweep interval rather than running forever, and
batches its output rather than streaming it — see
`docs/superpowers/specs/2026-08-06-rtlsdr-spectrum-scanner-design.md` for
the verified details.

Both `scanner-spectrum` and `scanner-rtlsdr` write into the same
`spectrum_hits` table and render on the same ground-station map layer —
run either, both, or neither; a hit from one is indistinguishable from the
other in the UI except by `source_unit_id` in the underlying row.

## Raspberry Pi deployment

The final-stage image for every service is built from `python:3.12-slim`,
which publishes multi-arch manifests (amd64/arm64), so the same
`docker-compose.yml` builds and runs unmodified on a Pi. Copy the project
over and run the same `docker compose up --build` — no compose file changes
needed unless the Pi's GPS is on a different serial device (that's a
gpsd/host config change, not a compose change).

`scanner-ubertooth` and `scanner-spectrum` both compile their host tools
(`ubertooth-btle`/`libbtbb`, and `hackrf_sweep`) from source in a
`debian:trixie-slim` build stage rather than using prebuilt binaries — that
base image is multi-arch too, so this still builds natively on a Pi with no
changes, just noticeably slower than on a dev laptop the first time (real C
compilation on Pi-class CPUs, not just a slow download). Budget extra time
for the first `--build` on-device; subsequent builds are cached as usual.

Running all three scanners (Sena UD100, Ubertooth One, HackRF One) plus a
USB GPS on one Pi puts four USB radios on a board with a limited USB power
budget, especially on a Pi 3 or Zero. If a scanner drops out intermittently
or a device fails to enumerate, try a powered USB hub before assuming a
software problem — this is a known sharp edge with SDR-class USB devices
(the HackRF in particular) on Pi-class hardware.

### Direct WiFi access point (hostapd)

For field use without existing WiFi infrastructure, the Pi's onboard WiFi
(`wlan0`) can run as its own access point via hostapd, so a laptop/phone can
connect directly to the ground-station UI/SSH. Configs are in
`deploy/pi-hostapd/`. This repurposes `wlan0` entirely -- it can no longer
also be the Pi's uplink, so the Pi needs Ethernet (or a second WiFi adapter)
for internet/LAN access once this is enabled.

Raspberry Pi OS (Bookworm+) manages WiFi through NetworkManager, not
`dhcpcd`, so hostapd needs `wlan0` handed off first or the two will fight
over the interface:

```
sudo apt install hostapd dnsmasq
sudo systemctl disable --now dnsmasq   # re-enabled below, once scoped

# Hand wlan0 off to hostapd
sudo cp deploy/pi-hostapd/unmanaged-wlan0.conf /etc/NetworkManager/conf.d/
sudo systemctl reload NetworkManager

# Static IP for wlan0 (hostapd brings the interface up itself)
sudo cp deploy/pi-hostapd/wlan0-ap.network /etc/systemd/network/40-wlan0-ap.network
sudo systemctl enable --now systemd-networkd

# hostapd -- edit the passphrase first (this file is a template)
sudo cp deploy/pi-hostapd/hostapd.conf /etc/hostapd/hostapd.conf
sudo $EDITOR /etc/hostapd/hostapd.conf   # set wpa_passphrase
sudo chmod 600 /etc/hostapd/hostapd.conf
sudo sed -i 's|^#DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd
sudo systemctl unmask hostapd
sudo systemctl enable --now hostapd

# dnsmasq, scoped to wlan0 only -- see the file's own comments for why the
# scoping matters (an unscoped dnsmasq can answer DHCP for someone else's
# network on whatever interface the Pi's uplink is on)
sudo sed -i 's|^#conf-dir=/etc/dnsmasq.d/,\*.conf|conf-dir=/etc/dnsmasq.d/,*.conf|' /etc/dnsmasq.conf
sudo cp deploy/pi-hostapd/wlan0-ap.conf /etc/dnsmasq.d/wlan0-ap.conf
sudo dnsmasq --test   # validate before (re-)starting
sudo systemctl enable --now dnsmasq
```

Connect to the `SARfly` SSID and reach the ground station at
`http://192.168.4.1:8080`. This setup gives connected devices access to the
Pi only, not internet -- there's no NAT/IP-forwarding configured, by design,
to keep the number of moving parts down. Add `net.ipv4.ip_forward=1` plus an
`iptables`/`nft` MASQUERADE rule on the uplink interface if a connected
laptop also needs internet through the Pi.

## Known limitations / not yet built

- The ground-station UI has no authentication — fine bound to `localhost` or
  a trusted field LAN, not fine exposed to an untrusted network.
- Map tiles require live internet (OpenStreetMap); markers/heatmap still
  render correctly without it, just with no basemap underneath. Use KMZ
  export for guaranteed offline terrain/imagery context.
- No device-track history on the map yet — it only plots each device's
  latest position, not its path over time. The heatmap shows historical
  density but not direction of travel.
- In a bench test with real hardware in a busy indoor RF environment,
  `scanner-spectrum` logged spectrum hits at ~55/s even after switching to
  per-bin baselines — likely still elevated by the calibration baseline
  being a simple mean per bin rather than a percentile, so a bin with
  bursty (not constant) occupancy can clear its own threshold repeatedly.
  At sustained rates like this, each per-hit log line is emitted at DEBUG
  (quiet by default), but a co-located per-hit GPS-staleness check still
  logs at WARNING on every hit when the GPS fix is stale — worth watching
  log volume on an SD-card-backed SBC in a canopy/urban-canyon flight where
  GPS is frequently stale. Revisit the baseline statistic (e.g. a high
  percentile instead of the mean) if false-positive rate remains high
  outdoors.
- A browser tab that's disconnected from the WebSocket (e.g. a drone-to-
  ground link blip) when someone clicks "Reset Database" will not clear its
  spectrum-hits map layer on reconnect — it merges the fresh (now-empty)
  state in by row id rather than blindly replacing, which fixed a worse bug
  (reconnects truncating accumulated map history) but means a reset missed
  while disconnected leaves stale markers on screen until the next explicit
  reset happens while connected. Low-probability and self-healing with one
  more click; a session/generation token on the reset broadcast would close
  it properly if it proves disruptive in practice.
- `privileged: true` on all four scanners is broader than strictly
  necessary; it can be narrowed once the host's D-Bus BlueZ policy is tuned
  (for `scanner`) and the exact USB caps are pinned down (for
  `scanner-ubertooth`, `scanner-spectrum`, and `scanner-rtlsdr`). The ground-station service
  needs neither.
- All four scanner services (plus ground-station) have run together in
  Docker Compose on a Raspberry Pi 5. Of those, `scanner` (Sena UD100, real
  BLE detections logged) and `scanner-rtlsdr` (real calibration against a
  physical dongle) have been validated against real hardware specifically
  on the Pi. `scanner-ubertooth` and `scanner-spectrum`'s real-hardware
  validation (described elsewhere in this doc) happened on an x86_64 dev
  laptop, not yet re-run against a physical Ubertooth/HackRF on the Pi
  itself — expected to work unmodified (same multi-arch base images, see
  [Raspberry Pi deployment](#raspberry-pi-deployment)) but not yet
  confirmed there.
- All four scanners retry every 5s rather than exit if their adapter is
  missing or disappears mid-run, which is what makes unattended boot safe
  (see [Running](#running)) — but none currently distinguishes "adapter
  missing" from other startup failures in its retry log line, so if
  scanning silently never starts, check the container logs for what the
  underlying error actually is rather than assuming it's just a missing
  adapter.
- The RTL-SDR's real tunable range (~24 MHz-1766 MHz, common R820T/R820T2
  tuners) means `scanner-rtlsdr` cannot cover the same band list as
  `scanner-spectrum` — see [RTL-SDR notes](#rtl-sdr-notes). This is a
  hardware limit, not a configuration gap.
- A narrow, low-probability race in both `scanner-spectrum`'s and
  `scanner-rtlsdr`'s subprocess-management code: if the coroutine
  supervising `hackrf_sweep`/`rtl_power` is cancelled twice in quick
  succession while it's already escalating from SIGTERM to SIGKILL (e.g.
  during a specific window of service shutdown), the underlying asyncio
  subprocess transport can end up in a state where a subsequent
  `proc.kill()` call silently does nothing even though the real OS process
  is still alive — leaking a process that (for `scanner-rtlsdr`
  specifically) keeps the USB device claimed until it's manually killed or
  the container is restarted. Needs two cancellations landing within the
  same several-second window to trigger; not something a routine restart
  or single Ctrl-C should hit. Tracked in `_run_rtl_power`'s docstring in
  `services/scanner-rtlsdr/app/rtlsdr_source.py`, not chased further for
  now.
- The spectrum scanner's per-band frequency ranges are US-centric defaults
  (e.g. keyfob covers 315/433.92 MHz, cellular covers US LTE low/mid
  bands) — deployments elsewhere may need different ranges for their local
  spectrum allocation.
