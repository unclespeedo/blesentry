<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# ADR-0008: Crowd detector — robust baseline + CUSUM

- **Status:** Approved
- **Date:** 2026-08-28
- **Deciders:** Ryan Speed <rspeed@speedo.ca>

## Context

Adaptive detection (`docs/detection-plan.md`) adds a **crowd** layer:
the site got unusually busy, judged from per-window band counts, not
from *who* each device is. That is rotation-proof: RPA shards inflate
per-identity alerts but aggregate counts absorb duplication.

Evidence baseline (anonymized, detection plan): per-window **near
count** (RSSI ≥ −70) has mean ≈ 4, is smooth and unimodal, and
exceeds 12 in only ~3% of windows — a baselineable signal. Single-window
spikes are common; sustained elevation is the anomaly.

Constraints this ADR must freeze (DC-3, DC-4, DC-5, ADR-0006):

- **DC-3.** Count series are near-zero and skewed; bare MAD hits 0 at
  the floor. The scale must be floored or count-appropriate; thresholds
  are validated by empirical false-alarm rate, not assumed sigma.
- **DC-4.** Seasonal (hour-of-week) baselines need monotonic timestamps,
  hold-and-backfill until NTP confirms wall clock, and a rolling fallback
  when wall clock is unreliable. EWMA horizon ≫ the longest legitimate
  busy episode; baseline updates **freeze** during an active CUSUM
  episode so a sustained crowd is not absorbed into "normal."
- **DC-5.** A BlueZ silent wedge drives counts to 0 — that is scan-health
  (P4-6), not "all clear." Crowd output is suppressed on degraded health;
  an abrupt collapse is not treated as a return to baseline.
- F3 already defines inclusive `band_counts` and default band edges.
  This ADR must not fork those formulas.
- ADR-0006 owns the seam. This ADR does **not** add `crowd` to the
  `[detection]` union, does **not** wire `run_cycle`, and does **not**
  enqueue. Those are C4.

C1's DoD is the frozen scale/model choice, CUSUM knobs, clockless
baseline policy, and FAR target so C2 / C3 / C4 / C5 are not guessing.

## Decision

### Features (every window)

From the post-resolve **`heard`** map (`device_id → best RSSI`), via
F3 `band_counts` with default `BandEdges`:

| Field | F3 name | Role |
|---|---|---|
| **Near count** | `count_near` | Primary CUSUM input (RSSI ≥ −70). |
| **All count** | `count_all` | Diagnostic + C4 alert roster context. |

**Source = `heard`.** Post-resolve counts de-duplicate RPA shards that
would inflate pre-fusion `advertisements`. Approach stays pre-fusion
(ADR-0007); crowd and inside use post-resolve aggregates (DC-6 /
own-gear exclusion is F6 + I2, not a crowd gate).

Executable helper: `blesentry.detection.crowd.crowd_counts`.

### Scale model (DC-3)

**Floored MAD** on the per-bucket **residual** series
(`count_near − ewma_baseline`), not a bare Gaussian z on unfloored MAD
and not a Poisson model in v1 (pure-Python / DC-8; revisit in C5 if
validation fails).

| Knob | Value | Role |
|---|---|---|
| **MAD floor** | **1.5** counts | `scale = max(MAD(residuals), 1.5)`. Handles the zero-MAD degeneracy at low means (~4). |

Standardized excess each window:

\[
z = \frac{\text{count\_near} - \text{baseline}}{\text{scale}}
\]

`scale` is recomputed from the capped per-bucket residual window (C3
owns the online window cap; this ADR only pins the floor).

### Baseline adaptation (DC-4)

Two tiers, tried in order:

1. **Seasonal (hour-of-week).** **168** buckets (`7 × 24`). Each bucket
   holds an EWMA of `count_near` with effective span **N = 56** samples
   (~8 weeks of the same hour slot once cold start completes).
   `alpha = 2 / (N + 1)`.
2. **Rolling fallback.** Last **40320** windows (~7 days at the default
   15 s cadence) when wall clock is untrusted or the seasonal bucket is
   in cold start.

**Hold-and-backfill:** seasonal bucket writes are queued until wall
clock is NTP-confirmed; then backfilled in order. Until then, use the
rolling tier only.

**Cold start:** seasonal bucket is not authoritative until it has ≥
**168** wall-clock hours of confirmed samples **or** the install age ≥
168 h. Until then, rolling only.

**In-episode freeze:** while the positive CUSUM accumulator `> 0`, do
not update the EWMA baseline for the active bucket. Episode ends when
the accumulator resets (see CUSUM).

C2 persists raw band-count rows; C3 implements the online model.

### CUSUM (sustained shift)

One-sided **upper Page CUSUM** on `z` (busy only; quiet is normal):

| Knob | Value | Role |
|---|---|---|
| **k** | **0.5** | Allowance in sigma units — slack before accumulation. |
| **h** | **5.0** | Decision threshold — fire when accumulator ≥ h. |

Update each window (after health check):

- If `z ≤ −k` → accumulator **reset to 0** (episode end / return to
  normal).
- Else → `S = max(0, S + z − k)`.
- Fire when `S ≥ h` (C4 owns fire-once / coalesced roster).

A single-window spike with `z = 4` yields `S = 3.5 < h` — not an alert.
Three sustained windows at `z = 2` yield `S = 4.5`; a fourth crosses
`h` — sustained shift, not a blip.

Executable helper: `blesentry.detection.crowd.cusum_positive`.

### Scan health (DC-5)

When scan liveness is degraded (P4-6 signal — not yet a protocol
argument in ADR-0006), the crowd backend returns no events and does
**not** treat the count collapse as a baseline update or CUSUM reset
toward "quiet." C4 wires the health input when P4-6 lands.

### Kind token (ADR-0006)

| Field | Token | When it becomes legal |
|---|---|---|
| `DetectionEvent.detector` | `crowd` | C4 adds the `[detection] backend` union member |
| `DetectionEvent.kind` | `crowd-busy` | C4's `observe` return |

Until C4, `backend = "crowd"` remains a load-time `ConfigError`
(closed union). This ADR only *reserves* the tokens.

### What is not a gate

- **`is_familiar` / labels.** Crowd is aggregate; familiar subtraction
  is inside (I2) and roster context (C4), not a count gate.
- **Adjacent-to-Pi (−55).** Inside feature; near count (−70) is the
  crowd primary.
- **Presence PRESENT.** Complementary layer.

### FAR target (C5)

**≤ 1** false `crowd-busy` event per **24 h** of labeled-benign
corpus time (quiet days + rotation-cloud episodes once F4 exists).
C5 validates `h` / `k` / MAD floor against this target on fixtures;
changing them after accept is a new ADR.

### Alert content (C4, not this PR)

One coalesced alert per episode listing contributor identities
(post-resolve, no raw addresses in CI logs — SECURITY.md). Coarse
near/all counts in text; never a metre figure (DC-6).

## Consequences

- C2 writes one band-count row per cycle inside the existing
  transaction (DC-1).
- C3 implements seasonal + rolling EWMA, floored-MAD scale, episode
  freeze, and hold-and-backfill.
- C4 is a new `[detection]` union member calling C3 + `cusum_positive`.
  Shipping this ADR does not change alert behaviour (`none` stays
  default).
- F3 `band_counts` stays canonical; edges and inclusive nesting live
  in `features.py`, not a second count path.
- Poisson / negative-binomial scale is explicitly deferred; C5 reports
  if floored MAD misses the FAR target.
- F4 is not required to *write* this spec; it is required to *measure*
  FAR (C5).

## Future Considerations

- **C2.** Migration + per-cycle band-count persistence.
- **C3.** Online baseline model + unit tests at the zero floor.
- **C4.** `crowd` backend, `kind="crowd-busy"`, roster alert, cycle
  enqueue, scan-health suppression.
- **C5.** Replay busy vs quiet; FAR ≤ 1 / quiet day; metrics logged.
- **L2.** Labeled-benign episodes excluded from baselines.
