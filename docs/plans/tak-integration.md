# SARfly → ATAK/TAK Integration — Implementation Plan

**Status:** planned, not started. **No code yet.** This doc is self-contained so
it can be executed later in a fresh context (e.g. via `claude-mem:do`).

## Goal

Bridge SARfly's localized RF detections into ATAK (Android Team Awareness Kit) —
the situational-awareness tool police/SAR teams use — so a responder with ATAK
sees SARfly's RF-detected device locations as **live map markers** that update in
place, with their uncertainty and an honest "unverified" label.

## Locked decisions (2026-08-12)

1. **Marker affiliation:** untagged detections → CoT type `a-u-G` (unknown ground,
   renders yellow in ATAK — the honest choice for a detected unknown). Devices the
   operator tagged **known** → `a-f-G` (friendly ground). Devices tagged **ignore**
   → not emitted at all.
2. **Confidence gate:** only emit a marker when the localization `confidence` is
   **≥ 0.4** (env-configurable, `TAK_MIN_CONFIDENCE`). Rationale: on real field
   data the localizer scored tight, trustworthy fixes at 0.9–1.0 and smeared /
   moving / low-information devices at ~0.0; 0.4 keeps moderate-or-better fixes and
   drops the junk so ATAK isn't flooded.
3. **Scope:** localized **devices** only (from `/api/localizations`). Spectrum
   energy hits have no point location, so they would be misleading as ATAK point
   markers and are excluded. (A future phase could render band-energy as an area
   overlay, but not as point markers.)

## Architecture decision: bridge, not fork

Add a **new `tak-bridge` service** (mirroring the scanner services) that consumes
SARfly's existing `/api/localizations` and emits CoT. Do **not** fork the web UI: a
fork duplicates the whole detection pipeline and doubles maintenance, whereas a
bridge reuses the localizer, DB, and API untouched and drops into the existing
Docker Compose model. The web UI and ATAK become two independent views of one
pipeline.

## Constraints (carry into every phase)

- **Offline-first / field use:** the SARfly AP (Pi, 5 GHz) has no internet. The
  default transport must work with no server and no uplink (UDP multicast mesh).
- **Minimal dependencies:** Python, matching the project's philosophy. `pytak` is
  the one new dependency (pure-Python, handles CoT + all TAK transports).
- **MAC-randomization caveat:** a device's MAC is not a stable long-term identity.
  Marker labels must not imply a confirmed person or a persistent identity.

---

## Phase 0 — Documentation Discovery (completed)

### Allowed APIs / verified facts (do not exceed these without re-checking docs)

**Transport — pytak** (https://pytak.readthedocs.io/en/latest/, /examples/, /configuration/):
- Pattern: `pytak.CLITool(config)` → `await clitool.setup()` →
  `clitool.add_tasks({Worker(clitool.tx_queue, config)})` → `await clitool.run()`.
- Custom sender: subclass `pytak.QueueWorker`, implement `run()`, call
  `put_queue(event_bytes)` to transmit.
- Helpers: `pytak.gen_cot(lat, lon)`, `pytak.cot_time()`.
- `COT_URL` config selects transport:
  - `udp+wo://239.2.3.1:6969` — **TAK Mesh SA multicast, write-only. DEFAULT.** No
    server; reaches ATAK devices on the same network (the SARfly AP).
  - `tcp://host:8087` / `tls://host:8089` — TAK Server (Phase 5).
  - `log://stdout` — debug; prints CoT instead of sending (Phase 2/6 testing).

**CoT event schema** (https://corvusintell.com/blog/c2-systems/cursor-on-target-cot-format/):
- `<event version uid type time start stale how>` with `<point lat lon hae ce le>`
  and `<detail>`.
- `uid` — **globally unique, persists across all reports for the same entity.**
  This is what makes a marker *update* rather than duplicate.
- `ce` — **circular error = 1-σ horizontal uncertainty in metres.** Maps directly
  onto our localization `semi_major_m`.
- `le`/`hae` — vertical error / height above ellipsoid; unknown for us → sentinels
  (e.g. `9999999.0` for unknown, per CoT convention).
- `type` — affiliation-domain atoms: `a-u-G` unknown ground, `a-f-G` friendly
  ground, `a-h-G` hostile.
- `stale` — receiver drops the marker after this time.
- `<detail>`: `contact` (has `callsign`), `remarks` (free text).

### Anti-patterns to guard against (grep/verify in the Final phase)

- A fresh/time-based `uid` each poll → duplicate, flickering markers. UID must be
  stable per MAC.
- Putting uncertainty anywhere other than `ce` (no invented `<detail>` schema).
- Inventing `<detail>` elements ATAK won't parse.
- Hand-rolling UDP sockets instead of using pytak's transport.
- Any affiliation/label implying a confirmed person or stable identity.

---

## Phase 1 — CoT mapping core (pure logic, no network, unit-tested)

**Implement** a pure function `localization_to_cot(mac, loc, cfg) -> bytes | None`
and small helpers, mapping one `/api/localizations` entry to a CoT event:
- `uid = f"sarfly-{mac}"` — stable per MAC.
- `point.lat/lon` from the estimate; `point.ce = loc["semi_major_m"]`;
  `hae`/`le` = unknown sentinels.
- `type` per **locked decision 1** (status → `a-u-G` / `a-f-G` / skip).
- `contact.callsign` = short honest label reflecting the MAC caveat, e.g.
  `RF?-{last 4 of MAC}`; append `(unverified)`.
- `remarks` = device name, confidence, full MAC, sample count, RSSI span, source
  unit — the context an operator needs, clearly hypothesis-not-fact.
- `stale` = now + `TAK_STALE_S` (default 300 s).
- Return `None` when below **locked decision 2** confidence gate or status=ignore.

**Docs to copy from:** CoT field names from Phase 0; `pytak.gen_cot` / `cot_time`
signatures from pytak examples.

**Verify:** unit tests — UID identical across two calls for the same MAC; `ce`
equals `semi_major_m`; correct type per status; `None` below gate / on ignore;
times are valid ISO-8601 and `stale > start`.

**Guards:** UID never time-based; uncertainty only in `ce`; no invented detail tags.

## Phase 2 — `tak-bridge` service skeleton + multicast transport

**Implement** a pytak `CLITool` plus a `pytak.QueueWorker` subclass; read `COT_URL`
from env (default `udp+wo://239.2.3.1:6969`, `log://stdout` for dev). Copy the
minimal send-loop structure from pytak's Examples page.

**Verify:** with `COT_URL=log://stdout`, well-formed CoT prints to console; with
multicast, a listener on the group receives it (see Phase 6).

**Guards:** use pytak transport only (no raw sockets).

## Phase 3 — Wire to SARfly data

**Implement:** in the worker `run()` loop, poll `GET /api/localizations` every
`TAK_POLL_S` (default 10 s) from `GROUND_STATION_URL` (default
`http://127.0.0.1:8080`). For each device passing the gate, build via Phase 1 and
`put_queue`. Let `stale` age out devices that stop being localizable (no explicit
delete needed). Tolerate the ground station being briefly unavailable (retry next
poll). Reading the HTTP API is preferred over the SQLite file — it decouples from
the DB schema and reuses the localizer's confidence/ellipse output.

**Verify:** markers update in place (stable UID, no duplicates); a device that
drops out ages out of ATAK; a ground-station blip doesn't crash the bridge.

**Guards:** never mint a new UID per poll.

## Phase 4 — Docker integration + field deployment

**Implement:** add a `tak-bridge` service to `docker-compose.yml`:
`build: ./services/tak-bridge`, `image: sarfly-tak-bridge:local`,
`restart: unless-stopped`, shared `logging` anchor, **`network_mode: host`** (so
multicast reaches wireless ATAK clients on the SARfly AP), env for `COT_URL`,
`TAK_POLL_S`, `TAK_MIN_CONFIDENCE`, `TAK_STALE_S`, `GROUND_STATION_URL`. Deploy with
the established `--no-deps` pattern so scanners are undisturbed. Requirements:
`pytak`.

**Verify:** an ATAK device (or emulator) joined to the SARfly 5 GHz AP sees SARfly
markers appear and update live, with **no internet**. Confirm the AP passes
multicast to wireless clients.

**Guards:** confirm host networking (bridge networking would trap multicast).

## Phase 5 — TAK Server path (optional, later)

For multi-team / over-internet / persistence beyond mesh line-of-sight: stand up
**FreeTAKServer** (open-source) or point at an existing TAK Server, and switch
`COT_URL` to `tcp://`/`tls://` (+ client certs for TLS). Phases 1–3 need no change —
it is a config/transport swap. This decoupling is the reason for using pytak.

## Phase 6 — Testing without ATAK hardware

Layered: (1) `log://stdout` for CoT correctness; (2) a multicast listener
(`pytak` RX, or a small Python UDP socket / `socat` on `239.2.3.1:6969`) to confirm
wire delivery; (3) **FreeTAKServer** locally as a visual sink; (4) real ATAK /
WinTAK on the SARfly AP for final acceptance.

## Final — Verification

End-to-end on the SARfly AP with an ATAK client: RF-detected device locations
appear as markers with correct position, an uncertainty ring from `ce`, honest
"unverified" labels, and live updates. Grep for anti-patterns (time-based UIDs,
invented detail tags, raw sockets). All Phase 1 unit tests green.

## New/changed files (anticipated)

- `services/tak-bridge/app/cot.py` — Phase 1 pure mapping (+ tests).
- `services/tak-bridge/app/main_tak.py` — Phase 2/3 pytak worker + poll loop.
- `services/tak-bridge/{Dockerfile,requirements.txt,pytest.ini}`.
- `services/tak-bridge/tests/` — Phase 1 unit tests.
- `docker-compose.yml` — add `tak-bridge` service (Phase 4).
- `README.md` — TAK/ATAK usage + field setup notes.

## Sources

- PyTAK docs: https://pytak.readthedocs.io/en/latest/
- PyTAK examples: https://pytak.readthedocs.io/en/latest/examples/
- PyTAK configuration: https://pytak.readthedocs.io/en/latest/configuration/
- pytak (GitHub): https://github.com/snstac/pytak
- CoT format reference: https://corvusintell.com/blog/c2-systems/cursor-on-target-cot-format/
