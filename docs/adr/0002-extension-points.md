# ADR-0002: Scanner Seam

- **Status:** Proposed
- **Date:** 2026-08-16
- **Deciders:** Ryan Speed (human sign-off pending)

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

### Config-driven selection

`Scanner` implementation is selected via TOML config (P1-9), not import
paths.  The daemon never imports a concrete scanner by name; config maps
a string key to the class.

### Notifier seam (deferred)

`Notifier` protocol: `async send(message) -> DeliveryResult` with an async
iterator for inbound commands.  TelegramNotifier is locked (P0-2);
MockNotifier for CI.  Formal ADR deferred to P2-5.

### Storage seam (deferred)

Repository modules isolate all SQL.  No raw SQL outside repository files.
aiosqlite is the v1 backend.  Formal ADR deferred to P1-5/P1-6.

## Consequences

- Adding a new scanner backend is a single-file addition plus a config
  entry — no core code changes.
- All downstream tests use `MockScanner`, never real hardware.
- Protocol conformance is checked at runtime via `isinstance()` where
  useful, and statically by type checkers.
- The Scanner protocol is frozen for v1; changes require a new ADR.
