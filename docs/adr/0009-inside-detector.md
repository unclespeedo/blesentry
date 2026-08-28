<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# ADR-0009: Inside detector — sustained adjacent-to-Pi

- **Status:** Approved
- **Date:** 2026-08-28
- **Deciders:** Ryan Speed <rspeed@speedo.ca>

## Context

Adaptive detection (`docs/detection-plan.md`) adds an **inside** layer:
a BLE device is **sustained** in the adjacent-to-Pi band (RSSI ≥ −55),
judged from post-resolve band counts, not from *who* each device is.

In the target low-traffic regime the adjacent band is **normally
near-empty** — a design assumption for the target regime, not a
measured property of any site. A sustained device there is a
high-SNR signal. Single-window blips are common; **sustained**
presence is the anomaly.

Constraints this ADR must freeze (DC-5, DC-6, ADR-0006):

- **DC-6.** RSSI is noisy and per-install. −55 dBm is
  **adjacent-to-Pi** (~1–2 m, `docs/tuning.md`), not "in-room."
  Alerts report coarse proximity bands, never a metre figure. The
  alert means *"a BLE device is sustained adjacent-to-Pi,"* **not**
  *"a person is inside."* Advertising-only recall ceiling: an
  intruder who disables BLE emits nothing.
- **DC-5.** A BlueZ silent wedge drives counts to 0 — that is
  scan-health (P4-6), not "all clear." Inside output is suppressed
  on degraded health; an abrupt collapse is not treated as quiet.
- F3 already defines inclusive `band_counts` and default band edges.
  This ADR must not fork those formulas.
- Own-gear exclusion is post-resolve (`device_id`) per ADR-0006. F6
  builds `is_familiar`; I2 wires familiar + rotating own-address
  gear into the `excluded` set. I1 only pins the count contract.
- ADR-0006 owns the seam. This ADR does **not** add `inside` to the
  `[detection]` union, does **not** wire `run_cycle`, and does **not**
  enqueue. Those are I3.

I1's DoD is the frozen **N** / **M** sustain rule, threat-model
boundary, and FAR target so I2 / I3 / I4 are not guessing.

## Decision

### Features (every window)

From the post-resolve **`heard`** map (`device_id → best RSSI`), via
F3 `band_counts` with default `BandEdges`, after subtracting the
**excluded** `device_id` set (I2):

| Field | F3 name | Role |
|---|---|---|
| **Adjacent count** | `count_adjacent` | Primary sustain input (RSSI ≥ −55). |

**Source = `heard`.** Post-resolve counts de-duplicate RPA shards.
Approach stays pre-fusion (ADR-0007); inside and crowd use
post-resolve aggregates.

Executable helper: `blesentry.detection.inside.inside_count`.

### Sustain model

The adjacent band is near-zero in the target regime, so a **CUSUM**
baseline (crowd, ADR-0008) is unnecessary. v1 uses a **consecutive
window threshold**:

| Knob | Value | Role |
|---|---|---|
| **N** (`min_devices`) | **1** | At least one adjacent identity after exclusion. |
| **M** (`sustain_windows`) | **8** | Consecutive windows with `count ≥ N` (~2 min at 15 s). |

Update each window (after health check and exclusion):

- If `count < N` → streak **reset to 0** (episode end / blip).
- Else → `streak += 1`.
- Fire when `streak ≥ M` (I3 owns fire-once / coalesced roster).

Seven consecutive windows at `count = 1` → streak `7 < M` — not an
alert. The eighth crosses `M`. A single quiet window resets the
streak.

Executable helper: `blesentry.detection.inside.inside_sustain_step`.

### Scan health (DC-5)

When scan liveness is degraded (P4-6 signal — not yet a protocol
argument in ADR-0006), the inside backend returns no events and does
**not** treat the count collapse as a streak reset toward "quiet."
I3 wires the health input when P4-6 lands.

### Kind token (ADR-0006)

| Field | Token | When it becomes legal |
|---|---|---|
| `DetectionEvent.detector` | `inside` | I3 adds the `[detection] backend` union member |
| `DetectionEvent.kind` | `inside-adjacent` | I3's `observe` return |

Until I3, `backend = "inside"` remains a load-time `ConfigError`
(closed union). This ADR only *reserves* the tokens.

### What is not a gate

- **`is_familiar` / labels.** I2 subtracts before counting; not a
  separate inside gate.
- **Near / far counts.** Crowd features; inside uses adjacent only.
- **Presence PRESENT.** Complementary layer.

### Threat model & recall ceiling (DC-6)

The inside alert is a **smoke alarm, not a tripwire**:

1. **Advertising devices only** — no BLE advertisement, no signal.
2. **Not a person detector** — sparse advertising on screen-locked
   phones; sustained detection not guaranteed.
3. **Coarse bands only** — F3 `proximity_band` in alert text; no
   metres.
4. **Per-install edges** — −55 is adjacent-to-Pi, not in-room.

### FAR target (I4)

**≤ 1** false `inside-adjacent` event per **24 h** of labeled-benign
corpus time (quiet days + own-gear rotation episodes once F4 exists).
I4 validates `N` / `M` against this target on fixtures; changing them
after accept is a new ADR.

### Alert content (I3, not this PR)

One coalesced alert per episode listing contributor identities
(post-resolve, no raw addresses in CI logs — SECURITY.md). Coarse
adjacent count in text; never a metre figure (DC-6).

## Consequences

- I2 wires F6 `is_familiar` + own rotating-address gear into the
  `excluded` set before `inside_count`.
- I3 is a new `[detection]` union member calling `inside_count` +
  `inside_sustain_step`. Shipping this ADR does not change alert
  behaviour (`none` stays default).
- F3 `band_counts` stays canonical; edges and inclusive nesting live
  in `features.py`, not a second count path.
- CUSUM on adjacent count is explicitly deferred; the near-zero
  baseline favours a consecutive threshold. I4 reports if `M` misses
  the FAR target.

## Future Considerations

- **I2.** Own-gear exclusion wiring + fixture tests.
- **I3.** `inside` backend, `kind="inside-adjacent"`, roster alert,
  cycle enqueue, scan-health suppression.
- **I4.** Replay sustained dwell vs transient pass; FAR ≤ 1 / quiet
  day; metrics logged.
- **F6.** Familiar-set builder that feeds I2's exclusion set.
