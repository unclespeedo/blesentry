<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# Crowd baseline + CUSUM (C1)

Unusual site busyness from per-window band counts. Frozen in
ADR-0008. This document is the implementer-facing copy of those
numbers so C5 does not fork them.

Three future modules; only C4 is a Detector backend:

| Piece | Module (planned) | Job |
|---|---|---|
| **C1 helpers** | `blesentry.detection.crowd` | Frozen knobs + `crowd_counts`, `floored_mad`, `cusum_positive` |
| **C2 persistence** | `WindowBandCountRepository` (#132) | Band-count row per cycle inside the txn |
| **C3 baseline** | `blesentry.detection.crowd_baseline` (#133) | Seasonal + rolling EWMA, episode freeze — see `docs/crowd-baseline.md` |
| **C4 backend** | `blesentry.detection.crowd_detector.CrowdDetector` | `[detection] backend = "crowd"`; `kind="crowd-busy"` |

Default `[detection] backend` stays `"none"`. Enabling crowd is a
config edit (`backend = "crowd"`).

## Why

Presence alerts on *who* reached PRESENT. Crowd asks *what changed*:
the RF environment got unusually dense in the near band. Rotation
shards inflate per-identity noise; aggregate counts absorb them.

Anonymized baseline from the detection plan: near count mean ≈ 4,
above 12 in ~3% of windows. Single-window spikes are normal;
**sustained** elevation is the signal — hence CUSUM, not a fixed
threshold on one window.

## Source

Post-resolve **`heard`**. Identity = decimal `device_id`. Counts
use F3 `band_counts` on `{str(device_id): rssi}` with default
`BandEdges`. Pre-fusion `advertisements` are out of scope (approach
uses those; crowd de-duplicates RPA via fusion).

## Frozen knobs

Cadence reminder: default window is `scan.window + scan.pause` =
**15 s**. Rolling window counts are window ordinals, not wall-clock.

| Knob | Value | Notes |
|---|---|---|
| Primary feature | `count_near` | RSSI ≥ −70 (F3 `DEFAULT_BANDS.near`) |
| Context feature | `count_all` | Every heard identity this window |
| Scale model | Floored MAD | `max(MAD(residuals), 1.5)` — DC-3 |
| MAD floor | 1.5 counts | `CROWD_MAD_FLOOR` |
| CUSUM k | 0.5 | Allowance (sigma units) |
| CUSUM h | 5.0 | Fire when accumulator ≥ h |
| EWMA span N | 56 | ~8 weeks per hour-of-week bucket |
| EWMA α | `2 / (N + 1)` | `ewma_alpha(56)` |
| Residual window cap | 56 | `CROWD_RESIDUAL_WINDOW` (= EWMA span, DC-2) |
| Seasonal buckets | 168 | Hour-of-week (7 × 24) |
| Rolling fallback | 40320 windows | ~7 days at 15 s |
| Cold start | 168 h | Seasonal untrusted until then |
| `kind` | `crowd-busy` | C4 `DetectionEvent.kind` |
| `detector` id | `crowd` | `[detection] backend = "crowd"` |
| FAR (C5) | ≤ 1 false event / 24 h benign corpus | Quiet + rotation-cloud days |

## Features

```text
crowd_counts(heard) -> tuple[int, int]
```

Returns `(count_near, count_all)`. Implementation:
`blesentry.detection.crowd.crowd_counts`. Pure; no I/O.

## Standardized excess

Each window (C3 computes baseline and scale online):

```text
z = (count_near - baseline) / scale
scale = floored_mad(residuals in bucket window)
```

`floored_mad` is F3-adjacent but lives in `crowd.py` (C1 pins the
floor; C3 owns the residual window cap).

## CUSUM (one-sided upper)

```text
cusum_positive(accumulator, z, *, k, h) -> tuple[float, bool]
```

1. If `z <= -k` → reset accumulator to `0`, not fired.
2. Else `S = max(0, S + z - k)`.
3. Fired when `S >= h`.

Episode freeze (C3): EWMA baseline does not update while `S > 0`.

Single-window spike at `z = 4` → `S = 3.5` (below `h = 5`). Three
windows at `z = 2` → `S = 4.5`; fourth crosses `h`.

Fire-once / coalesced roster is **C4**, not the CUSUM helper.

## Baseline tiers (DC-4)

1. **Seasonal** — hour-of-week bucket EWMA when wall clock is trusted
   and cold start complete.
2. **Rolling** — last 40320 `count_near` samples otherwise.

Hold-and-backfill: queue seasonal writes until NTP confirms wall
clock; drain in order. In-episode freeze: no EWMA update while CUSUM
`S > 0`.

## Scan health (DC-5)

On degraded scan liveness (P4-6, future protocol field): return no
events; do not treat count collapse as quiet. C4 wires the input.

## What C1 is not

- **Not a Detector.** C4's backend is.
- **Not persistence.** C2 writes band-count rows.
- **Not the online baseline.** C3 implements EWMA + seasonal state.
- **Not `is_familiar`.** F6 / I2; roster context only at C4.
- **Not a distance.** Counts and bands only (DC-6).

## Detector backend (C4)

```text
CrowdDetector.prepare_window(*, observed_at, wall_clock_trusted=True) -> None
CrowdDetector.observe(window) -> tuple[DetectionEvent, ...]
format_crowd_alert(event) -> str
```

Implementation: `blesentry.detection.crowd_detector`. Holds
:class:`~blesentry.detection.crowd_baseline.CrowdBaseline` state, the
positive CUSUM accumulator, and a fire-once flag per episode.

`observe` is synchronous and I/O-free (ADR-0006). It reads
**post-resolve `heard`** only (`advertisements` is ignored). Each
window:

1. `crowd_counts(heard)` → `(count_near, count_all)`
2. `CrowdBaseline.begin_window` → `preview` → `cusum_positive` →
   `commit` (episode freeze when `S > 0`; see `docs/crowd-baseline.md`)
3. When CUSUM fires and this episode has not already alerted, return
   one `DetectionEvent`:

| Field | Value |
|---|---|
| `detector` | `crowd` |
| `kind` | `crowd-busy` |
| `window_index` | the window's index |
| `count` | `count_near` |
| `count_all` | every heard identity this window |
| `contributors` | sorted post-resolve `device_id`s in the near band |

No raw address, no metres (DC-6, SECURITY.md).

**Wall clock.** Seasonal baselines need UTC `observed_at`. Live
`run_cycle` calls `prepare_window(observed_at=cycle_at)` before
`observe`. Replay without injection synthesizes timestamps from
`index × 15 s` anchored at a fixed epoch.

**Fire-once per episode.** CUSUM may stay above `h` on later windows;
C4 emits on the first crossing only. When `z ≤ −k` resets `S` to
zero, the fire-once flag clears so a later busy spell can alert again.

**Alert text** (snapshot-tested), never a distance:

```text
Unusual site busyness: 7 near / 7 total (device 1, device 2, …).
```

`run_cycle` / `run_loop` enqueue inside the cycle transaction (DC-1),
mirroring inside (I3). Scan-health suppression (P4-6) is not wired
yet — the backend always treats the window as healthy.

### Replay (DoD)

Programmatic heard windows and synthetic fixtures under
`tests/fixtures/replay/`:

| Scenario | Expect |
|---|---|
| Sustained near-count elevation (≈ `z = 2` for four windows after warmup) | one `crowd-busy` event |
| Single-window spike (≈ `z = 4`) | silent |

Golden: `crowd-busy-golden.json`. Counts and event fields only — no
addresses in the report.

## Future work

- **C5 (#135).** FAR validation on fixtures; tune only via new ADR.
