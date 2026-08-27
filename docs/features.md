<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# Detection feature vectors (F3)

Canonical per-window aggregates and per-identity trajectories the
eval harness (F5) and later detectors consume. A2's online tracker
reuses these **formulas**; this document is the definition, not a
second copy in the approach spec.

Offline batch only. The extractor reads `DetectionWindow` sequences
(the same type F1 replay and the live cycle share — ADR-0006) and
returns frozen feature vectors. It does **not** call `observe`,
enqueue to the outbox, or run inside `run_cycle`.

## Why

Approach needs a rolling RSSI slope / span / dwell. Crowd needs
band counts. Inside needs an adjacent-to-Pi count. Without one
definition, each detector (and the eval report) will drift. F3
freezes the vectors; detectors decide *thresholds* later (A1 / C1
/ I1).

## Source stream

A window may carry pre-fusion `advertisements`, post-resolve
`heard`, or both. The extractor takes **one** source per run:

| `source` | Identity | `max_rssi` |
|---|---|---|
| `advertisements` (default) | BLE address string | max RSSI among ads for that address in the window |
| `heard` | decimal `device_id` | the window's per-device best RSSI |

F1 replay fills exactly one stream, so the default matches fixture
replay. A live cycle that has both still picks one source; mixing
address space with `device_id` space in one vector is undefined.

Identities are opaque strings in the vector. Do not dump feature
vectors of a real capture into CI logs (SECURITY.md) — the same
rule as replay reports.

## Per-window aggregates

Inclusive nested counts of identities whose `max_rssi` meets each
lower bound. Bands are **features**, never a wall (the observation
gate stays wide; DC-6). Defaults match `docs/detection-plan.md`:

| Field | Default bound | Meaning |
|---|---|---|
| `count_adjacent` | RSSI ≥ **−55** | adjacent-to-Pi (~1–2 m per `docs/tuning.md`) |
| `count_near` | RSSI ≥ **−70** | near |
| `count_far` | RSSI ≥ **−80** | far / observation-band |
| `count_all` | any RSSI | every identity heard this window |

So `count_adjacent ≤ count_near ≤ count_far ≤ count_all`. An
identity at −65 counts as near, far, and all — not adjacent.

Edges are a constructor argument (`BandEdges`). They must satisfy
`adjacent > near > far` (higher dBm = closer). Per-install
calibration (DC-6, F6) passes different edges; it does not fork
the formulas.

### Churn

Relative to the **previous** window's identity set (empty before
the first window):

| Field | Definition |
|---|---|
| `appeared` | \|current − previous\| |
| `disappeared` | \|previous − current\| |
| `churn` | `appeared + disappeared` |

An empty window is still emitted (F1 fills gaps). That is how
dwell resets and `disappeared` sees a fade. A quiet site is zeros,
not an error.

## Per-identity trajectories

One row per identity **heard in this window**, sorted by identity
string. A fade is churn, not a zero-RSSI row.

History for slope / span is the last **W** *heard* samples up to
and including the current window. Missed windows (BlueZ
duplicate-filtering, F1 gaps) are omitted from the point list so
slope is not flattened by silence (DC-5 / DC-7). Default
`W = 8` (`DEFAULT_SLOPE_WINDOWS`) — two minutes at the 15 s scan
cadence. A1 owns the approach trigger's W; it may pass a
different value. F3's default is the eval / A2-reuse number.

| Field | Definition |
|---|---|
| `max_rssi` | this window's per-identity max (dBm) |
| `slope` | OLS slope of `(window_index, rssi)` over the last W heard samples; `None` if fewer than 2 points or the x-variance is 0. Units: **dBm per window index**. |
| `span` | `max(rssi) − min(rssi)` over those same samples; `0` with one point; `None` is not used (a heard identity always has ≥1 sample) |
| `dwell` | consecutive windows ending here in which the identity was heard. A miss resets to 1 on return. |
| `first_seen_index` | first window index (in this batch) where the identity was heard |
| `age_windows` | `index − first_seen_index + 1` (elapsed windows, including misses) |
| `windows_seen` | count of heard windows in that span |
| `duty` | `windows_seen / age_windows` in `(0, 1]` — F3's familiarity **proxy**. F6 owns `is_familiar` (K-day set). |

### OLS slope

For points \((x_k, y_k)\), \(n \ge 2\):

\[
\bar{x} = \mathrm{mean}(x),\quad
\bar{y} = \mathrm{mean}(y),\quad
\mathrm{slope} =
\frac{\sum (x_k-\bar{x})(y_k-\bar{y})}{\sum (x_k-\bar{x})^2}
\]

Denominator 0 → `None` (should not happen with distinct window
indexes). A monotonic −90 → −80 → −70 over indexes 0,1,2 is
exactly **+10.0** dBm/window — the unit test pins that.

A2 keeps a bounded deque of size W (DC-2). Feeding that deque to
`rssi_slope` / `rssi_span` is the reuse contract. Dwell is an O(1)
counter (last heard index + streak), not a rescan of history. F3's
offline batch may retain unbounded history so `windows_seen` /
`first_seen_index` stay exact; those are counters on the online
side, not a second buffer.

`W < 2` is rejected (`ValueError`). A non-`int` W (`bool`,
`float`, `inf`, `nan`) is `TypeError`. A rolling window that
cannot produce a slope is a programmer error, not a quiet site.

## API

```text
extract_features(windows, *, source="advertisements",
                 slope_windows=8, bands=DEFAULT_BANDS)
    -> tuple[WindowFeatures, ...]
```

Pure and synchronous. One output row per input window, including
empties. Same windows + same kwargs → equal vectors. Windows must
be in strictly increasing unique `index` order (F1 replay and the
live cycle both are); out-of-order input is undefined.

Public formula helpers (A2 imports these, does not reimplement):

- `max_rssi_by_identity(window, source)`
- `band_counts(max_rssi, bands)`
- `rssi_slope(points)` — `points` is `(index, rssi)*`
- `rssi_span(rssis)`

## What this is not

- **Not a detector.** No `DetectionEvent`, no `kind` tokens.
- **Not wired into replay CLI.** `blesentry replay` still reports
  window counts + events. F5 consumes these vectors.
- **Not `is_familiar`.** That allow-list is F6.
- **Not DC-2 eviction.** Offline batch is unbounded by design;
  A2 caps live memory.
- **Not a presence gate.** `[presence] rssi_threshold` still
  decides PRESENT. Band edges here are eval features.
