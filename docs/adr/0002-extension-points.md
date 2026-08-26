<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# ADR-0002: Extension-Point Architecture (Scanner / Notifier / Storage seams)

- **Status:** Accepted
- **Date:** 2026-08-16 (accepted 2026-08-17)
- **Deciders:** Ryan Speed

## Context

`blesentry` is designed as a modular OSS project from day 1.  The project
brief requires that external dependencies sit behind small interfaces with
config-selected implementations, so that:

- A third party can add a new scanner transport (e.g. raw HCI, ESP32 remote
  scanner) or notifier backend (e.g. Discord, ntfy) without forking.
- CI tests run against mock implementations, never touching hardware.
- Swapping implementations is a config edit, not a code change.

Three seams are identified: **Scanner**, **Notifier**, and **Storage**.  This
ADR defines the Scanner seam; the others follow in dedicated ADRs.

The first deployment target is a Raspberry Pi 3 A+ (512 MB RAM) with
BlueZ 5.82 on Raspberry Pi OS Lite Trixie arm64.  Every interface must
stay lean enough to run comfortably within this budget; the Scanner
protocol surface is deliberately minimal to keep the Python process,
BLE stack, and optional `tailscaled` under the ~407 MiB visible to
Linux after GPU/kernel reservations.

## Decision

### Scanner seam

The `Scanner` protocol (Python `typing.Protocol`, runtime-checkable) defines:

```python
class Scanner(Protocol):
    async def scan(self, duration: float) -> list[Advertisement]: ...
```

- `Advertisement` is the frozen Pydantic V2 value object (P1-1).
- The only surface the rest of the system touches.
- `MockScanner` (P1-2) replays scripted fixture data for CI.
- `BleakScanner` (P1-3) is the v1 production implementation.
- Future adapters (raw HCI, ESP32) implement the same protocol.

#### Error semantics

`scan()` uses a **fail-fast** contract at the protocol level:

- **Configuration errors** (unknown adapter name, passive mode
  requested without `or_patterns`, backend unavailable) — the
  implementation **raises** at construction or on the first
  `scan()`, never degrades.  A sentinel that cannot scan must never
  look like a quiet site.
- **Hardware failure** (adapter removed, HCI error, transport
  failure) — the implementation **raises** an exception.  Callers
  (the scan loop in P1-8) are expected to handle transient errors
  and escalate through the wedge-recovery ladder (P4-6) rather
  than silently swallowing failures.
- **No devices heard** — a valid, successful scan with zero
  advertisements returns an empty `list`.  This is *not* an error;
  it is the normal case during quiet periods or when no BLE devices
  are within range.
- **One malformed advertisement** — the only sanctioned
  degradation.  A single advertisement that fails normalization is
  logged and skipped (data-level degradation); the scan itself
  still succeeds and returns the rest.

**Wedge detection is implementation-specific**, not part of the
protocol contract.  BlueZ D-Bus can silently return nothing (a
"wedge") — this looks identical to a quiet scan at the protocol
level.  Each implementation decides how to detect wedges:
`BleakScanner` could, for example, timestamp the last advertisement
received and raise if the scan window completes with zero results
*and* no D-Bus activity was observed (planned under P4-6; not yet
implemented).  A raw-HCI backend would use different heuristics.  The recovery ladder (P4-6) starts with a
retry, then progresses through adapter reset and bluetooth.service
restart.

The rationale: silent failure hides real problems and makes remote
debugging impossible on a device that will be unreachable for months.
Returning an empty list on error would make P4-6 wedge detection
unreliable and would violate the project's "fail loud" posture for
remote-only infrastructure.  Keeping wedge detection
implementation-specific lets each backend use the most reliable
signal available to it without constraining the protocol.

#### Cancellation semantics

`scan()` supports asyncio `Task` cancellation, which is the expected
shutdown mechanism:

- On **SIGTERM** the main loop (P1-8) cancels the running `scan()`
  task.  The implementation must release the BLE adapter and clean
  up D-Bus resources in a `finally` block (or use an async context
  manager).
- On cancellation the implementation **re-raises `CancelledError`**
  after cleanup.  It must **not** swallow the exception — doing so
  breaks `task.cancelled()` and prevents standard asyncio shutdown
  coordination.  The caller (scan loop) catches `CancelledError`
  and treats any partially-collected advertisements as a normal
  short window.
- Implementations **must not** hold D-Bus connections or HCI file
  descriptors after cancellation — the adapter must be free for the
  next scan window or for a clean shutdown.

The rationale: the scan loop runs continuously for months.  Clean
cancellation on SIGTERM avoids orphaned D-Bus proxies and prevents
BlueZ from entering a wedged state due to abandoned connections.
Re-raising preserves cooperative cancellation semantics — the caller
decides how to handle partial results without the protocol needing
to define a special return path.

#### Concurrency contract

`scan()` is called from **a single asyncio task** at a time.  The
scan loop (P1-8) owns the only `Scanner` instance and never overlaps
`scan()` calls.  Implementations do **not** need to be
internally synchronised for concurrent calls, but they must not
assume single-threaded access to shared OS resources (the asyncio
event loop may interleave awaits).

If a future requirement needs concurrent scans (e.g. multi-radio or
scan-and-process overlap), it will be a new ADR — the protocol
surface does not change, only the calling convention.

#### Duplicate MAC policy

Implementations **may** return advertisements with the same MAC
address within a single scan window (e.g. a device sending multiple
advertisement types).  The fingerprint/resolver layer (P1-7) is
responsible for deduplication and identity fusion — the scanner is
a raw observation feed, not a deduplicated device list.

This is explicit rather than implicit because a third-party scanner
implementor might assume dedup is the scanner's job.  It is not:
dedup at the scanner level would lose information that the resolver
needs (different advertisement types from the same MAC can carry
different manufacturer data or service UUIDs).

#### Advertisement field table

The following table is the complete `Advertisement` output shape.
A third-party scanner implementor can build a conforming
implementation from this table and the protocol signature alone.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `mac` | `str` | yes | — | BLE device address, colon-separated hex (e.g. `"AA:BB:CC:DD:EE:FF"`).  May be a randomised address.  The model does not enforce case; capture backends *should* normalise to uppercase for consistency. |
| `rssi` | `int` | yes | — | Received signal strength indicator in dBm at the time of observation. |
| `local_name` | `str \| None` | no | `None` | The device's advertised local name (`LocalName` / complete local name).  `None` when the advertisement carries no name. |
| `service_uuids` | `Sequence[str]` | no | `()` | Service UUIDs advertised by the device (128-bit or 16-bit short form).  Converted to `tuple` at construction.  Case is not enforced by the model; backends should normalise for consistency. |
| `manufacturer_data` | `Mapping[str, str]` | no | `{}` | Manufacturer-specific data keyed by company ID, value as hex-encoded bytes string.  Converted to `MappingProxyType` at construction.  Key format (decimal vs hex string) varies by backend — the model accepts any string key. |
| `service_data` | `Mapping[str, str]` | no | `{}` | Service-specific data keyed by service UUID, value as hex-encoded bytes string.  Converted to `MappingProxyType` at construction. |
| `tx_power` | `int \| None` | no | `None` | Advertised transmit power in dBm, when present. |
| `timestamp` | `float` | yes | — | Unix epoch (seconds) of the observation, set by the capture backend. |
| `adapter_id` | `str` | yes | — | Identifier of the BLE adapter that heard this advertisement (e.g. `"hci0"`).  Enables multi-radio deployments. |

The model is defined in `src/blesentry/scanner/models.py` as a
Pydantic V2 `BaseModel` with `ConfigDict(frozen=True, extra="forbid")`.
Container fields (`service_uuids`, `manufacturer_data`,
`service_data`) are replaced with immutable equivalents in
`model_post_init` — the model is frozen after construction.

### Config-driven selection

`Scanner` implementation is selected via TOML config (P1-9), not import
paths.  The daemon never imports a concrete scanner by name; config maps
a string key to the class.

Example config snippet (P1-9 will produce the full schema):

```toml
[scanner]
# Backend selector — maps to a concrete Scanner implementation.
# "bleak"  → blesentry.scanner.bleak.BleakScanner  (production)
# "mock"   → blesentry.scanner.mock.MockScanner    (tests/CI)
backend = "bleak"

[scanner.options]
# Backend-specific options; passed as kwargs to the constructor.
adapter_id = "hci0"
```

The registry pattern is straightforward: a dictionary mapping backend
names to (module_path, class_name) pairs, resolved at startup.
Implementations are imported lazily so unused backends do not add to
the import-time memory footprint — important on 512 MB.

### Notifier seam (deferred)

`Notifier` protocol: `async send(message) -> DeliveryResult` with an async
iterator for inbound commands.  TelegramNotifier is locked (P0-2);
MockNotifier for CI.  Formal ADR deferred to P2-5.

### Storage seam (deferred)

Repository modules isolate all SQL.  No raw SQL outside repository files.
aiosqlite is the v1 backend.  Formal ADR deferred to P1-5/P1-6.

### Resolver seam (named internal; not a plugin)

The README's **Device resolver** box is a fourth *named* seam.  It is
**not** a config-selected extension point — there is one
`DeviceResolver` implementation; `[resolver]` tunes thresholds, it
does not pick a backend.  Duplicate-MAC policy (above) still defers
deduplication and identity fusion to this seam.

Lifecycle (`resolve` / `commit` / `abort` / `seed`), the
cycle-transaction contract, the `min_score` floor, and the durable
`device_aliases` path are ADR-0005.  Changing that surface is a new
ADR, not an ADR-0002 amendment.

## Consequences

- Adding a new scanner backend is a single-file addition plus a config
  entry — no core code changes.
- All downstream tests use `MockScanner`, never real hardware.
- Protocol conformance is checked at runtime via `isinstance()` where
  useful, and statically by type checkers.
- The Scanner protocol is frozen for v1; changes require a new ADR.
- Error semantics are explicit: callers can distinguish "quiet scan"
  from "hardware failure" without probing implementation internals.
- The Advertisement field table is the single source of truth for
  scanner implementors — no need to read Python source to build a
  conforming backend.
- The concurrency contract is single-caller; concurrent scan support
  requires a new ADR.
- Duplicate MAC policy is the resolver's responsibility, not the
  scanner's — documented to prevent implementor confusion.
- The protocol surface stays minimal (one async method, one data
  model) to keep import-time overhead and memory usage low on
  constrained hardware.

## Future Considerations

- **P4-3: raw-HCI scanner backend.** If bleak/BlueZ passive mode
  misses transient advertisements or scan interval/window control
  proves necessary, an `HciScanner` implementing the same protocol
  is the documented escape hatch.  The protocol seam is designed to
  validate with a second implementation (P4-3 spike).
- **ESP32 remote scanner.** A network-attached BLE scanner
  (ESP32 + NimBLE) feeding advertisements to the Pi over MQTT or
  WebSocket.  Would implement the same `Scanner` protocol on the
  Pi-side agent; the ESP32 firmware is outside this ADR's scope.
- **`scan_stream()` variant.** A streaming/async-iterator interface
  (`AsyncIterator[Advertisement]`) for event-driven architectures
  that do not want fixed-window batching.  Deferred unless a
  concrete need arises; the window-based `scan()` covers all v1
  use cases and is simpler to reason about for the presence state
  machine.
