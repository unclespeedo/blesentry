# blesentry schema v1

SQLite database layout for the storage seam. All SQL lives in repository
modules (`P1-6`); nothing else touches the database. The schema is deployed
exclusively by the migration runner — never by hand.

## Migration strategy

- Migration scripts are numbered SQL files in
  `src/blesentry/storage/migrations/` (e.g. `0001_schema_v1.sql`).
- The runner (`blesentry.storage.database.apply_migrations`) executes files
  in filename order, recording each applied version in a `schema_migrations`
  bookkeeping table with a sha256 checksum of the file.
- **Idempotent:** re-running the runner is a no-op; already-applied versions
  are skipped. A version whose file changed since it was applied raises
  `MigrationError` — never silently ignore a modified script.
- **Atomic:** each migration runs inside a transaction and rolls back
  completely on failure. Files must contain plain DDL statements only — the
  runner wraps them in `BEGIN`/`COMMIT`; do not include transaction
  statements yourself.
- Add a new migration by appending the next-numbered `NNNN_*.sql` file. Do
  not edit an already-shipped file.
- **Schema-changing deploys:** stop the collector, deploy, then start it
  (startup migrates). A still-running old collector fails loudly after a
  migration renames columns under it, and a migration that cannot get the
  write lock raises `MigrationError` leaving the schema untouched — safe
  to retry once the collector is stopped.

## Connection pragmas

Applied by `blesentry.storage.database.connect` on every open. Choice
rationale targets SD-card longevity on the Pi 3 A+ (512MB):

| PRAGMA | Value | Why |
|---|---|---|
| `journal_mode` | `WAL` | Fewer fsyncs than the default rollback journal; readers never block the writer; crash-safe without the checkpoint-every-commit cost. Persists in the DB file. |
| `synchronous` | `NORMAL` | In WAL mode this is durable against application crashes and cannot corrupt on power loss; a power cut may lose the most recent committed transactions (acceptable — see `P4-5`), which is the standard trade-off on flash. |
| `busy_timeout` | `5000` | The daemon is long-lived with a single writer; this prevents spurious `SQLITE_BUSY` on transient lock contention. |
| `foreign_keys` | `ON` | Referential integrity across `devices` → `observations` / `presence_events` / `outbox` / `label_audit` / `device_aliases`. Per-connection; reapplied on every open. |

## Conventions

- **`site_id` on every table.** `TEXT`, opaque site identifier from config
  (`P1-9`). Guarantees a second site's data partitions cleanly (`P4-8`)
  without any schema change.
- **Timestamps are TEXT** in ISO-8601 UTC with fractional seconds, e.g.
  `2026-08-16T05:25:18.123Z`, via `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`.
  Sorts correctly lexicographically. Wall-clock skew at boot is a
  daemon-level concern (`P4-5`), not a schema one.
- **`id` is `INTEGER PRIMARY KEY`** (rowid alias) everywhere.

## Tables

### `schema_migrations`

Runner bookkeeping: `version` (filename), `checksum`, `applied_at`. Not
created by a migration file — the runner bootstraps it.

### `devices`

One row per resolved identity. Identity is the fingerprint fusion key
(`P1-7`), not the raw MAC — MACs randomize.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `site_id` | TEXT NOT NULL | |
| `fingerprint` | TEXT NOT NULL | The identity's *founding* (first-seen) key, versioned (`\"v\":2`). Fusion aliases (later fingerprints resolved to the same device) persist in `device_aliases` (ADR-0005 / #148): `DeviceResolver.resolve` writes them inside the cycle transaction and consults them after this founding-key lookup; `seed()` warms the exact-key cache from both, and backfills leftover window slots with aliases (founding keys keep the budget). A fingerprint must not be both a founding key and an alias (enforced in `DeviceRepository`). `UNIQUE (site_id, fingerprint)`. |
| `address` | TEXT NULL | Currently-known source address (a peripheral UUID on CoreBluetooth captures); updated on each fused rotation (#19), so it tracks the most recent fusion event. Indexed (`idx_devices_address`). Renamed from `mac` in `0002`. |
| `label` | TEXT NULL | Operator-assigned friendly name (`P2-6/P2-7`); null until labeled. |
| `description` | TEXT NULL | Optional operator notes. |
| `created_at` / `updated_at` | TEXT NOT NULL | `updated_at` means *identity/metadata changed*, not last-seen (observations carry last-seen); the per-sighting bump was removed in #84. |

### `observations`

Append-only rolling RSSI history, kept indefinitely (by decision). Each
advertisement heard becomes a row.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `site_id` | TEXT NOT NULL | |
| `device_id` | INTEGER NOT NULL | FK → `devices(id)` only — not site-qualified. A resolver on another site can stamp this with a foreign identity; `run_cycle` fail-fasts if the resolver's connection or `site_id` differs from the cycle `devices` repo (#149). |
| `rssi` | INTEGER NOT NULL | dBm, typically negative. |
| `observed_at` | TEXT NOT NULL | When the advertisement was heard. Indexed with `site_id` and `device_id` for the P1-6 recent-window queries. |
| `adapter_id` | TEXT NULL | Which radio adapter heard it (from the `Advertisement` model). |
| `address_type` | TEXT NULL | Authoritative provenance at reception (`public` / `random_static` / `rpa` / `non_resolvable`, plus unrefined `random` for the reserved 0b10 bit pattern); NULL where the OS does not report it (CoreBluetooth) — and NULL on every row predating `0002`: treat those as heuristic-grade regardless of adapter. Added in `0002` (#56). |
| `adv_type` | TEXT NULL | PDU type when a backend exposes it; null on both current backends. Added in `0002` (#56). |

### `presence_events`

One row per ABSENT↔PRESENT transition from the presence state machine
(`P2-1`).

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `site_id` | TEXT NOT NULL | |
| `device_id` | INTEGER NOT NULL | FK → `devices(id)`. |
| `event_type` | TEXT NOT NULL | CHECK constraint: `PRESENT` or `ABSENT`. |
| `occurred_at` | TEXT NOT NULL | Indexed with `device_id`. |

### `outbox`

Every outbound message (alert, summary, deferred command reply) is written
here before any delivery attempt (`P2-3`); the drain loop (`P2-4`) claims and
delivers. Nothing is ever fire-and-forget.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `site_id` | TEXT NOT NULL | |
| `status` | TEXT NOT NULL | `PENDING` (default) → `IN_FLIGHT` → `DELIVERED`; `FAILED` on repeated error. CHECK constraint. |
| `attempt_count` | INTEGER NOT NULL | Delivery attempts so far, default 0. |
| `next_attempt_at` | TEXT NULL | Backoff/jitter scheduling (`P2-4`); null when not scheduled. Indexed with `status` (`idx_outbox_claim`). |
| `payload` | TEXT NOT NULL | Opaque to storage. By convention (P2-4) it is a JSON-serialized `notifier.models.OutboundMessage`; the drain loop deserializes it, and a producer (P2-6/P2-8) enqueues `OutboundMessage(...).model_dump_json()`. An unparseable payload is dead-lettered. |
| `last_error` | TEXT NULL | Most recent delivery failure detail. |
| `created_at` | TEXT NOT NULL | |

### `device_aliases`

Durable fusion aliases (ADR-0005 / #96 / #148). One row per *later*
fused fingerprint bound to an existing device — a truncated audit
of which keys were absorbed into which identity (newest 32 per
device), and the restart-stable lookup path for those retained
keys. `DeviceResolver.resolve` persists a fused key via
`record_alias` inside the ambient cycle transaction (not from
`commit()`, which is synchronous and runs after COMMIT). `resolve`
consults `get_by_alias` after the founding-key exact match;
`seed()` warms cache and window from aliases as well as founding
keys (founding keys keep the window budget; aliases backfill
leftover slots). Founding keys stay on `devices.fingerprint`; they
must not also appear here (repository-enforced). Per-device
retention keeps the 32 newest alias rows (highest `id` = insert
order, not wall clock — NTP steps must not delete the row just
inserted). A pruned key that is also outside the recent window is
not restart-stable: it opens a new device row (split identity),
so this table is a truncated audit of recent joins, not a complete
absorption history. Do not export alias rows into issues or CI
(fingerprints embed addresses, names, and payloads). `record_alias`
prunes on insert and `prune_excess_aliases` sweeps at `seed()` so
a pre-retention database is bounded without waiting for the next
rotation. `seed()` also caps the in-process exact-key cache at the
resolver's `_MAX_KEY_CACHE` (founding keys first).

All access is through `DeviceRepository` (`record_alias`,
`get_by_alias`, `list_aliases`, `prune_excess_aliases`). No other
module may touch this table.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `site_id` | TEXT NOT NULL | |
| `fingerprint` | TEXT NOT NULL | Canonical fusion key (`fingerprint_key` JSON). `UNIQUE (site_id, fingerprint)`. |
| `device_id` | INTEGER NOT NULL | FK → `devices(id)`. Site-scoped at the repository (unknown or other-site id raises). Re-binding the same fingerprint to a *different* device is an alias conflict and raises — the trail must not silently rewrite identity. |
| `created_at` / `updated_at` | TEXT NOT NULL | ISO-8601 UTC, same `strftime` default as `devices`. Set at insert; an idempotent same-device `record_alias` is a no-write (does not bump `updated_at`). |

Indexed `(site_id, device_id)` for per-device audit listing. Added in
`0004` (#96); resolver persist/consume wired in #148; per-device
retention (32 newest) in #151.

### `label_audit`

Append-only record of every label change: who, what, when (`P2-6`).

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `site_id` | TEXT NOT NULL | |
| `device_id` | INTEGER NOT NULL | FK → `devices(id)`. |
| `actor` | TEXT NOT NULL | Who changed it (operator id, bot session, CLI). |
| `previous_label` / `new_label` | TEXT NULL | Diff of the change; null label means unlabeled. |
| `changed_at` | TEXT NOT NULL | Indexed with `device_id`. |

## Later phases

- `P2-3/P2-4` implement the outbox claim/deliver query on `idx_outbox_claim`.
- `P1-6` adds the repository layer over these tables (the only SQL writers).
- Retention, clock-skew backfill, and integrity hardening are `P3-4`/`P4-5`
  concerns and do not change this schema.
