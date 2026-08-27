<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# ADR-0006: Detector seam — protocol, window, config selection

- **Status:** Accepted
- **Date:** 2026-08-26
- **Deciders:** Ryan Speed

## Context

The shipped alert primitive is per-identity: an unlabeled device that
reaches PRESENT fires. Against real passive-collection logs that
fatigues for identity fragmentation (RPA rotation) and density (every
lingering unlabeled device nags). The adaptive-detection epic
(`docs/detection-plan.md`) adds detectors that decide on *what
changed* in the RF environment rather than *who* a device is.

Those detectors — approach, crowd, inside — must share one interface so
the offline replay harness (F1) and the live scan cycle consume the
same type. ADR-0002 already requires config-selected, lazily imported
backends for extension points. Detector is that kind of seam
(unlike Resolver, which is a named internal singleton — ADR-0005).

Constraints that this ADR must freeze, not leave as folklore:

- **DC-1.** Detectors run on the in-memory window (advertisement
  batch + post-resolve `heard` map). No per-cycle `observations`
  reads. Alert *enqueue* stays the cycle consumer's job, inside the
  existing cycle transaction, mirroring `alerter.handle`.
- **DC-4 / DC-9.** The Pi has no RTC and replay must be
  clock-free. Windows are indexed, not wall-clocked.
- **DC-7.** Approach is pre-fusion (raw addresses). Presence and
  inside-style own-gear exclusion are post-resolve (`device_id`).
  The window therefore carries **both** streams.

F2 ships the seam only: protocol, value objects, `none` / `mock`
backends, `[detection]` config. Real detectors and `run_cycle`
wiring are later issues.

## Decision

### Config-selected extension point

`blesentry.detection.protocol.Detector` is a fourth ADR-0002 plugin
seam. Implementation is selected by `[detection] backend`, not an
import path. Unused backends are imported lazily (512 MB target).

The v1 closed union (same posture as Scanner / Notifier; open
registry is #101, not this ADR):

| `backend` | Class | Role |
|---|---|---|
| `none` (default) | `NullDetector` | Emits nothing. Existing daemons unchanged. |
| `mock` | `MockDetector` | Records windows; replays scripted events. CI and F1. |

Adding a backend is a discriminated-union edit in `config.py` plus
one module — the same recipe as a new Scanner. Approach / crowd /
inside backends are later issues; they are **not** legal tags until
those issues land.

### Frozen method surface

```python
@runtime_checkable
class Detector(Protocol):
    def observe(self, window: DetectionWindow) -> Sequence[DetectionEvent]: ...
```

`observe` is **synchronous**. A detector must not perform I/O, await,
touch a repository, or enqueue to the outbox. The scan-cycle
consumer (later) calls `observe` inside the cycle transaction and
enqueues from the returned events, the same way `alerter.handle`
enqueues from presence transitions.

`observe` is called from **one asyncio task** at a time (the scan
loop). Implementations do not need internal synchronization. They
must be O(window) with bounded memory (DC-2 is an implementation
duty: caps and eviction live on the concrete detector, not this
protocol).

### `DetectionWindow` field table

One scan window, frozen and closed. The complete input shape; a
third-party detector can implement `observe` from this table and the
protocol signature alone.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `index` | `int` | yes | — | Clock-free window ordinal, `>= 0`. Live cycle will use its cycle count; F1 replay assigns a dense 0-based index. **Not** a wall-clock timestamp (DC-4 / DC-9). |
| `advertisements` | `Sequence[Advertisement]` | no | `()` | Pre-fusion observation stream for this window (DC-7). Empty is a valid quiet window or a replay that only has aggregates. Converted to `tuple` at construction. |
| `heard` | `Mapping[int, int]` | no | `{}` | Post-resolve per-`device_id` best RSSI (dBm) in this window — the same map `presence.update` consumes. Converted to `MappingProxyType` at construction. Callers must copy; mutating the dict they passed in must not change the window. |

Empty `advertisements` **and** empty `heard` is success, not an
error — a quiet site is the common case.

### `DetectionEvent` field table

The envelope later detectors fill. Kind vocabulary is **not** frozen
here; A1 / C1 / I1 own their `kind` tokens. The envelope rejects
empty `detector` / `kind` and a negative `window_index`.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `detector` | `str` | yes | — | Backend id that produced the event (`mock`, later `approach` / `crowd` / `inside`). |
| `kind` | `str` | yes | — | Detector-defined event class. Opaque to the seam. |
| `window_index` | `int` | yes | — | The `DetectionWindow.index` that produced this event (`>= 0`). Replay's clock-free timestamp. |

No payload bag. Detector-specific fields are added later with
defaults (additive, like `OutboundMessage` staying `text`-only for
v1). Changing the method surface or removing a field is a new ADR.

### Error semantics

- **Empty window** — return an empty sequence. Never raise.
- **No detection** — return an empty sequence. That is the default
  `none` backend's only behaviour.
- **Programmer / invariant errors** (bad construction, type errors)
  — raise. Fail loud.
- **Must not enqueue.** Returning events is the whole contract.
  Outbox writes belong to the cycle consumer so F1 replay can run
  read-only against a snapshot (DC-9) and a rolled-back cycle cannot
  leak an alert (same atomicity as `alerter.handle`).
- **Must not mutate** the window or its containers. The window is
  frozen; implementations that need a working copy make one.

Wedge / scan-health (DC-5, P4-6) is **not** a protocol argument in
this ADR. When a liveness signal exists it will be added as an
additive `DetectionWindow` field with a default, in a new ADR — not
by smuggling health through `heard`.

### Null and mock

`NullDetector.observe` always returns `()`. It exists so the daemon
never branches on "is detection configured."

`MockDetector` records each `DetectionWindow` it observes (in order)
and pops scripted event batches supplied at construction. Exhausted
script → empty sequence, still recorded. It is the F1 / CI double,
mirroring `MockNotifier` / `MockScanner`.

The `blesentry.detection` package `__init__` is docstring-only and
does not re-export backends (same as `blesentry.notifier`). Importing
`NullDetector` must not load `mock`.

### Config

```toml
[detection]
# "none" (default) → NullDetector. "mock" → MockDetector.
backend = "none"
```

Omitting `[detection]` is `none`. An unknown `backend` is a
load-time `ConfigError` (fail-fast, ADR-0002).

`build_detector` is the lazy factory, next to `build_scanner` /
`build_notifier`. F2 does **not** call it from `run_cycle`; the first
alert-emitting detector issue wires that.

## Consequences

- Approach / crowd / inside can land as additional union members
  without touching Scanner, Notifier, or Resolver.
- F1 replay and the live cycle share one input type. Replay does not
  need a second protocol.
- Default `none` means shipping the seam does not change alert
  behaviour. Presence (P2-1) stays the v1 alert primitive until a
  later issue enables a real backend.
- The protocol stays I/O-free, so F1 can run off-device against an
  immutable snapshot without pinning the live WAL (DC-9).
- Closed union keeps fail-fast tags; an open plugin path is #101.
- This ADR is **Accepted** (human sign-off 2026-08-27 on #169).
  Changing the frozen surface after accept is a new ADR, not an
  ADR-0002 amendment beyond the pointer this PR adds.

## Future Considerations

- **`run_cycle` wiring (A3 / C4 / I3).** Call `observe` inside the
  existing cycle transaction; enqueue returned events via the
  outbox, mirroring `alerter.handle`. Not F2.
- **Composite / stack backend (L5).** A backend that fans a window
  out to several detectors. New union member or a dedicated wrapper;
  not a protocol change.
- **Scan-health field (DC-5 / P4-6).** Additive `healthy: bool =
  True` (or a richer enum) on `DetectionWindow` in a new ADR.
- **Detector-specific event fields.** Additive optional fields on
  `DetectionEvent` (peak RSSI, band counts, address roster) as those
  specs freeze. Keep `extra="forbid"`.
- **Feature extractor (F3).** Canonical feature vectors for eval;
  A2's online tracker reuses the definitions. Not part of this
  protocol.
