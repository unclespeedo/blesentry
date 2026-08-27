# blesentry

Local-first BLE presence sentinel for remote sites. Scans, fingerprints, and
learns the Bluetooth devices around it; alerts an operator via private chat
when unknown devices appear; survives multi-day network outages with zero
data loss. First deployment target: Raspberry Pi 3 A+ at an off-grid cabin.

**Status: pre-alpha — Phase 0 (see [ROADMAP.md](ROADMAP.md)).**

## Architecture

```
                ┌──────────────────────────────────────────────┐
                │                blesentry daemon              │
                │                                              │
 BLE adverts ──▶│ Scanner (protocol)                           │
                │   ├─ BleakScanner  (v1, BlueZ passive)       │
                │   └─ HciScanner    (escape hatch, Phase 4)   │
                │        │                                     │
                │        ▼                                     │
                │ Fingerprint engine ──▶ Device resolver       │
                │  (address + svc UUIDs + mfr data + adv name)     │
                │        │                                     │
                │        ▼                                     │
                │ SQLite (aiosqlite) — source of truth         │
                │  devices / device_aliases / observations /   │
                │  presence / outbox / label_audit /           │
                │  init_sessions / site_state                  │
                │                          (all keyed site_id) │
                │        │                                     │
                │        ▼                                     │
                │ Presence state machine                       │
                │  (N-consecutive-window RSSI, cooldowns)      │
                │        │                                     │
                │        ▼                                     │
                │ Detector (protocol; A3 in scan loop)         │
                │   ├─ NullDetector  (none, default)           │
                │   ├─ MockDetector  (CI / F1 replay)          │
                │   └─ ApproachDetector (approach; A3)         │
                │        │                                     │
                │        ▼                                     │
                │ Outbox ──▶ Drain loop (exp. backoff) ──▶     │
                │             Notifier (protocol)              │
                │               ├─ TelegramNotifier (locked)   │
                │               └─ DiscordNotifier (fut. P4-7) │
                │                                              │
                │ Bot command handler ◀── inbound chat msgs    │
                │  (label, status, list, force-scan, init…)    │
                └──────────────────────────────────────────────┘
```

Every external dependency sits behind a small config-selected interface
(`Scanner`, `Notifier`, `Storage`, `Detector`) — swapping
implementations is a config edit, no code change. See
`docs/adr/0002-extension-points.md` for those plugin contracts.
Detector's frozen surface (`observe(window) → events`) is ADR-0006;
v1 backends are `none` (default, no events), `mock` (CI / replay),
and `approach` (A3 rising-RSSI). Crowd / inside are later issues.
`run_cycle` calls `observe` inside the cycle transaction and
enqueues returned events (DC-1); default `none` does not change
alert behaviour. The Device resolver is a *named
internal* seam (ADR-0005), not a backend selector: one instance, one
lifecycle, no plugin registry.

## Licensing (plain English)
MPL-2.0. Use it anywhere, including commercially. If you modify *these files*
and ship them, publish those modifications. Your own plugins, glue code, and
out-of-tree Scanner/Notifier/Detector backends are yours, under any license.

## Deploying to the Pi

One command from a dev machine to a provisioned node (see
`scripts/provision/flash-notes.md` for provisioning):

```
scripts/deploy.sh                 # $USER@blesentry-pi.local
scripts/deploy.sh pi@other.local  # explicit target
```

rsyncs the working tree (never git state), runs `uv sync` on the Pi,
restarts `blesentry.service` when it exists (P3-1), and smoke-checks
the install. Idempotent — a no-op re-run takes ~3 seconds.
`BLESENTRY_HOST` / `BLESENTRY_DIR` override the defaults.

At a busy site the defaults will alert on every lingering device. Turn it
down with the `[presence]` section — start by raising `rssi_threshold` to
alert only on nearby devices. See `docs/tuning.md` for the recipe.

### Operator commands (Telegram)

Once a Telegram notifier is configured, the daemon's command loop accepts
`/status`, `/list`, `/label`, `/unlabel`, `/ignore`, `/describe`, and
`/init`. `/init` starts a time-boxed bulk-label session of currently
PRESENT unlabeled devices (reply with a name — no slash — then `/skip` /
`/ignore` / `/done` / `/init cancel`). After 30 minutes the session
expires; `/init` starts a fresh list. The same session is reachable on
the host with `blesentry init --config config.local.toml` — useful for
the first on-site pass; Ctrl-D pauses it for later resume. Don't type a
name on a CLI prompt after answering the same device in chat (EOF-pause
the CLI first); the session is shared. See `docs/tuning.md`.

A **daily digest** (devices seen, new devices, presence transitions,
outbox health) is enqueued at or after `[summary] hour_utc` (default
12:00 UTC) and delivered through the same outbox. Disable with
`[summary] enabled = false`. See `docs/tuning.md`.

### Offline detector replay

`blesentry replay` feeds a sanitized advertisement fixture or a
**copied** observations snapshot through any Detector (ADR-0006) and
prints a JSON report of would-be alerts. Clock-free window indexes,
read-only, no outbox writes. Use a snapshot copy, never the live
daemon database. See `docs/replay.md`.

Canonical per-window / per-identity **feature vectors** for eval
(band counts, churn, rolling RSSI slope, dwell) are
`blesentry.detection.features` — offline batch, not a detector.
See `docs/features.md`.

The **approach** detector (`[detection] backend = "approach"`) is
A3: ADR-0007 / `docs/approach.md`. It wraps the A1 predicate
(`is_rising_approach`) and the A2 tracker (`TrajectoryTracker`)
and emits `kind="approaching"` inside the scan-cycle transaction.
Default `backend` is still `none`. Replay a labeled walk-by with
`blesentry replay --fixture tests/fixtures/replay/walkby.json
--backend approach`. Alert text reports the F3 coarse band and
RSSI, never a distance.

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md). Agentic contributions operate under
[AGENTS.md](AGENTS.md). This project's instances observe real sites — read
[SECURITY.md](SECURITY.md) before contributing: operational site details
never enter this repository.
