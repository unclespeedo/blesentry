<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# Approach trigger (A1)

Magnitude-based rising RSSI span on a **raw BLE address**. Frozen in
ADR-0007. This document is the implementer-facing copy of those
numbers so A2 / A3 / A5 do not fork them.

Not a Detector backend. `[detection] backend = "approach"` is still
illegal (ADR-0006 closed union). A3 adds that member and calls
`observe`. This module only answers "do these last-W heard samples
look like an approach?"

## Why

Presence alerts on *who* reached PRESENT. Approach asks *what
changed*: a large, mostly-monotonic climb out of the noise band,
ending close enough to matter. Novelty-by-address is ~always true
under RPA (DC-7), so unfamiliarity is not a gate.

The motivating (anonymized) walk-by climbed ≈ −99 → −72 dBm over
~2 min and never crossed −55. A peak floor at adjacent-to-Pi would
miss it.

## Source

Pre-fusion `advertisements`. Identity = address string. One sample
per window = that address's max RSSI in the window (F3
`max_rssi_by_identity`). Missed windows are omitted from the point
list so BlueZ duplicate-filtering does not flatten slope (DC-5).

`heard` / `device_id` / F6 `is_familiar` are not inputs.

## Frozen knobs

Cadence reminder: default window is `scan.window + scan.pause` =
**15 s**. Counts below are heard samples, not wall-clock.

| Knob | Value | Notes |
|---|---|---|
| W | 8 | Last 8 *heard* samples. ~2 min. Same integer as F3 `DEFAULT_SLOPE_WINDOWS`; A1 owns trigger W, F3 owns eval W. |
| Δ | 18 dB | `rssi_span(rssis) >= 18` |
| Peak / terminal floor | −75 dBm | `max(rssis) >= -75` **and** last sample `>= -75` |
| Far start | −85 dBm | `min(rssis) <= -85` on the **last W** (not visit-min) |
| Mostly-monotonic | `rssi_slope(points) > 0` and `(last - first) >= span * APPROACH_MONO_FRACTION` (0.5) | `rssi_slope` is F3's OLS helper; `None` → no trigger |
| `kind` | `approaching` | A3 fills `DetectionEvent` |
| `detector` id | `approach` | Reserved; union member is A3 |
| FAR (A5) | ≤ 1 false event / 24 h benign corpus | Quiet + rotation cloud + stationary own-gear |

−55 dBm stays **adjacent-to-Pi** (F3 `count_adjacent`, Inside). It
is not this peak floor.

## Predicate

```text
is_rising_approach(points) -> bool
```

`points` is `(window_index, rssi)*` oldest-first. Implementation:
`blesentry.detection.approach.is_rising_approach`.

1. If fewer than W points → `False` (not enough history).
2. Take the last W points (F3 rolling window).
3. `span = rssi_span(rssis)` (F3). Empty is not reachable at W ≥ 1.
4. `slope = rssi_slope(rolling)` (F3).
5. `True` iff all of:
   - `span >= 18`
   - `slope is not None and slope > 0`
   - `max(rssis) >= -75`
   - `rssis[-1] >= -75`
   - `min(rssis) <= -85`
   - `(rssis[-1] - rssis[0]) >= span * 0.5`

No I/O. No `DetectionEvent`. A2's deque contents are a valid
`points` argument. All inequalities use the last W samples only
(a visit-lifetime min is A2).

A3 fire-once / fade-reset is **not** in the predicate: once a rise
crosses the bar, later windows of the same visit still match until
the address fades (A2 eviction).

## Coverage bound (DC-7)

**Covered class:** walk-bys with ≥ W heard samples whose **last W**
satisfy the predicate (span ≥ 18 dB, trajectory `min` ≤ −85 dBm,
peak and terminal ≥ −75 dBm, slope > 0, net rise ≥ ½ span). A
−85 → −75 (10 dB) climb is out of class.

**Blind (accepted misses):**

- `< W` heard samples (median RPA shard ~1 window; sub-2-min)
- Drive-bys lasting seconds (A4)
- Last-W `min` > −85 (already-near, or far samples aged out of the tail)
- Address rotation that splits the span
- Last-W span < 18 dB or not mostly-monotonic

**Lead time:** fires only after the span has accumulated —
typically near peak closeness, ~W windows (~2 min) after first
hear. Late by construction; the bound includes that.

Empirical fraction of labeled approaches in the class is unmeasured
until F4. A5: recall = 1 **on that class**; also report overall
labeled-walk-by recall.

## What this is not

- **Not a Detector.** No `observe`, no `[detection]` backend.
- **Not wired into replay CLI or `run_cycle`.** A3.
- **Not a distance.** Alert text (A3) uses F3 coarse bands of the
  terminal RSSI, never metres (DC-6).
- **Not own-gear exclusion.** A phone walking toward the Pi matches.
- **Not F3's eval W.** Equal today (8); change them independently
  and pass A1's W into A2's deque / F3 helpers.
