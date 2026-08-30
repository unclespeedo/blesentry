<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# Familiar / resident baseline (F6)

Auto-learned allow-list of resolved device identities every detector
subtracts (I2 wires it into inside; crowd uses roster context at C4).
Frozen in this document; I2/C4 import the constants from
`blesentry.detection.familiar`.

## Why

Novelty-by-address is almost always true under RPA (DC-7). Detectors
need a **device-class** allow-list built from history: fingerprint
classes fused to one `device_id` (ADR-0005), including own rotating
gear once the resolver joins shards. F3's `duty` is a per-batch
proxy; F6 owns the K-day persistent set.

## Identity

Post-resolve **`device_id`** (decimal integer). Not BLE address, not
pre-fusion `advertisements`. A rotator that the resolver fused into one
device row counts as one class.

## Membership

| Rule | Condition |
|---|---|
| **Label** | Any non-null `devices.label` — operator-named or `(ignored)` |
| **Auto-learn** | Observed on ≥ **K** distinct UTC calendar days (`substr(observed_at,1,10)`) |

Auto-learned entries are capped at **M** devices (DC-2). When more
than M qualify, keep those with the highest distinct-day counts; tie-
break by lowest `device_id`. Labeled devices are always familiar even
when the auto-learn cap is full. The ranked auto-learn query excludes
labeled ids so the M slots are not wasted on entries already in the
set via the label rule.

## Frozen knobs

| Knob | Value | Constant |
|---|---|---|
| K (min distinct days) | **3** | `FAMILIAR_MIN_DAYS` |
| M (auto-learn cap) | **48** | `FAMILIAR_MAX_DEVICES` |
| Refresh cadence | Startup + once per UTC day after COMMIT | `FamiliarSetRefresher` |

K = 3 separates daily residents from a two-day visitor without the
seven-day crowd cold start. M ≈ a few dozen persistent fixtures (DC-2).

## Lifecycle (DC-1)

Built **outside** the per-cycle hot path:

1. **Startup** — before the first scan window (`blesentry run`).
2. **Periodic** — at most once per UTC calendar day, after the cycle
   transaction COMMIT (same posture as `WindowBandCountRepository`
   retention).

`run_cycle` does not read `observations` for familiarity. Callers hold
one `FamiliarSetRefresher` across cycles (like `TrajectoryTracker`).

## API

```text
FamiliarSet.is_familiar(device_id) -> bool

build_familiar_device_ids(devices, observations, ...) -> frozenset[int]
FamiliarSetRefresher.build() / refresh_if_due(now)
```

Pure membership test; no I/O inside `is_familiar`.

## What this is not

- **Not wired into detectors yet.** I2 subtracts before inside; C4
  may use roster context. F6 builds the set.
- **Not per-cycle observation reads in detectors.** DC-1.
- **Not the RPA cloud.** Trajectory tracker cap is separate (DC-2).
- **Not RSSI distribution learning.** DC-6 calibration is a follow-on;
  band edges stay in F3 `BandEdges` for now.
- **Not `duty`.** F3 proxy only; `is_familiar` is K-day history.

## References

- `docs/features.md` — F3 vectors; `duty` vs `is_familiar`
- `docs/detection-plan.md` — F6 issue map
- ADR-0005 — resolver fusion / `device_id`
- ADR-0009 — I2 exclusion consumes this set
