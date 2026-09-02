<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# Offline replay harness (F1)

Feed historical observations through any `Detector` without a radio,
without the scan loop, and without writing the source. Clock-free,
deterministic, off-device (DC-9). The live cycle and this harness
share one input type: `DetectionWindow` (ADR-0006).

This is an evaluation tool. It does **not** enqueue to the outbox.
Live `run_cycle` wiring is A3 (`docs/approach.md`): the daemon
calls the same `observe` inside the cycle transaction. Canonical
feature vectors over the same windows are F3 (`docs/features.md`);
replay does not emit them.

## Why

Approach / crowd / inside detectors (M2–M4) and the F5 evaluation
report need a way to ask "what would this detector have fired on
this log?" against a sanitized fixture or a copied SQLite snapshot.
Replay answers that with a JSON report a golden-file test can pin.

## Inputs

Exactly one source:

| Source | CLI | Windows contain |
|---|---|---|
| Sanitized advertisement fixture (JSON array of `Advertisement`) | `--fixture PATH` with `--backend none`/`mock`/`approach` | `advertisements` populated; `heard` empty (no resolver ran) |
| Sanitized heard-window fixture (JSON array of `{heard: [[id,rssi],…]}`) | `--fixture PATH` with `--backend inside` or `crowd` | `heard` populated; `advertisements` empty |
| Immutable `observations` snapshot | `--db PATH --site-id ID` | `heard` = per-`device_id` best RSSI; `advertisements` empty (payloads are not in that table) |

A detector that needs **both** streams in one window is out of
scope here. Observation-row replay does not reconstruct
advertisement payloads from `devices.fingerprint`.

`--db` must be a **copy**, not the live daemon database. A long
reader against the live WAL pins checkpointing and the run is not
reproducible (DC-9). The harness opens the file `mode=ro` with
`PRAGMA query_only=ON` and never runs migrations.

## Windowing

Windows are indexed, not wall-clocked (DC-4 / DC-9).

- **Period** defaults to the scan cadence: `scan.window + scan.pause`
  = **15 s**. Override with `--period`. Must be finite and `> 0`.
- `t0` is the earliest timestamp in the source (advertisement
  `timestamp`, or `observations.observed_at` parsed as UTC).
- Window index `i = floor((t − t0) / period)`, dense from `0`
  through the last occupied index **inclusive**.
- A gap in the source becomes an **empty** window (no advertisements,
  empty `heard`). Later detectors that count misses need those
  calls; skipping them would lie about dwell.
- An empty source yields zero windows, not an error.

Heard-window fixtures are one JSON object per window index (dense
from 0); they do not use timestamp bucketing or `--period`. The report
`period` field is still 15.0 (scan cadence label only).

N-day span: a timestamp-bucketed source whose last timestamp is `Δ`
seconds after `t0` produces `floor(Δ / period) + 1` windows, including
empties. Two days at the default 15 s period is 11,521 windows if the
last sample lands on the last instant of day two; tests pin the formula
with a 86400 s period so the count stays small.

## Detector

`--backend none` (default), `mock`, `approach`, `inside`, or `crowd` — the same
closed union as `[detection]` (ADR-0006). `none` emits nothing;
unscripted `mock` records windows and also emits nothing. `approach`
is A3 (`ApproachDetector`); it reads `advertisements` only, so
`--fixture` (advertisement JSON) is the path that can fire. `inside`
is I3 (`InsideDetector`); it reads `heard` only, so `--fixture`
(heard-window JSON, e.g. `inside-dwell.json`) or `--db` snapshot
replay is the path that can fire. `crowd` is C4 (`CrowdDetector`);
it also reads `heard` only (synthetic timestamps from window index
when `prepare_window` is not used). `--db` snapshot replay has empty
`advertisements` and will not produce approach events.
Scripted `MockDetector` events are a unit-test concern, not a CLI
flag.

`observe` is synchronous and I/O-free. Replay just loops:

```text
for window in windows:
    events.extend(detector.observe(window))
```

Returned `DetectionEvent.window_index` is the clock-free timestamp.

## Report

Stdout is JSON (stable key order) so a golden file can diff it:

```json
{
  "events": [],
  "period": 15.0,
  "window_count": 4,
  "windows": [
    {"advertisement_count": 1, "heard": [], "index": 0},
    {"advertisement_count": 1, "heard": [], "index": 1},
    {"advertisement_count": 0, "heard": [], "index": 2},
    {"advertisement_count": 1, "heard": [], "index": 3}
  ]
}
```

Fixture replay: advertisement fixtures have empty `heard`; heard-window
fixtures (inside) and snapshot replay populate `heard` as
`[device_id, rssi]` pairs sorted by `device_id`. A single run never
fills both streams.

- `heard` is a list of `[device_id, rssi]` pairs, sorted by
  `device_id`. JSON objects cannot carry integer keys.
- Window summaries carry counts, not addresses or payloads
  (SECURITY.md). Do not dump a capture corpus through replay into
  CI logs.
- `events` is the concatenated `observe` output, in window order.

Same source + period + backend → identical `format_report` text
(`json.dumps(..., indent=2, sort_keys=True)`). The committed golden
is compared as parsed JSON, so a trailing newline on the file does
not matter.

## CLI

```text
blesentry replay --fixture tests/fixtures/replay/span.json
blesentry replay --fixture path.json --period 15 --backend mock
blesentry replay --fixture tests/fixtures/replay/walkby.json --backend approach
blesentry replay --db snapshot.db --site-id example-site --period 15
```

`--fixture` cannot combine with `--db` / `--site-id`. Unknown
backend is a load-time error (fail-fast, ADR-0002).

## Tests

Synthetic fixtures live under `tests/fixtures/replay/` — not a
capture corpus. `tests/fixtures/*.json` remains the sanitized
advertisement schema (`tests/test_fixtures.py` is non-recursive).
The span golden pins windowing + empty `none` events. The walk-by
golden pins one `approaching` event at the peak (A3). Neither is a
live site log.
