# Component G: RTL-SDR Spectrum Scanner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second, independent multi-band spectrum scanner backed by a cheap, widely-owned RTL-SDR dongle instead of the HackRF One that Component F uses — same detection philosophy (per-bin calibrated baseline, GPS-tagged hits into the shared `spectrum_hits` table), scoped to the two bands (keyfob, cellular low) this hardware can actually and fully cover.

**Full design reference:** `docs/superpowers/specs/2026-08-06-rtlsdr-spectrum-scanner-design.md` — read this first. It documents real, verified-against-hardware facts (tuner range, `rtl_power`'s CSV format and process-lifecycle behavior) that this plan's code depends on. Do not re-derive these from assumption; they were checked against the actual dongle attached to the project's Raspberry Pi.

**Architecture:** A new self-contained `services/scanner-rtlsdr/` service, structurally parallel to `services/scanner-spectrum/` (Component F) but wrapping `rtl_power` instead of `hackrf_sweep`. Reuses Component F's already-hardened pure logic (per-bin baseline, out-of-band filtering, detect_hits) by duplication — this repo's established cross-service-directory reuse pattern (see `services/scanner-spectrum/app/gps_client.py`, a verbatim copy of `services/scanner/app/gps_client.py`) — rather than a shared package. Writes into the **same** `spectrum_hits` table Component F already created, via its own `SOURCE_UNIT_ID`. **No ground-station changes are needed at all** — see the design doc Section 6.

**Tech Stack:** Python 3.12 (stdlib only), `rtl_power` (from Debian's packaged `rtl-sdr` — no from-source build needed, unlike Ubertooth/HackRF), SQLite.

## Global Constraints

- Python 3.12-slim base image for the final stage (matches every other scanner service).
- No new third-party Python dependencies — stdlib only (`asyncio`, `re`, `time`, `sqlite3`, `dataclasses`).
- Every env var read via a local `_env(name, default)` helper, UPPER_SNAKE_CASE, documented in README.
- Every SQLite connection uses `isolation_level=None` (autocommit).
- Any subprocess-backed data source must retry forever every 5s on failure or a missing device, never crash — same philosophy as every other scanner, but see the design doc Section 4 for why `rtl_power`'s actual failure signal differs from `hackrf_sweep`'s.
- `spectrum_hits` already exists (created by Component F's `scanner-spectrum`); this service is a second writer, not a creator — do not add a `CREATE TABLE` for it here.
- Git commits end with:
  ```
  Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
  ```

---

### Task 1: `rtlsdr_source.py` — pure parsing/band/detection logic (duplicated from Component F, band list trimmed)

**Files:**
- Create: `services/scanner-rtlsdr/app/rtlsdr_source.py`
- Create: `services/scanner-rtlsdr/tests/test_rtlsdr_source.py`
- Create: `services/scanner-rtlsdr/pytest.ini`

**Interfaces:**
- Produces: `Band`, `DEFAULT_BANDS`, `SpectrumHitReading`, `parse_sweep_line`, `bin_center_freqs`, `expand_readings`, `_readings_from_line`, `band_for_freq`, `average_power`, `average_power_by_freq`, `detect_hits`, `_readings_in_band`

- [ ] **Step 1: Copy the pure-logic half of Component F's `spectrum_source.py` verbatim, with one change**

Read `services/scanner-spectrum/app/spectrum_source.py` in full. Copy everything from the top of the file (module docstring, imports, `Band`, `SpectrumHitReading`, `_LINE_RE`, `parse_sweep_line`, `bin_center_freqs`, `expand_readings`, `_readings_from_line`, `band_for_freq`, `average_power`, `average_power_by_freq`, `detect_hits`, `_readings_in_band`) into `services/scanner-rtlsdr/app/rtlsdr_source.py` **unchanged**, except:

1. Update the module docstring to describe this as the RTL-SDR variant (wraps `rtl_power` instead of `hackrf_sweep`) — reference `docs/superpowers/specs/2026-08-06-rtlsdr-spectrum-scanner-design.md` instead of the Component F design doc.
2. Replace `DEFAULT_BANDS` with:
   ```python
   # Scoped to what the RTL-SDR's R820T tuner can actually and fully cover
   # (~24 MHz-1766 MHz, verified against real hardware) -- see the design
   # doc Section 3 for why cellular_mid and ism_2_4ghz are intentionally
   # absent here rather than truncated to a same-named-but-different range.
   DEFAULT_BANDS: list[Band] = [
       Band("keyfob", 300_000_000, 450_000_000),
       Band("cellular_low", 698_000_000, 960_000_000),
   ]
   ```

Do not copy `_run_hackrf_sweep`, `_sweep_band`, or `stream_hits` from Component F — those are HackRF-specific subprocess plumbing; this service's equivalents are written fresh in Task 4 against `rtl_power`'s different, verified behavior.

Do not copy Component F's `DEFAULT_MARGIN_DB`/`DEFAULT_CALIBRATION_S`/`DEFAULT_DWELL_S` constants yet — those live with the subprocess plumbing in Task 4, where their values may need to differ given `rtl_power`'s narrower per-hop bandwidth (more retune steps per band).

- [ ] **Step 2: Write tests**

Create `services/scanner-rtlsdr/pytest.ini`:
```ini
[pytest]
pythonpath = app
```

Create `services/scanner-rtlsdr/tests/test_rtlsdr_source.py` with the same test coverage as Component F's `test_spectrum_source.py` Task 1 + Task 2/Task 4-fix-era tests for the functions this file actually contains — copy the existing, already-reviewed test bodies for `parse_sweep_line`, `bin_center_freqs`, `expand_readings`, `_readings_from_line`, `band_for_freq`, `average_power`, `average_power_by_freq`, `detect_hits` (4-argument signature, with the fallback-baseline test), and `_readings_in_band` from `services/scanner-spectrum/tests/test_spectrum_source.py` verbatim, **except**:

- `test_band_for_freq_matches_ism` and any test asserting on `ism_2_4ghz` or `cellular_mid` band membership must be dropped or rewritten against `DEFAULT_BANDS`' actual two entries (e.g. rewrite as `test_band_for_freq_matches_keyfob` asserting `band_for_freq(350_000_000) == "keyfob"`).
- `test_band_for_freq_boundary_is_inclusive_low_exclusive_high` should use `"cellular_low"` (or `"keyfob"`) instead of `"ism_2_4ghz"` as the band under test, since that band no longer exists in this file's `DEFAULT_BANDS`.
- `test_band_for_freq_no_match_outside_any_default_band` should use a frequency outside both remaining bands (e.g. `2_450_000_000`, which was `ism_2_4ghz`'s own range in Component F and is correctly unmatched here since that band doesn't exist in this file).

- [ ] **Step 3: Run tests, verify pass**

```bash
cd services/scanner-rtlsdr && python3 -m venv .venv && .venv/bin/pip install pytest && .venv/bin/pytest tests/ -v
```
Expected: all tests pass (should be close to Component F's Task 1+2/4-era count minus the 2-3 ism/cellular_mid-specific tests removed/rewritten in Step 2).

- [ ] **Step 4: Commit**

```bash
git add services/scanner-rtlsdr/app/rtlsdr_source.py services/scanner-rtlsdr/tests/test_rtlsdr_source.py services/scanner-rtlsdr/pytest.ini
git commit -m "$(cat <<'EOF'
Add rtlsdr_source.py pure logic (duplicated from Component F, 2-band list)

Same per-bin-baseline/out-of-band-filtering pure logic Component F's
spectrum_source.py already has hardened and tested, duplicated per
this repo's established cross-service-directory reuse pattern.
DEFAULT_BANDS is trimmed to keyfob + cellular_low -- the two bands the
RTL-SDR's real R820T tuner range (~24MHz-1766MHz, verified against
hardware) can fully cover, per the Component G design doc.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `storage.py` — SQLite persistence (verbatim copy)

**Files:**
- Create: `services/scanner-rtlsdr/app/storage.py`
- Create: `services/scanner-rtlsdr/tests/test_storage.py`

**Interfaces:** `SpectrumHit`, `SpectrumStorage(db_path)` with `.insert_hit(h)` and `.close()`

- [ ] **Step 1: Copy verbatim**

Copy `services/scanner-spectrum/app/storage.py` to `services/scanner-rtlsdr/app/storage.py` **unchanged** — it already targets the shared `spectrum_hits` table with `CREATE TABLE IF NOT EXISTS`, `isolation_level=None`, and WAL mode, all of which apply identically here (this service is a second writer to the same table). Copy `services/scanner-spectrum/tests/test_storage.py` to `services/scanner-rtlsdr/tests/test_storage.py` unchanged too.

- [ ] **Step 2: Run tests, verify pass**

```bash
cd services/scanner-rtlsdr && .venv/bin/pytest tests/ -v
```

- [ ] **Step 3: Commit**

```bash
git add services/scanner-rtlsdr/app/storage.py services/scanner-rtlsdr/tests/test_storage.py
git commit -m "$(cat <<'EOF'
Copy storage.py into scanner-rtlsdr (same shared spectrum_hits table)

Verbatim copy of Component F's SpectrumStorage -- this service is a
second, independent writer into the same spectrum_hits table (created
by scanner-spectrum's CREATE TABLE IF NOT EXISTS), distinguished only
by SOURCE_UNIT_ID, matching how the two BLE scanners already share
the detections table.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `gps_client.py` — verbatim copy

**Files:**
- Create: `services/scanner-rtlsdr/app/gps_client.py`

- [ ] **Step 1: Copy exactly**

Copy `services/scanner/app/gps_client.py` (the original, canonical copy) to `services/scanner-rtlsdr/app/gps_client.py` with byte-identical content.

- [ ] **Step 2: Verify**

```bash
diff services/scanner/app/gps_client.py services/scanner-rtlsdr/app/gps_client.py
```
Expected: no output.

- [ ] **Step 3: Commit**

```bash
git add services/scanner-rtlsdr/app/gps_client.py
git commit -m "$(cat <<'EOF'
Copy gps_client.py into scanner-rtlsdr

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `rtl_power` subprocess wrapper, calibration, and sweep loop

**Files:**
- Modify: `services/scanner-rtlsdr/app/rtlsdr_source.py` (append)
- Modify: `services/scanner-rtlsdr/tests/test_rtlsdr_source.py` (append)

**Interfaces:**
- Consumes: everything from Task 1
- Produces: `DEFAULT_MARGIN_DB`, `DEFAULT_CALIBRATION_S`, `DEFAULT_DWELL_S`, `async stream_hits(margin_db=..., calibration_s=..., dwell_s=..., bands=None) -> AsyncIterator[SpectrumHitReading]`

This is the impure half — subprocess management and calibrate-then-sweep orchestration. Read the design doc's Section 4 before writing this task; it documents three verified, load-bearing differences from `hackrf_sweep`'s behavior (which this file must NOT copy uncritically from Component F):

1. `rtl_power -1` (single-shot) is *designed* to exit on its own after one `-i`-second reporting interval — a real device failure was verified to fail almost instantly (tens of ms, exit code 1, zero stdout) by contrast. So "exited far sooner than the interval could plausibly allow" is the failure signal, not "exited before an external timeout" (which is `hackrf_sweep`'s signal, and is `rtl_power`'s *normal successful* case).
2. `rtl_power` does not stream output incrementally — verified: stdout stays empty until the whole pass completes. Use `proc.communicate()` (collect everything after the process exits) rather than reading line-by-line.
3. `rtl_power` was verified to delay exiting after SIGTERM until it finishes its current scan pass (`Signal caught, finishing scan pass.` in stderr) — size any grace period off the sweep's own duration, not a small fixed constant.

- [ ] **Step 1: Write the failing tests**

Append to `services/scanner-rtlsdr/tests/test_rtlsdr_source.py`:

```python
import asyncio
import os

import pytest


@pytest.fixture
def fake_rtl_power(tmp_path, monkeypatch):
    """Fakes rtl_power's *actual* verified behavior: batches all output at
    once right before exiting (not streamed), and exits successfully on its
    own after roughly its -i interval -- unlike hackrf_sweep, which never
    exits on its own while sweeping.
    """
    script = tmp_path / "rtl_power"
    script.write_text(
        "#!/bin/sh\n"
        "sleep 0.05\n"
        'echo "2026-08-05, 20:11:31, 433000000, 434000000, 7812.50, 71488, -23.61, -24.44"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return script


@pytest.fixture
def fake_rtl_power_device_gone(tmp_path, monkeypatch):
    """Real rtl_power was verified to fail this way when the device index
    doesn't exist: exit code 1, near-instant (tens of ms), zero stdout,
    a "No matching devices found." stderr line.
    """
    script = tmp_path / "rtl_power"
    script.write_text(
        "#!/bin/sh\n"
        "echo 'No matching devices found.' >&2\n"
        "exit 1\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    return script


def test_run_rtl_power_collects_readings(fake_rtl_power):
    from rtlsdr_source import _run_rtl_power

    readings = asyncio.run(_run_rtl_power(433_000_000, 434_000_000, duration_s=1.0))
    assert (433003906, -23.61) in readings or any(f == 433003906 for f, _ in readings)


def test_run_rtl_power_raises_when_binary_missing(tmp_path, monkeypatch):
    from rtlsdr_source import _run_rtl_power

    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        asyncio.run(_run_rtl_power(433_000_000, 434_000_000, duration_s=1.0))


def test_run_rtl_power_raises_when_device_gone(fake_rtl_power_device_gone):
    from rtlsdr_source import _run_rtl_power

    with pytest.raises(OSError, match="device likely unavailable"):
        asyncio.run(_run_rtl_power(433_000_000, 434_000_000, duration_s=2.0))


def test_run_rtl_power_does_not_misflag_a_real_full_duration_sweep(tmp_path, monkeypatch):
    """A real sweep that legitimately takes close to the full requested
    duration (not the fast device-gone path) must not be treated as a
    failure just because it exited "before an external timeout" -- that
    was hackrf_sweep's failure signal, not rtl_power's.
    """
    from rtlsdr_source import _run_rtl_power

    script = tmp_path / "rtl_power"
    script.write_text(
        "#!/bin/sh\n"
        "sleep 0.3\n"
        'echo "2026-08-05, 20:11:31, 433000000, 434000000, 7812.50, 1, -20.0"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")

    readings = asyncio.run(_run_rtl_power(433_000_000, 434_000_000, duration_s=0.3))
    assert readings  # did not raise, and collected the reading
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
.venv/bin/pytest tests/test_rtlsdr_source.py -v -k rtl_power
```
Expected: `ImportError: cannot import name '_run_rtl_power' from 'rtlsdr_source'`

- [ ] **Step 3: Write the implementation**

Append to `services/scanner-rtlsdr/app/rtlsdr_source.py`:

```python
import asyncio
import logging
import time

logger = logging.getLogger(__name__)

DEFAULT_MARGIN_DB = 10.0
DEFAULT_CALIBRATION_S = 15.0
DEFAULT_DWELL_S = 8.0


async def _run_rtl_power(
    low_hz: int, high_hz: int, duration_s: float, bin_hz: int = 1_000_000
) -> list[tuple[int, float]]:
    """Run one rtl_power single-shot sweep over [low_hz, high_hz) for one
    ~duration_s reporting interval and return every (freq_hz, power_dbm)
    reading collected.

    Unlike hackrf_sweep (which never exits on its own while sweeping),
    rtl_power's -1 (single-shot) mode is *designed* to exit on its own
    after one -i-second interval -- verified against real hardware. A
    genuine device failure was verified to fail almost instantly (tens of
    ms, exit code 1, zero stdout) by contrast, so "exited far sooner than
    duration_s could plausibly allow" is this function's failure signal,
    not "exited at all."

    rtl_power does not stream output incrementally -- verified: stdout
    stays empty until the whole pass completes -- so this collects
    everything via proc.communicate() rather than reading line-by-line the
    way hackrf_sweep's wrapper does.
    """
    args = ["rtl_power", "-f", f"{low_hz}:{high_hz}:{bin_hz}", "-i", str(duration_s), "-1", "-"]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    start = time.monotonic()
    # rtl_power was verified to delay exiting after SIGTERM until it
    # finishes its current scan pass ("Signal caught, finishing scan
    # pass."), so this grace period is sized off duration_s itself rather
    # than a small fixed constant -- a short fixed grace would routinely
    # SIGKILL a healthy process still finishing its pass.
    budget_s = duration_s + max(duration_s, 5.0)
    try:
        stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=budget_s)
    except asyncio.TimeoutError:
        proc.terminate()
        try:
            stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=duration_s + 5.0)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            except asyncio.TimeoutError:
                raise OSError(
                    f"rtl_power (pid={proc.pid}) did not exit even after SIGKILL "
                    f"while sweeping {low_hz}:{high_hz} Hz"
                ) from None

    elapsed = time.monotonic() - start
    readings: list[tuple[int, float]] = []
    for raw_line in stdout_data.decode(errors="replace").splitlines():
        readings.extend(_readings_from_line(raw_line))

    # A real device-gone failure was verified to fail in ~58ms; a real
    # sweep, even a short one, takes a meaningful fraction of duration_s.
    # 50% margin distinguishes "fast-failed" from "legitimately quick."
    if proc.returncode not in (0, None) and elapsed < duration_s * 0.5:
        stderr_text = stderr_data.decode(errors="replace").strip() or "(no stderr output captured)"
        raise OSError(
            f"rtl_power exited early (returncode={proc.returncode}, after "
            f"{elapsed:.2f}s of a {duration_s:.0f}s window) while sweeping "
            f"{low_hz}:{high_hz} Hz -- device likely unavailable: {stderr_text}"
        )

    return readings


async def _sweep_band(band: Band, duration_s: float) -> list[tuple[int, float]]:
    """Run _run_rtl_power for one band, retrying forever every 5s if the
    binary is missing or the sweep fails -- same reconnect philosophy as
    GpsClient.run() and Component F's _sweep_band.
    """
    while True:
        try:
            return await _run_rtl_power(band.low_hz, band.high_hz, duration_s)
        except FileNotFoundError:
            logger.error("rtl_power not found on PATH; is the rtl-sdr package installed?")
            await asyncio.sleep(5)
        except OSError as exc:
            logger.warning("rtl_power failed for band %s (%s); retrying in 5s", band.name, exc)
            await asyncio.sleep(5)


async def stream_hits(
    margin_db: float = DEFAULT_MARGIN_DB,
    calibration_s: float = DEFAULT_CALIBRATION_S,
    dwell_s: float = DEFAULT_DWELL_S,
    bands: list[Band] | None = None,
):
    """Calibrate a per-frequency-bin baseline across all bands once, then
    sweep them forever, yielding SpectrumHitReading for every in-band
    reading that exceeds its own bin's calibrated baseline + margin_db.

    Mirrors Component F's already-hardened stream_hits (per-bin baseline,
    out-of-band filtering, retry-forever on zero-reading calibration) --
    see docs/superpowers/specs/2026-08-06-rtlsdr-spectrum-scanner-design.md
    Section 5.
    """
    active_bands = bands if bands is not None else DEFAULT_BANDS

    baseline_by_freq: dict[str, dict[int, float]] = {}
    fallback_baseline: dict[str, float] = {}
    logger.info("calibrating baseline for %d bands (%.0fs each)...", len(active_bands), calibration_s)
    for band in active_bands:
        while True:
            readings = _readings_in_band(await _sweep_band(band, calibration_s), band)
            if readings:
                break
            logger.warning("zero in-band readings calibrating %s; retrying in 5s", band.name)
            await asyncio.sleep(5)
        powers = [p for _, p in readings]
        baseline_by_freq[band.name] = average_power_by_freq(readings)
        fallback_baseline[band.name] = average_power(powers)
        logger.info(
            "baseline[%s] = %.1f dBm avg across %d bins (%d samples)",
            band.name, fallback_baseline[band.name], len(baseline_by_freq[band.name]), len(powers),
        )

    while True:
        for band in active_bands:
            readings = _readings_in_band(await _sweep_band(band, dwell_s), band)
            for hit in detect_hits(
                readings, band.name, baseline_by_freq[band.name], margin_db, fallback_baseline[band.name]
            ):
                yield hit
```

- [ ] **Step 4: Run tests, verify pass**

```bash
.venv/bin/pytest tests/ -v
```
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add services/scanner-rtlsdr/app/rtlsdr_source.py services/scanner-rtlsdr/tests/test_rtlsdr_source.py
git commit -m "$(cat <<'EOF'
Add rtl_power subprocess wrapper and calibrate-then-sweep loop

_run_rtl_power wraps rtl_power's single-shot (-1) mode, which -- unlike
hackrf_sweep -- is designed to exit on its own after one reporting
interval and does not stream output incrementally (both verified
against the real dongle). Failure detection uses "exited far sooner
than the interval could plausibly allow" rather than hackrf_sweep's
"exited before an external timeout" signal. _sweep_band/stream_hits
otherwise mirror Component F's already-hardened retry-forever and
per-bin-baseline logic.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `main_rtlsdr.py` entrypoint

**Files:**
- Create: `services/scanner-rtlsdr/app/main_rtlsdr.py`

**Interfaces:** `run() -> None` (async), `main() -> None`

No new test — thin orchestration glue, matching `main_ubertooth.py`/`main_spectrum.py`'s precedent of no test for this layer.

- [ ] **Step 1: Write the entrypoint**

Create `services/scanner-rtlsdr/app/main_rtlsdr.py`, structurally identical to `services/scanner-spectrum/app/main_spectrum.py` with these substitutions:
- Module docstring describes this as the RTL-SDR variant, references the Component G design doc.
- Imports from `rtlsdr_source` instead of `spectrum_source`.
- `SOURCE_UNIT_ID` default: `"ground-logger-rtlsdr"` (env var `SOURCE_UNIT_ID`, same variable name as every other scanner).
- Env vars: `RTLSDR_CALIBRATION_S` (default `str(DEFAULT_CALIBRATION_S)`), `RTLSDR_DWELL_S` (default `str(DEFAULT_DWELL_S)`), `RTLSDR_HIT_MARGIN_DB` (default `str(DEFAULT_MARGIN_DB)`).
- `logger = logging.getLogger("scanner-rtlsdr")`.
- The per-hit log line uses `logger.debug` (not `logger.info`) — matching the established convention (`main.py`, `main_ubertooth.py`, and Component F's own post-review fix all use `debug` for per-event logging; `info` is reserved for startup/lifecycle messages).

Read `services/scanner-spectrum/app/main_spectrum.py` in full before writing this file — it is the template.

- [ ] **Step 2: Sanity-check it imports cleanly**

```bash
cd services/scanner-rtlsdr && .venv/bin/python -c "import sys; sys.path.insert(0, 'app'); import main_rtlsdr"
```
Expected: no output, exit code 0.

- [ ] **Step 3: Commit**

```bash
git add services/scanner-rtlsdr/app/main_rtlsdr.py
git commit -m "$(cat <<'EOF'
Add main_rtlsdr.py entrypoint

Structurally identical to main_spectrum.py, wired to rtlsdr_source
instead of spectrum_source. Per-hit logging is DEBUG from the start
(main_spectrum.py originally shipped this at INFO and had to be fixed
after a real-hardware bench run showed log-flood risk at high hit
rates -- no reason to reintroduce that here).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Dockerfile and docker-compose.yml wiring

**Files:**
- Create: `services/scanner-rtlsdr/Dockerfile`
- Modify: `docker-compose.yml`

- [ ] **Step 1: Write the Dockerfile**

Create `services/scanner-rtlsdr/Dockerfile`:

```dockerfile
FROM python:3.12-slim

# Unlike Ubertooth/HackRF, Debian's packaged rtl-sdr is current enough --
# no from-source build needed here (verified: rtl_power's CSV output and
# process-lifecycle behavior were checked against exactly this package on
# the target Raspberry Pi).
RUN apt-get update && apt-get install -y --no-install-recommends \
        rtl-sdr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/ .

ENTRYPOINT ["python", "main_rtlsdr.py"]
```

- [ ] **Step 2: Build it**

```bash
cd /home/seraph/Projects/ble-sar-df && docker build -t ble-sar-df-scanner-rtlsdr:local services/scanner-rtlsdr
```
Expected: builds quickly (no compilation, just an apt package) — final line `Successfully tagged` or the buildkit equivalent.

- [ ] **Step 3: Add the compose service**

In `docker-compose.yml`, after the `scanner-spectrum` service block (ends at the blank line before `ground-station:`), insert:

```yaml
  # Fourth, independent capture unit -- an RTL-SDR dongle doing the same
  # kind of wide-area energy detection as scanner-spectrum, on cheaper and
  # much more widely-owned hardware, scoped to the two bands (keyfob,
  # cellular_low) its R820T tuner can actually and fully cover. See
  # docs/superpowers/specs/2026-08-06-rtlsdr-spectrum-scanner-design.md.
  # Writes into the same shared detections.sqlite3's spectrum_hits table,
  # tagged with its own SOURCE_UNIT_ID. Starts unconditionally like the
  # other scanners -- if the RTL-SDR isn't attached, rtlsdr_source.py logs
  # a warning and retries every 5s forever.
  scanner-rtlsdr:
    build: ./services/scanner-rtlsdr
    image: ble-sar-df-scanner-rtlsdr:local
    restart: unless-stopped
    network_mode: host
    privileged: true
    volumes:
      - ./data:/data
    environment:
      DB_PATH: /data/detections.sqlite3
      GPSD_HOST: 127.0.0.1
      GPSD_PORT: "2947"
      SOURCE_UNIT_ID: ground-logger-rtlsdr-01
      RTLSDR_CALIBRATION_S: "15"
      RTLSDR_DWELL_S: "8"
      RTLSDR_HIT_MARGIN_DB: "10"
      LOG_LEVEL: INFO

```

- [ ] **Step 4: Add it to ground-station's `depends_on`**

Change:
```yaml
    depends_on:
      - scanner
      - scanner-spectrum
```
to:
```yaml
    depends_on:
      - scanner
      - scanner-spectrum
      - scanner-rtlsdr
```

- [ ] **Step 5: Validate and smoke-test**

```bash
docker compose config --quiet
```
Expected: exit 0, no output.

```bash
docker compose up --build scanner-rtlsdr
```
Expected: image builds, container starts, logs show `starting spectrum scanner: ...` (or whatever the startup banner reads) followed by either a real calibration pass (if the RTL-SDR is attached) or an `rtl_power not found`/device-unavailable retry warning every ~5s — either way, the container must stay up, not exit or crash-loop. Stop with Ctrl-C.

- [ ] **Step 6: Commit**

```bash
git add services/scanner-rtlsdr/Dockerfile docker-compose.yml
git commit -m "$(cat <<'EOF'
Wire scanner-rtlsdr into docker-compose.yml

Unlike scanner-ubertooth/scanner-spectrum, this uses Debian's packaged
rtl-sdr directly -- no from-source build needed, verified current
enough for rtl_power's CSV format and process-lifecycle behavior to
match what this service's code assumes. Starts unconditionally
alongside the other three scanners, retrying quietly if the RTL-SDR
isn't attached. ground-station now depends_on it too.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Validate against real RTL-SDR hardware

**Files:** none (verification only — may adjust timing constants in `rtlsdr_source.py` if real-world sweep timing doesn't match what Task 4 assumed)

This needs the physical RTL-SDR dongle, so it's one for you to run directly (or hand to a subagent with SSH/hardware access, if this session has it — check before assuming you don't).

- [ ] **Step 1: Confirm real sweep timing per band**

With the dongle attached, time how long a real calibration pass actually takes for each band at the Task 4 defaults (`RTLSDR_CALIBRATION_S=15`):
```bash
time rtl_power -f 698M:960M:1M -i 15 -1 -
time rtl_power -f 300M:450M:1M -i 15 -1 -
```
Compare against the `-i` value requested. If a band's sweep takes meaningfully longer than the requested interval (`rtl_power`'s own help text warns sweeps can exceed the requested interval when there are many hops), increase `DEFAULT_CALIBRATION_S`/`DEFAULT_DWELL_S` and the corresponding compose env var defaults so a legitimate sweep never trips the `_run_rtl_power` failure heuristic. Re-run the affected unit test(s) if you change the elapsed-time threshold logic itself (unlikely to need it — only the duration_s inputs should need tuning).

- [ ] **Step 2: Bring the full stack up with the RTL-SDR attached**

```bash
cd /home/seraph/Projects/ble-sar-df
docker compose up --build scanner-rtlsdr
```
Expected: logs show a calibration pass across both bands (`baseline[keyfob] = ...`, `baseline[cellular_low] = ...`), then continuous sweeping.

- [ ] **Step 3: Trigger a real signal and confirm a hit is logged**

While it's sweeping the `keyfob` band, press a car remote or garage door opener near the antenna (same technique Component F's Task 7 used). Confirm a hit lands in the shared table:
```bash
python3 -c "
import sqlite3
c = sqlite3.connect('data/detections.sqlite3')
print(c.execute(\"select band, freq_hz, power_dbm, baseline_dbm, source_unit_id, timestamp_utc from spectrum_hits where source_unit_id='ground-logger-rtlsdr-01' order by id desc limit 5\").fetchall())
"
```
Expected: at least one `keyfob` row with `source_unit_id='ground-logger-rtlsdr-01'` and a timestamp matching when you pressed the remote.

- [ ] **Step 4: Confirm it appears on the existing ground-station map with no code changes**

Open the ground-station UI, check "Spectrum hits", confirm the new hit renders (same yellow keyfob-colored dot Component F's hits already use — no new UI code should have been needed, per the design doc Section 6). If it doesn't render, that's a signal something in Task 1-6 deviated from the shared schema/contract — investigate before considering this task done, don't add UI code to compensate.

- [ ] **Step 5: If timing constants needed adjusting, commit that**

```bash
git add services/scanner-rtlsdr/app/rtlsdr_source.py docker-compose.yml
git commit -m "$(cat <<'EOF'
Tune scanner-rtlsdr sweep timing against real hardware

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
(Skip this step if the defaults already worked and nothing changed.)

---

### Task 8: Update README.md and docs/ARCHITECTURE.md

**Files:**
- Modify: `README.md`
- Modify: `docs/ARCHITECTURE.md`

- [ ] **Step 1: Update the Supported hardware table**

Add a row after the `scanner-spectrum` row:
```markdown
| RTL-SDR spectrum scanner | Any RTL2832U-based RTL-SDR dongle (R820T/R820T2 tuner) | Validated end-to-end (see [RTL-SDR notes](#rtl-sdr-notes)) |
```
Update the paragraph below the table (currently "The three scanners are independent and additive...") to say "four scanners".

- [ ] **Step 2: Update the Contents list**

Add `- [RTL-SDR notes](#rtl-sdr-notes)` after `- [Spectrum scanner notes](#spectrum-scanner-notes)`.

- [ ] **Step 3: Add a host prerequisite entry**

After the HackRF One prerequisite, add:
```markdown
6. **RTL-SDR dongle (optional)** — plug it in; confirm with `lsusb` (any
   Realtek RTL2832U-based device, e.g. `0bda:2838`). Like the others, no
   host-side driver setup needed for the Docker path: `scanner-rtlsdr` runs
   `privileged: true` for raw USB access, and unlike Ubertooth/HackRF, its
   `rtl-sdr` package comes straight from Debian (no from-source build).
```

- [ ] **Step 4: Update the Running section**

Change "all four services"/"the UD100/Ubertooth/HackRF" references to "all five services"/"the UD100/Ubertooth/HackRF/RTL-SDR".

- [ ] **Step 5: Add a Configuration table for scanner-rtlsdr**

After the `scanner-spectrum` table, add:
```markdown
**scanner-rtlsdr**

| Var | Default | Meaning |
|---|---|---|
| `DB_PATH` | `/data/detections.sqlite3` | Same SQLite file as the other scanners |
| `GPSD_HOST` / `GPSD_PORT` | `127.0.0.1` / `2947` | Same as the other scanners |
| `SOURCE_UNIT_ID` | `ground-logger-rtlsdr-01` | Kept distinct so this unit's hits are separately attributable |
| `RTLSDR_CALIBRATION_S` | `15` | Seconds to sample each band at startup when establishing that flight's baseline (tuned from real-hardware timing in Task 7) |
| `RTLSDR_DWELL_S` | `8` | Seconds to sweep each band per pass once calibration is done |
| `RTLSDR_HIT_MARGIN_DB` | `10` | How far above a reading's own frequency bin's calibrated baseline it must be to count as a hit |
```

- [ ] **Step 6: Add an "RTL-SDR notes" section**

After "## Spectrum scanner notes" and before "## Raspberry Pi deployment", add:
```markdown
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
```

- [ ] **Step 7: Update the privileged-mode and retry-behavior limitations**

Update the `privileged: true` bullet and the "all N scanners retry every 5s" bullet in Known limitations to cover four scanners instead of three, and add:
```markdown
- The RTL-SDR's real tunable range (~24 MHz-1766 MHz, common R820T/R820T2
  tuners) means `scanner-rtlsdr` cannot cover the same band list as
  `scanner-spectrum` — see [RTL-SDR notes](#rtl-sdr-notes). This is a
  hardware limit, not a configuration gap.
```

- [ ] **Step 8: Add Component G to ARCHITECTURE.md**

After Component F's section and before "## 3. Data Model", add:
```markdown
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
```

- [ ] **Step 9: Update the Hardware Sketch table and Suggested Build Order**

Add a row after Component F's in the Hardware Sketch table:
```markdown
| SDR, RTL-SDR variant of the spectrum scanner (G) | Any RTL2832U + R820T/R820T2 dongle | Built and validated — see `services/scanner-rtlsdr/`. Cheaper, more widely-owned alternative to Component F's HackRF, at the cost of tuner range (~24MHz-1766MHz) |
```
After item 6 ("Multi-band spectrum scanner (F)..."), add:
```markdown
7. RTL-SDR spectrum scanner (G) -- independent of (F), same detection
   logic on cheaper hardware. See
   `docs/superpowers/plans/2026-08-06-rtlsdr-spectrum-scanner.md`.
```

- [ ] **Step 10: Commit**

```bash
git add README.md docs/ARCHITECTURE.md
git commit -m "$(cat <<'EOF'
Document Component G (RTL-SDR spectrum scanner) in README/ARCHITECTURE

Adds the RTL-SDR to Supported hardware/Configuration/host-prereqs, a
new "RTL-SDR notes" section mirroring the existing Spectrum scanner
notes, and an ARCHITECTURE.md Component G writeup explaining the
tuner-range tradeoff against Component F.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```
