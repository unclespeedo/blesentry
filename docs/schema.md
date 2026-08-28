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
| `foreign_keys` | `ON` | Referential integrity across `devices` → `observations` / `presence_events` / `outbox` / `label_audit` / `device_aliases`. Per-connection; reapplied on every open. `init_sessions` has no FK (the device-id snapshot is JSON). `site_state` has no FK (opaque per-site key/value). |

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
| `address` | TEXT NULL | Currently-known source address (a peripheral UUID on CoreBluetooth captures); updated on each fused rotation (#19) and on an exact-key cache hit of an alias key (#153). Founding-key cache hits do not bump it (#84). Indexed (`idx_devices_address`). Renamed from `mac` in `0002`. |
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
| `device_id` | INTEGER NOT NULL | FK → `devices(id)` only — not site-qualified. A resolver on another site can stamp this with a foreign identity; `run_loop` fail-fasts before `seed()` and `run_cycle` before the cycle transaction if the resolver's connection or `site_id` differs from the cycle `devices` repo (#149). |
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

`idx_presence_events_site_device_id` on `(site_id, device_id, id)` (added
in `0005`) serves `list_present_unlabeled`: latest row per device is
`MAX(id)` grouped by `device_id` for one site.

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
leftover slots). Alias rows for the seeded window load in **one**
`list_aliases_for_devices` query (#153) — not one `list_aliases`
per device. Founding keys stay on `devices.fingerprint`; they
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
resolver's `_MAX_KEY_CACHE` (founding keys first). An exact-key
cache hit on an **alias** key still `touch_address` when the
advertisement carries an address, so `devices.address` tracks that
sighting (after restart via seed, and live via `commit()`);
founding-key cache hits stay a no-touch (#84).

All access is through `DeviceRepository` (`record_alias`,
`get_by_alias`, `list_aliases`, `list_aliases_for_devices`,
`prune_excess_aliases`). No other module may touch this table.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `site_id` | TEXT NOT NULL | |
| `fingerprint` | TEXT NOT NULL | Canonical fusion key (`fingerprint_key` JSON). `UNIQUE (site_id, fingerprint)`. |
| `device_id` | INTEGER NOT NULL | FK → `devices(id)`. Site-scoped at the repository (unknown or other-site id raises). Re-binding the same fingerprint to a *different* device is an alias conflict and raises — the trail must not silently rewrite identity. |
| `created_at` / `updated_at` | TEXT NOT NULL | ISO-8601 UTC, same `strftime` default as `devices`. Set at insert; an idempotent same-device `record_alias` is a no-write (does not bump `updated_at`). |

Indexed `(site_id, device_id)` for per-device audit listing. Added in
`0004` (#96); resolver persist/consume wired in #148; per-device
retention (32 newest) in #151; seed batch-load + alias cache-hit
touch in #153.

### `init_sessions`

At-most-one in-flight bulk-label session per site (`P2-7` `/init` and
`blesentry init`). The command loop and the CLI share this row so a
partial session survives daemon restart and can finish on the other
surface. All access is through `InitSessionRepository` — no other
module may touch this table.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `site_id` | TEXT NOT NULL | |
| `status` | TEXT NOT NULL | `ACTIVE` / `DONE` / `CANCELLED` / `EXPIRED`. CHECK constraint. Partial unique index `idx_init_sessions_one_active` on `site_id` WHERE `status = 'ACTIVE'` — a second live session for the site is a hard error, not a silent overwrite. |
| `cursor` | INTEGER NOT NULL | 0-based index into `device_ids` of the device currently being prompted. Default 0. |
| `device_ids` | TEXT NOT NULL | JSON array of integer device ids, **snapshotted at session start**. Resume walks this list (skipping any that have since been labeled), never a freshly queried present set — so a device that appears mid-session is not injected, and a device that leaves stays in the queue until skipped or labeled. |
| `expires_at` | TEXT NOT NULL | ISO-8601 UTC, same fixed-width format as other timestamps. Default time-box is 30 minutes from start (`blesentry.init.DEFAULT_TIMEOUT_SECONDS`). Compared lexicographically against `iso_utc(now)`. Any init-session touch (`/init`, free-text name, `/skip`, `/done`, bare `/ignore`, `/init cancel`, or `blesentry init`) flips a stale `ACTIVE` row to `EXPIRED`. Only `/init` / `blesentry init` then start a new snapshot; other expired touches reply `init session expired; send /init to start over` and do not apply the in-flight name. |
| `last_message_id` | INTEGER NULL | Telegram `message_id` of the last inbound update that mutated this session. A redelivered update with the same id re-prompts and does not apply again (getUpdates is at-least-once). CLI has no message id and leaves this unchanged. |
| `created_at` / `updated_at` | TEXT NOT NULL | ISO-8601 UTC; `updated_at` bumps on cursor/status changes. |

`device_ids` is JSON rather than a child table because the snapshot is
immutable and small (PRESENT unlabeled devices at one site — dozens, not
thousands). Ids are not FK-enforced (the snapshot must outlive a row the
operator then `/unlabel`s); missing ids are skipped at prompt time.

**Present** for the snapshot is the storage-seam definition: a device's
latest `presence_events` row **by `id`** (insert-monotonic, not
`occurred_at` — NTP steps must not hide a just-written ABSENT) is
`PRESENT`, and `devices.label IS NULL`. The in-memory `PresenceTracker`
is not consulted — the command loop runs on its own connection (#91)
and the tracker is not restart-seeded (#112).

Applying a name re-reads the cursor inside one `BEGIN IMMEDIATE`
transaction so a stale chat or CLI prompt cannot overwrite a label the
other surface just wrote.

Added in `0005` (#28).

### `site_state`

Opaque per-site key/value store for restart-stable daemon markers
(`P2-9` daily-summary last-sent). Not a dumping ground for config —
runtime *progress* that must survive process death lives here; tunables
stay in the TOML file. All access is through `SiteStateRepository`
(`get`, `set`) — no other module may touch this table.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `site_id` | TEXT NOT NULL | |
| `key` | TEXT NOT NULL | Opaque marker name, e.g. `daily_summary.last_sent`. `UNIQUE (site_id, key)`. |
| `value` | TEXT NOT NULL | Opaque payload. For `daily_summary.last_sent` this is the schema's fixed-width UTC timestamp of the last successful enqueue. |
| `updated_at` | TEXT NOT NULL | ISO-8601 UTC, same `strftime` default as `devices`. Bumped on every `set`. |

A `set` of `daily_summary.last_sent` is wrapped in the same ambient
transaction as the outbox enqueue it records (never a fire-and-forget
marker outliving a rolled-back digest, never a digest without a
marker). Added in `0006` (#30).

### `window_band_counts`

Per-cycle inclusive band-count snapshot from the post-resolve **`heard`**
map (F3 `band_counts`, default `BandEdges`). A replay/baseline cache for
the crowd (C3/C4) and inside (I3) detectors — derivable from
`observations`, not a second source of truth. One row per completed scan
cycle; written inside the cycle transaction (DC-1 / #132). Retention is
an incremental time-window `DELETE` at most once per UTC day (no
`VACUUM`; SD wear). Rows older than **8** wall-clock days are removed
(seven-day rolling fallback at the default 15 s cadence plus one day
buffer — `CROWD_ROLLING_WINDOWS` in `docs/crowd.md`).

All access is through `WindowBandCountRepository` — no other module may
touch this table.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `site_id` | TEXT NOT NULL | |
| `window_index` | INTEGER NOT NULL | Monotonic cycle ordinal from `run_cycle` / `run_loop`. |
| `observed_at` | TEXT NOT NULL | Cycle wall time (ISO-8601 UTC, schema timestamp format). Indexed with `site_id` for retention and C3 reads. |
| `count_all` | INTEGER NOT NULL | CHECK ≥ 0. Inclusive nested band counts: adjacent ≤ near ≤ far ≤ all. |
| `count_far` | INTEGER NOT NULL | CHECK ≥ 0. RSSI ≥ −80 (F3 default). |
| `count_near` | INTEGER NOT NULL | CHECK ≥ 0. RSSI ≥ −70 — crowd primary (ADR-0008). |
| `count_adjacent` | INTEGER NOT NULL | CHECK ≥ 0. RSSI ≥ −55 — inside feature band. |

Added in `0007` (#132).

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
- `P2-7` adds `init_sessions` (migration `0005`) and
  `DeviceRepository.list_present_unlabeled`.
- `P2-9` adds `site_state` (migration `0006`) plus
  `idx_presence_events_site_time` / `idx_devices_site_created` /
  `idx_outbox_site_status` for the digest window queries
  (`DeviceRepository.list_observed_between`,
  `DeviceRepository.list_created_between`,
  `PresenceEventRepository.list_between`, `OutboxRepository.count_failed`).
- Retention, clock-skew backfill, and integrity hardening are `P3-4`/`P4-5`
  concerns and do not change this schema.
