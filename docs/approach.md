<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# Approach trigger (A1), tracker (A2), detector (A3)

Magnitude-based rising RSSI span on a **raw BLE address**. Frozen in
ADR-0007. This document is the implementer-facing copy of those
numbers so A5 does not fork them.

Three modules; only A3 is a Detector backend:

| Piece | Module | Job |
|---|---|---|
| **A1 predicate** | `blesentry.detection.approach.is_rising_approach` | Do these last-W heard samples look like an approach? |
| **A2 tracker** | `blesentry.detection.trajectory.TrajectoryTracker` | Bounded per-identity deques + fade/cap; feeds A1 |
| **A3 backend** | `blesentry.detection.approach_detector.ApproachDetector` | `[detection] backend = "approach"`; `kind="approaching"` |

Default `[detection] backend` stays `"none"`. Enabling approach is a
config edit. `run_cycle` calls `observe` inside the cycle
transaction and enqueues via the outbox (DC-1). Replay stays
read-only (DC-9).

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
| `kind` | `approaching` | A3 `DetectionEvent.kind` |
| `detector` id | `approach` | `[detection] backend = "approach"` |
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
(a visit-lifetime min is tracked by A2 and is **not** a gate).

Fire-once / fade-reset is **not** in the predicate: once a rise
crosses the bar, later windows of the same visit still match until
the address fades (A2 eviction). A3 owns fire-once.

## Online tracker (A2)

```text
TrajectoryTracker.observe(window, *, source="advertisements")
    -> tuple[AddressTrajectory, ...]
```

One call per scan window. Default `source` is pre-fusion
`advertisements` (ADR-0007). `heard` is supported so tests can drive
the same object F3 uses; A3 feeds advertisements. Identity
strings are opaque — do not dump them in CI logs (SECURITY.md).
`AddressTrajectory.identity` is omitted from the default Pydantic
`repr` so a stray `repr(row)` / pytest dump cannot leak a
production address. Tests that need the field read `.identity`.

Implementation: `blesentry.detection.trajectory`. Pure and
synchronous. No SQL, no outbox, no `DetectionEvent`.

### Per-identity state (DC-2)

Each live track holds:

| Field | Bound | Notes |
|---|---|---|
| `samples` | `deque(maxlen=W)` | `(window_index, rssi)` oldest-first. W = A1 `APPROACH_WINDOWS` (8), **not** F3's eval default (equal today; pass A1's W in). |
| `visit_min` | one int | Min RSSI since the track was created. Survives deque truncation. **Not** an A1 gate. |
| `dwell` | one int | F3 consecutive-index streak: `last_heard == index - 1` → +1, else reset to 1. |
| `windows_seen` / `first_seen_index` | counters | Not a second RSSI buffer (`docs/features.md`). |

`span` / `slope` are computed on the deque via F3 `rssi_span` /
`rssi_slope`. `rising` is `is_rising_approach(samples)`. Do not
copy Δ / peak / far-start / W into this module.

`observe` returns one `AddressTrajectory` per identity **heard this
window and admitted**. Quiet windows return `()`. Missed windows are
omitted from `samples` (DC-5), same as F3.

Window `index` must be **strictly increasing** across calls
(fail-loud). Out-of-order is a caller bug, not a fade.

### Memory cap (stated, tested)

Cadence reminder: default cycle is 15 s. These are window-index
counts, not wall-clock.

| Knob | Value | Role |
|---|---|---|
| `TRACKER_MAX_ADDRESSES` | **256** | Hard cap on live tracks. ~10× a busy window's unique count; a few dozen persist >30 min (`docs/detection-plan.md`). |
| `TRACKER_FADE_AFTER_WINDOWS` | **12** | Evict when `index - last_heard_index >= 12` and the identity was not heard this window. Same default as presence prune (`4 * disappear_windows`). |
| Peak RSSI samples | **2048** | `256 × 8`. Address cap is asserted under RPA churn; sample cap is asserted with 256 full deques. |

Eviction order, each `observe`:

1. **Fade.** Unheard tracks whose index-delta ≥ 12 are dropped.
   "Unheard" means absent from this window's identity set, not
   "too weak to keep."
2. **Update heard incumbents.** Every already-tracked identity that
   advertised this window is pushed, even if it is quieter than
   newcomers. A mid-approach track is not a miss just because a
   crowd appeared.
3. **LRU admit.** New identities that would exceed 256: evict
   unheard tracks, oldest `last_heard_index` first, until there is
   room. Remaining slots go to the **strongest** newcomers
   (`max_rssi`). If every remaining track was heard this window,
   `room == 0` and there are no LRU victims: newcomers (including
   the weakest) are not admitted (hard cap, not a fade).

Constructor kwargs (`max_addresses`, `fade_after_windows`) exist so
unit tests can shrink the cap. Production callers use the module
constants. Deque maxlen is A1 ``APPROACH_WINDOWS`` — not a
constructor knob (changing it would fork W). Integers only
(``bool`` / ``float`` → ``TypeError``; ``< 1`` → ``ValueError``).

### Rise / fall

A monotonic W-sample climb that satisfies A1 (the motivating
−99 → −72 walk-by) reports `rising=True` on the last window. The
same samples reversed (a fade) report `rising=False`. Later
windows of the same visit still report `rising=True` while the
last-W deque satisfies A1 — fire-once is A3, not the tracker.

Deque truncation drops aged samples. `span` / `slope` / A1 use
the remaining last-W only; a loud prefix that has aged out must
not inflate `span`. `visit_min` still keeps the visit floor.

### What the tracker is not

- **Not a Detector.** No `DetectionEvent`; A3 wraps it.
- **Not `is_familiar`.** F6.
- **Not a visit-min gate.** Last-W far-start stays last-W (A1).
- **Not persistence.** Tracks die on process restart; F3's offline
  batch remains the eval path.

## Detector backend (A3)

```text
ApproachDetector.observe(window) -> tuple[DetectionEvent, ...]
format_approach_alert(event) -> str
```

Implementation: `blesentry.detection.approach_detector`. Holds one
`TrajectoryTracker` (A2) and a fire-once set.

`observe` is synchronous and I/O-free (ADR-0006). It feeds
**pre-fusion `advertisements`** into the tracker (`heard` is
ignored — snapshot replay cannot drive this backend). For each
admitted identity whose `rising` is true and that has not already
fired this visit, it returns one `DetectionEvent`:

| Field | Value |
|---|---|
| `detector` | `approach` |
| `kind` | `approaching` |
| `window_index` | the window's index |
| `rssi` | terminal (this-window max) RSSI, dBm |
| `band` | exclusive F3 label of that RSSI (`proximity_band`) |
| `rising` | `True` |

No raw address, no metres (DC-6, SECURITY.md). Additive fields on
`DetectionEvent` default to `None` so `mock` events stay three
tokens.

**Fire-once per visit.** The predicate stays true on later windows
of the same climb; A3 emits on the first match only. Fade-eviction
(A2, 12 missed indexes) clears the identity so a later visit can
fire again.

**Alert text** (snapshot-tested), never a distance:

```text
Approaching BLE device (far, RSSI -72 dBm, rising).
```

`run_cycle` / `run_loop` take a `Detector` and the scan-connection
`OutboxRepository`. Default daemon config still uses `NullDetector`
(`none`) — shipping A3 does not change alert behaviour until
`backend = "approach"`. When that backend is selected, each
returned event is formatted and enqueued **inside the existing
cycle transaction**, mirroring `alerter.handle` (DC-1). Replay does
not enqueue.

The same fail-loud contract as presence: `observe` mutates the
tracker before COMMIT; a rolled-back cycle re-raises out of
`run_loop` and the process restarts with a fresh detector.

### Replay (DoD)

Synthetic labeled walk-by: `tests/fixtures/replay/walkby.json`
(the motivating −99 → −72 climb, one address, W samples at the
15 s cadence). `blesentry replay --fixture … --backend approach`
emits one `approaching` event at window index 7 (the peak). Golden:
`tests/fixtures/replay/walkby-approach-golden.json`. Counts and
event fields only — no addresses in the report.

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

## What the predicate is not

- **Not a Detector.** A3's `ApproachDetector` is.
- **Not a distance.** Alert text uses F3 exclusive bands of the
  terminal RSSI, never metres (DC-6).
- **Not own-gear exclusion.** A phone walking toward the Pi matches.
- **Not F3's eval W.** Equal today (8); change them independently
  and pass A1's W into A2's deque / F3 helpers.
