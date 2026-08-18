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
                │  devices / observations / presence /         │
                │  outbox / label_audit    (all keyed site_id) │
                │        │                                     │
                │        ▼                                     │
                │ Presence state machine                       │
                │  (N-consecutive-window RSSI, cooldowns)      │
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
(`Scanner`, `Notifier`, `Storage`) — swapping implementations is a config
edit, no code change. See `docs/adr/0002-extension-points.md` for the seam
contracts.

## Licensing (plain English)
MPL-2.0. Use it anywhere, including commercially. If you modify *these files*
and ship them, publish those modifications. Your own plugins, glue code, and
out-of-tree Scanner/Notifier backends are yours, under any license.

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

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md). Agentic contributions operate under
[AGENTS.md](AGENTS.md).
