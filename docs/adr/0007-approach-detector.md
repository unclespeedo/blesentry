<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# ADR-0007: Approach detector — magnitude trigger

- **Status:** Proposed
- **Date:** 2026-08-27
- **Deciders:** <human sign-off required — agents may draft, never accept>

## Context

The shipped alert primitive is per-identity PRESENT (P2-1). Adaptive
detection (`docs/detection-plan.md`) adds an **approach** layer:
something is coming closer, judged from a per-address RSSI
trajectory, not from *who* the address is.

Constraints this ADR must freeze (DC-6, DC-7, ADR-0006):

- Novelty-by-address is almost always true under RPA. Unfamiliarity
  is **not** a gate.
- Approach is **pre-fusion**: raw BLE addresses, the window's
  `advertisements` stream. `heard` / `device_id` / F6 `is_familiar`
  are out of this trigger.
- Median rotated-row lifetime is ~0.3 min (~1 window at the 15 s
  cadence), so a W-window slope is structurally impossible for the
  median address. The spec must state which approaches it can see
  and accept blindness to the rest.
- RSSI is noisy and per-install. Band edges are features, not
  metres. −55 dBm is **adjacent-to-Pi** (~1–2 m, `docs/tuning.md`),
  not "in-room". The motivating walk-by climbed ≈ −99 → −72 dBm in
  ~2 min and **never** crossed −55; a peak floor at −55 would miss
  the case this detector exists to catch.
- F3 already defines OLS `rssi_slope` / `rssi_span` over the last
  W *heard* samples, omitting misses (DC-5). A2 reuses those
  formulas. This ADR must not fork them.
- ADR-0006 owns the seam. This ADR does **not** add `approach` to
  the `[detection]` union, does **not** wire `run_cycle`, and does
  **not** enqueue. Those are A3.

A1's DoD is the frozen numbers plus the coverage bound so A2 / A3 /
A5 are not guessing.

## Decision

### Trigger (all of these, same window)

Over the last **W heard samples** of one raw address (F3 rolling
window: misses omitted, one sample per window index — the
per-window max RSSI):

| Knob | Value | Role |
|---|---|---|
| **W** | **8** heard samples | ~2 min at the default 15 s scan cadence. Same integer as F3's `DEFAULT_SLOPE_WINDOWS`; A1 owns the *trigger* W. Changing one does not silently change the other. |
| **Δ** | **18 dB** | `rssi_span` of those samples ≥ 18. Mid-range of the plan's 15–20 dB band; larger than typical ±several dB multipath (DC-6). |
| **Peak floor** | **−75 dBm** | `max(rssi)` **and** the **terminal** (last) sample ≥ −75. Inclusive. The motivating walk-by peaked ≈ −72; −75 is a 3 dB noise margin. This is **not** the −55 adjacent-to-Pi edge. |
| **Far start** | **−85 dBm** | `min(rssi)` ≤ −85. Inclusive. The climb must have been heard in the noise / beyond-far band, so a stationary near device with an 18 dB fade does not qualify. |
| **Mostly-monotonic** | OLS `rssi_slope` **> 0** **and** `(last − first) ≥ ½ · span` | Rejects a mid-window spike whose net start-to-end rise is a minority of the span. `rssi_slope` is F3's helper (`None` → not a trigger). |

Fewer than W heard samples → not a trigger (not an error). Extra
samples: use the last W, same as F3.

**Last-W only.** Every gate in the table (Δ, peak, terminal,
far-start, slope, net rise) is evaluated on that truncated tail,
not the visit lifetime. A slow climb whose ≤ −85 dBm samples have
aged out of the last W is an **accepted miss**. Tracking a
visit-min outside the deque is A2, not this predicate.

The executable form is
`blesentry.detection.approach.is_rising_approach`. A2 feeds it a
bounded deque; A3 does not reimplement the inequalities.

### Kind token (ADR-0006)

| Field | Token | When it becomes legal |
|---|---|---|
| `DetectionEvent.detector` | `approach` | A3 adds the `[detection] backend` union member |
| `DetectionEvent.kind` | `approaching` | A3's `observe` return |

Until A3, `backend = "approach"` remains a load-time `ConfigError`
(closed union, ADR-0002 / ADR-0006). This ADR only *reserves* the
tokens.

A4's vehicle class is a **different** `kind`, not this one.

### What is not a gate

- **Novelty / `is_familiar` / labels.** Weak context for A3 alert
  *text* at most. Not in the predicate. Own gear walking toward the
  Pi **will** match (I2 is the inside detector's exclusion).
- **Adjacent-to-Pi (−55).** Feature band for Inside / F3, not the
  approach peak floor.
- **Presence PRESENT.** Complementary; not a precondition.

### Coverage bound (DC-7)

This detector covers **walk-bys whose raw address is heard in ≥ W
windows and whose last W heard samples satisfy the trigger**:
`rssi_span` ≥ 18 dB, `min(rssi)` ≤ −85 dBm, peak **and** terminal
≥ −75 dBm, OLS `rssi_slope` > 0, and `(last − first) ≥ ½ · span`.
A −85 → −75 (10 dB) climb is **out of class**, not a recall miss.

It is structurally blind to:

1. Addresses with fewer than W heard samples (median RPA shard;
   BlueZ duplicate-filtering drops; sub-2-min appearances).
2. Drive-bys lasting seconds (A4 / P4-2).
3. Last-W tails whose `min` is > −85 (already-near, **or** a slow
   climb whose far-band samples rolled out of the tail).
4. Mid-approach address rotation that splits the span across two
   identities.
5. Last-W tails that fail Δ or mostly-monotonic (span < 18 dB or
   a mid-window spike).

Lead time is **late by construction**: the alert can fire only
after the span has accumulated, typically near peak closeness,
on the order of W windows (~2 min) after first hear. The coverage
bound includes that delay.

From the anonymized identity-lifetime baseline in the detection
plan, ~¼ of identities live under 1 min (< 4 windows) and cannot
meet W = 8 even with no misses. That is an **upper-bound miss
from persistence**, not a measured approach-recall. The empirical
fraction of *labeled* approaches inside the coverage class is
**unmeasured until F4**. A5 reports it.

**A5 recall contract:** recall = 1 on labeled approach positives
**in this coverage class** (the predicate would return True).
Overall labeled-walk-by recall may be lower because of (1)–(5);
A5 reports both.

### FAR target (A5)

**≤ 1** false `approaching` event per 24 h of labeled-benign
corpus time (quiet + rotation-cloud + own-gear-stationary
episodes once F4 exists). Stationary rotation shards stay in a
narrow path-loss band and fail Δ / far-start / slope.

### Alert content (A3, not this PR)

Coarse proximity of the **terminal** RSSI (F3 bands: adjacent /
near / far / beyond-far). Never a metre figure (DC-6). Current
RSSI + that the span was rising. No raw address in CI logs
(SECURITY.md).

Fire-once per address visit is A3 (predicate stays true on later
windows of the same rise; A2 evicts on fade). The predicate is
stateless.

## Consequences

- A2's deque `maxlen` is W = 8. Hard address cap + evict-on-fade
  remain A2 (DC-2).
- A3 is a new `[detection]` union member that calls
  `is_rising_approach` inside `observe`. Shipping this ADR does
  not change alert behaviour (`none` stays default).
- F3 formulas stay canonical. Δ / floors live in
  `blesentry.detection.approach`, not a second slope.
- Per-install RSSI (DC-6) may eventually retune Δ / floors (F6
  calibration). That is a new ADR, not a silent constant edit.
- F4 is not required to *write* this spec; it is required to
  *measure* the coverage fraction and FAR (A5).

## Future Considerations

- **A2.** Bounded per-address deque of `(index, rssi)`; call the
  predicate; do not fork helpers.
- **A3.** `approach` backend, `kind="approaching"`, cycle
  transaction enqueue, fire-once, snapshot-tested alert text.
- **A4.** Walk vs vehicle; separate `kind`.
- **A5.** Replay vs F4; recall = 1 on the coverage class; FAR ≤ 1
  per quiet day.
- **Additive `DetectionEvent` fields** (peak RSSI, terminal band)
  when A3 needs them; keep `extra="forbid"` (ADR-0006).
