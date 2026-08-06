# Component G: RTL-SDR Spectrum Scanner — Design

## 1. Motivation

Component F (the HackRF-based multi-band spectrum scanner) works, but a
HackRF One costs ~$300 and isn't something most volunteer SAR teams already
own. An RTL-SDR dongle costs ~$25-30 and is genuinely common — a lot of
teams, or individual members, already have one from another hobby (ham
radio, ADS-B tracking, scanning). Lowering the hardware bar for this
capability matters more than the capability itself being best-in-class.

This component is the same idea as Component F — sweep fixed bands, flag
energy above a calibrated per-flight baseline, log a GPS-tagged hit — built
on cheaper, more widely-owned hardware with a smaller usable frequency range.
It is not a replacement for Component F; a team with a HackRF should still
prefer it for its wider band coverage. This is an additional, independent,
lower-barrier-to-entry option.

## 2. Placement in the existing architecture

Per `docs/ARCHITECTURE.md`, this is a new **Component G**, deployed
alongside Component F in Phase 1. It writes into the same shared
`spectrum_hits` table Component F already created, tagged with its own
`SOURCE_UNIT_ID` — no new database schema, ground-station API endpoint, or
UI work is needed (see Section 6). A hit from either component is
indistinguishable to the ground-station UI/API; `source_unit_id` in the row
is the only thing that tells them apart, same as the two independent BLE
scanners today.

## 3. Hardware, verified against the actual dongle

Verified directly against the physical dongle attached to the project's
Raspberry Pi 5 (not assumed from a datasheet):

- **Chipset**: RTL2838 (RTL2832U-compatible demodulator) + **Rafael Micro
  R820T** tuner, confirmed via `rtl_test -t`.
- **Tunable range**: ~24 MHz–1766 MHz (the R820T's real limit). This is a
  hard physical constraint, not a driver/config choice — **this hardware
  cannot reach the 2.4 GHz ISM band at all**, and can only reach the bottom
  ~56 MHz sliver (1710–1766 MHz) of Component F's `cellular_mid` band.
- **Instantaneous bandwidth**: ~1-2.8 MHz per tune step (`rtl_power`'s own
  help text caps `bin_size` at 2.8 MHz), vs. the HackRF's 20 MHz — sweeping
  the same span takes proportionally more retune steps and wall-clock time.
- **Sweep tool**: `rtl_power`, packaged in Debian's `rtl-sdr` package —
  **no from-source build needed**, unlike Ubertooth/HackRF (Debian bookworm
  ships a current-enough version). This is a real simplification over
  Component F's Dockerfile.

Per the human's decision (see conversation), the default band list is scoped
to what this hardware can actually and fully cover — no truncated or
misleadingly-named partial bands:

| Band | Range | Note |
|---|---|---|
| Keyfob | 300–450 MHz | Identical range to Component F's keyfob band — same rationale (315/433.92 MHz remotes) |
| Cellular low | 698–960 MHz | Identical range to Component F's cellular_low band |

`cellular_mid` and `ism_2_4ghz` are omitted entirely for this backend, not
truncated — a same-named band covering a different range than Component F's
would be a silent, confusing inconsistency in the `spectrum_hits` table
(two rows with `band="cellular_mid"` meaning different frequency ranges
depending on which scanner wrote them). If this component's band list needs
to grow later (e.g. FM broadcast at 88-108MHz, well within R820T range, is a
plausible future add — someone lost with a car radio scanning for signal is
a real if weaker signature), that's a separate decision, not implied by this
plan.

## 4. `rtl_power`'s CSV output — verified, and where it differs from `hackrf_sweep`

Verified directly against the real dongle (not assumed):

```
$ rtl_power -f 433.0M:434.0M:10k -1 -
2026-08-05, 20:11:31, 433000000, 434000000, 7812.50, 71488, -23.61, -24.44, -24.33, ...
```

Field order — `date, time, hz_low, hz_high, hz_bin_width, num_samples, dB,
dB, ...` — is **identical in shape** to `hackrf_sweep`'s format that
`services/scanner-spectrum/app/spectrum_source.py`'s `parse_sweep_line`
already parses. The pure parsing/band/detection logic from that module
(`parse_sweep_line`, `bin_center_freqs`, `expand_readings`,
`_readings_from_line`, `band_for_freq`, `average_power`,
`average_power_by_freq`, `detect_hits`, `_readings_in_band`,
`SpectrumHitReading`) can be duplicated into this new service verbatim,
following the same "duplication is the established cross-service reuse
pattern in this repo" precedent `gps_client.py` already set (there is no
shared-package infrastructure across independently-built Docker images
here).

**Two real behavioral differences from `hackrf_sweep`, verified by running
both tools, that the subprocess wrapper (this component's version of
`_run_hackrf_sweep`) must account for — get these wrong and the scanner
either hangs, or silently collects nothing:**

1. **`rtl_power` is designed to exit on its own; `hackrf_sweep` is not.**
   `hackrf_sweep -f low:high` sweeps forever until killed — Component F's
   fix (`_run_hackrf_sweep`) treats *any* exit before the caller's
   `duration_s` timeout as a failure, unconditionally. `rtl_power -1`
   (single-shot mode) is the opposite: it runs **one** reporting interval
   (`-i seconds`) across the whole requested range and then exits
   successfully with output already written. That means this component's
   subprocess call should be built as **one `rtl_power -1` invocation per
   calibration/dwell window** (`-i` set to the window's duration), not a
   long-running process cut off by an external timeout — closer in spirit
   to "make one blocking call and collect its result" than to
   `_run_hackrf_sweep`'s "stream lines until our own timeout fires."
   Failure detection also differs: a real device-gone failure was verified
   to fail almost instantly (58ms, exit code 1, zero stdout lines) —
   dramatically faster than any real sweep completing. "Exited before
   producing output, or well before the requested interval could plausibly
   have elapsed" is this component's equivalent failure signal, not "exited
   before timeout at all" (which is `rtl_power`'s *normal, successful* case
   in single-shot mode).

2. **`rtl_power` does not stream lines incrementally.** Verified: over a
   150-hop sweep, redirecting stdout to a file showed **zero bytes written**
   until the whole pass completed, then all 150 lines appeared at once
   (likely internal buffering across the full reporting interval, not
   per-line flushing). `hackrf_sweep`'s CSV output, by contrast, streams
   continuously as it sweeps (Component F's `_run_hackrf_sweep` reads it
   line-by-line via `async for raw_line in proc.stdout`). This component's
   subprocess wrapper should **not** assume incremental output is available
   to react to mid-sweep — collect all of stdout after the process exits
   (or is killed), then parse it as one batch.

3. **SIGTERM handling has pass-completion latency, not instant response.**
   Verified: sending SIGTERM mid-sweep produced `Signal caught, finishing
   scan pass.` in stderr — `rtl_power` finishes its current reporting
   interval before exiting, rather than stopping immediately the way
   `hackrf_sweep` was observed to. A short SIGTERM grace period (Component
   F used 5s, sized for `hackrf_sweep`'s near-instant response) may be too
   short here if the in-progress interval has meaningfully more than 5s
   left to run — size the grace period relative to the actual dwell/
   calibration duration in use, not a fixed constant, or accept more
   frequent SIGKILL escalation as a lower-severity cost (the bounded-wait
   SIGKILL-escalation code Component F's hardening added is still the
   correct safety net either way — this is a tuning note, not a new bug
   class).

## 5. Calibration & detection algorithm

Same design as Component F, reusing its already-hardened logic verbatim
where the interface matches: per-frequency-bin calibrated baseline (not a
per-band scalar — Component F's post-launch findings showed why a per-band
average produces persistent false positives on real ambient RF), threshold
detection at baseline + margin, out-of-band reading filtering before it can
pollute a band's baseline or get mislabeled. `DEFAULT_CALIBRATION_S`/
`DEFAULT_DWELL_S`/`DEFAULT_MARGIN_DB` may need different defaults than
Component F's (10s/5s/10dB) given the narrower instantaneous bandwidth means
more retune steps per band per interval — the implementation task should
measure real sweep timing against the actual dongle before picking final
defaults, the same way Component F's task 7 validated against real
hardware rather than guessing.

## 6. Ground-station integration — none needed

This is the biggest scope reduction versus Component F. Because this
component reuses the existing `spectrum_hits` table and the two bands it
uses (`keyfob`, `cellular_low`) already have defined colors in
`services/ground_station/app/static/index.html`'s `BAND_COLORS` map (yellow
and orange respectively, from Component F), **zero ground-station changes
are required** — no new DB methods, no new API endpoint, no new WebSocket
message type, no new UI toggle. A hit from this component simply appears on
the existing "Spectrum hits" map layer, colored the same as the equivalent
HackRF-sourced band, distinguishable only by `source_unit_id` in the
underlying row if that ever matters operationally.

## 7. Non-goals

- Not attempting to cover `cellular_mid` or `ism_2_4ghz` with this hardware
  (Section 3) — a future FM-broadcast or other R820T-range band is a
  separate decision, not implied here.
- Not building a pluggable "either HackRF or RTL-SDR" backend inside
  `scanner-spectrum` — this is a new, independent, always-optional service
  (`scanner-rtlsdr`), matching how `scanner`/`scanner-ubertooth`/
  `scanner-spectrum` are already independent, additive units that each
  retry quietly if their hardware isn't attached.
- Not deduplicating logic across `scanner-spectrum` and the new service via
  a shared package — duplication is this repo's established pattern for
  independently-built Docker images (see `gps_client.py`).
