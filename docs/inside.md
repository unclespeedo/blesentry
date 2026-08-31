<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# Inside sustained adjacent-to-Pi (I1)

A BLE device held in the adjacent-to-Pi band. Frozen in ADR-0009.
This document is the implementer-facing copy of those numbers so I4
does not fork them.

Three future modules; only I3 is a Detector backend:

| Piece | Module | Job |
|---|---|---|
| **I1 helpers** | `blesentry.detection.inside` | Frozen knobs + `inside_count`, `inside_sustain_step` |
| **I2 exclusion** | `blesentry.detection.inside` | `build_inside_excluded`, `build_own_rotating_gear_device_ids` |
| **I3 backend** | TBD (#138) | `[detection] backend = "inside"`; `kind="inside-adjacent"` |

Default `[detection] backend` stays `"none"`. Enabling inside is a
future config edit.

## Why

Presence alerts on *who* reached PRESENT. Inside asks *what changed*:
something is **sustained** in the adjacent-to-Pi band (RSSI ≥ −55).
In the target low-traffic regime that band is normally near-empty, so
a sustained device there is a high-SNR signal (detection plan design
assumption — not a measured property of any site).

The alert means *"a BLE device is sustained adjacent-to-Pi,"* **not**
*"a person is inside."*

## Threat model & recall ceiling

State the boundary honestly (DC-6):

- **Advertising only.** Detects BLE devices that **advertise**. An
  intruder who disables Bluetooth, uses airplane mode, or leaves a
  phone in a vehicle emits nothing — a hard recall ceiling, trivially
  defeated by an aware adversary.
- **Not a person detector.** An idle screen-locked phone advertises
  sparsely; sustained detection is not guaranteed.
- **Not a distance.** Alerts report coarse proximity bands (F3
  `proximity_band`), never a metre figure (DC-6).
- **Band edges are per-install.** −55 is adjacent-to-Pi (~1–2 m per
  `docs/tuning.md`), not "in-room." Someone 4–5 m away sits at
  −70…−75 and is not adjacent.
- **Complements presence.** P2-1 still handles transient passers-by;
  inside is an additional detector into the same outbox.

## Source

Post-resolve **`heard`**. Identity = decimal `device_id`. Counts use
F3 `band_counts` on `{str(device_id): rssi}` with default `BandEdges`.
Pre-fusion `advertisements` are out of scope (approach uses those).

Own-gear / familiar subtraction happens **before** counting (I2 wires
F6 `is_familiar` plus rotating own-address gear). I1 only defines the
`excluded` set contract on `inside_count`.

## Exclusion (I2)

```text
build_inside_excluded(heard, *, familiar, own_rotating_gear=frozenset())
    -> frozenset[int]

build_own_rotating_gear_device_ids(devices, observations) -> frozenset[int]
```

`build_inside_excluded` returns every `device_id` in `heard` that is
F6-familiar **or** in the own-rotating-gear set. I3 passes the result
as `excluded=` to `inside_count`.

`build_own_rotating_gear_device_ids` runs at startup / daily refresh
(DC-1, same posture as `FamiliarSetRefresher`). It returns unlabeled
device ids whose observations are **not known-stable**
(`public` / `random_static` — same cut as resolver `_STABLE_TYPES`)
on at least one UTC calendar day when labeled operator gear was also
observed — the under-join case where the resolver keeps a phone
rotation as a separate `device_id`. Null provenance (CoreBluetooth /
legacy) counts as non-stable so those sources are not silently
skipped. Labeled and F6-familiar ids are already excluded via
`familiar`; this query catches shards that are not yet familiar.

## Frozen knobs

Cadence reminder: default window is `scan.window + scan.pause` =
**15 s**. Sustain counts are window ordinals, not wall-clock.

| Knob | Value | Notes |
|---|---|---|
| Primary feature | `count_adjacent` | RSSI ≥ −55 (F3 `DEFAULT_BANDS.adjacent`) |
| **N** (`min_devices`) | **1** | At least one adjacent identity after exclusion |
| **M** (`sustain_windows`) | **8** | Consecutive windows at ≥ N (~2 min at 15 s) |
| Sustain model | Consecutive threshold | Not CUSUM — baseline is near-zero |
| `kind` | `inside-adjacent` | I3 `DetectionEvent.kind` |
| `detector` id | `inside` | `[detection] backend = "inside"` |
| FAR (I4) | ≤ 1 false event / 24 h benign corpus | Quiet + own-gear rotation days |

## Features

```text
inside_count(heard, *, excluded=frozenset()) -> int
```

Returns `count_adjacent` after subtracting excluded `device_id`s.
Implementation: `blesentry.detection.inside.inside_count`. Pure; no
I/O.

## Sustained trigger (consecutive windows)

```text
inside_sustain_step(streak, count, *, min_devices, sustain_windows)
    -> tuple[int, bool]
```

1. If `count < min_devices` → reset streak to `0`, not fired.
2. Else `streak += 1`.
3. Fired when `streak >= sustain_windows`.

Seven windows at `count = 1` → streak `7`, not fired. The eighth
crosses `M = 8`. A single-window blip resets the streak.

Fire-once / coalesced roster is **I3**, not the sustain helper.

## Scan health (DC-5)

On degraded scan liveness (P4-6, future protocol field): return no
events; do not treat a count collapse as "all clear." I3 wires the
input.

## What I1 is not

- **Not a Detector.** I3's backend is.
- **Not own-gear exclusion.** I2 subtracts F6 + rotating own gear.
- **Not `is_familiar` implementation.** F6 builds the set; I2 applies it.
- **Not a distance.** Bands only (DC-6).
- **Not crowd / approach.** Post-resolve aggregate vs pre-fusion
  trajectory.

## Future work

- **I3 (#138).** Backend, alert-with-roster, replay sustained dwell;
  calls `build_inside_excluded` before `inside_count`.
- **I4 (#139).** FAR validation on fixtures; tune only via new ADR.
