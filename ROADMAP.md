# ROADMAP.md — v1.2 (merged v1.0 + v1.1 + v1.2)

# `blesentry` — BLE Presence Sentinel for Remote Sites

> Repo name: **`unclespeedo/blesentry`** (short, searchable, describes
> function not location — this is deliberately a general-purpose BLE presence
> sentinel, of which "my cabin" is the first deployment).

---

## 1. Executive Summary

`blesentry` is a local-first, offline-safe BLE presence scanner. It runs
continuously on low-power hardware (first target: Raspberry Pi 3 A+),
fingerprints every BLE device it hears, stores all observations in a local
SQLite database, and maintains a presence state machine per device. Unknown
devices trigger alerts to a private interactive chat, where the operator
labels them — the system learns friendly names and descriptions over time.
An "init mode" bulk-labels the operator's own devices on demand.

**Design stance:** although v1 targets one Pi, one BLE radio, and one chat
platform, the project is structured as a **modular OSS project** from day 1.
Every external dependency sits behind a small interface with a config-selected
implementation:

| Seam | v1 implementation | Alternatives (documented, pluggable) |
|---|---|---|
| `Scanner` | bleak (BlueZ/D-Bus, passive) | raw HCI / bleson, macOS CoreBluetooth (dev), future ESP32 remote scanner |
| `Notifier` | Telegram (locked, P0-2) | Discord/ntfy (P4-7); webhook later |
| Storage | SQLite via aiosqlite | schema carries `site_id`; repository layer isolates SQL |
| Host | Pi 3 A+ / Raspberry Pi OS | any Linux SBC with BlueZ; hardware notes kept generic |

Locked decisions (per project brief, not revisited here): Python 3 + uv,
ruff (79) + ty + pytest TDD, Pydantic V2 `ConfigDict` models, aiosqlite,
fingerprint fusion identity, consecutive-window RSSI debouncing as a v1
acceptance criterion, systemd + watchdog + journald, no Docker, no runtime
cloud dependencies, MPL-2.0 license (DCO sign-off, no CLA).

---

## 2. Architecture Overview

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
                │  (MAC + svc UUIDs + mfr data + adv name)     │
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

Key properties:

- **Local-first / offline-safe.** Every alert is written to the `outbox` table
  before any delivery attempt. A drain loop retries with exponential backoff.
  Multi-day Starlink outages lose nothing; alerts arrive late, in order.
- **Scanner seam.** `Scanner.scan(duration) -> list[Advertisement]` is the
  only surface the rest of the system touches. `MockScanner` (replaying a
  fixture corpus of real captured advertisements) drives all CI tests.
- **Device identity = fingerprint fusion** from day 1: MAC alone is unreliable
  under randomization (especially Apple). Resolver scores candidates across
  MAC, service UUIDs, manufacturer data, and advertised name.
- **Schema highlights:** `site_id` on every table; `observations` is an
  append-only rolling RSSI history (kept indefinitely); `presence_events`
  records ABSENT↔PRESENT transitions; `label_audit` records every label change
  with actor and timestamp.
- **Config-driven modularity:** a single TOML config (pydantic-settings)
  selects scanner transport, notifier backend, site identity, and thresholds —
  no code changes to swap implementations.

---

## 3. Staged Roadmap

Sizes: **S** ≈ one short session (<2h), **M** ≈ one focused session (half day),
**L** ≈ needs splitting or a full day. Labels use `phase:N`, `epic:<name>`,
`type:<feature|infra|spike|docs|test|ops>`, `priority:<p0|p1|p2>`.

The **3-week physical-access window (ended 2026-08-30)** was the dominant
scheduling constraint for hands-on / on-site ground-truth work. See §5 for
the historical week-by-week plan.

**Post-window status (2026-09-02):** Leave-Site Gate closed (P0-7…P0-11,
P3-0). Phases 0–2 are complete on GitHub. Human operator has remote
deploy access for the near term (agents still must not SSH to the Pi).
Current focus: Phase 5 detection remainder (eval / vehicle class / C5)
+ remote Phase 3 ops; Phase 4 stays gated on P3-6 soak. See **Phase 5**
below and `docs/detection-plan.md`.

---

### Phase 0 — Provisioning & Decision Gates

Goal: hardware reachable remotely, decisions written down, empty-but-green repo.

#### Epic 0.A — Decision Gates

**P0-1 · Decision: OS image (64-bit Bookworm vs 32-bit) — verify uv support**
Evaluate Raspberry Pi OS Lite Bookworm arm64 vs armhf on the Pi 3 A+ (512MB).
Verify `uv` binary availability/behavior for the chosen arch (uv armv7 support
is unverified — check before considering 32-bit). Check BlueZ version shipped
and its passive-scan D-Bus behavior. Write a short ADR (`docs/adr/0001-os-image.md`).
- **DoD:** ADR committed with decision, rationale, BlueZ version, memory headroom notes.
- **Deps:** none. **Size:** S. **Labels:** `phase:0 epic:decisions type:spike priority:p0`

**P0-2 · ADR: Telegram as v1 chat platform** *(decision gate → record)*
Write `docs/adr/0003-chat-platform.md` recording Telegram (bot token +
single chat_id, long-poll `getUpdates` — no webhook, works fine behind
Meraki/CGNAT with zero inbound exposure). Record the decided security posture:
transport security + private chat is sufficient; single-operator auth =
configured `chat_id` **and** `user_id` must both match. Note Discord/ntfy as
future `Notifier` adapters (P4-7).
- **DoD:** ADR merged; auth rule stated verbatim for P2-5/P2-8 to implement.
- **Deps:** none. **Size:** S *(was M)*. **Labels:** `phase:0 epic:decisions type:docs priority:p0`

**P0-3 · Spike: bleak passive scanning on macOS + BlueZ feasibility check**
Throwaway script: run `bleak` passive scan on the dev Mac (CoreBluetooth),
capture ~15 min of real advertisements to JSON — this becomes the seed of the
test fixture corpus. Document known BlueZ passive-mode caveats (D-Bus
duplicate filtering, no scan interval/window control) and confirm raw-HCI
(bleson as head start) as the documented escape hatch.
- **DoD:** Captured corpus committed under `tests/fixtures/`; risk notes added to `docs/risks.md`.
- **Deps:** none. **Size:** M. **Labels:** `phase:0 epic:decisions type:spike priority:p0`

#### Epic 0.B — Repo Scaffold

**P0-4 · Scaffold repo: uv, ruff (79), ty, pytest, Pydantic V2, src layout, licensing**
New repo under `unclespeedo`. `pyproject.toml` with uv-managed deps, ruff
line-length 79, ty config, pytest + pytest-asyncio, `src/blesentry/` package,
`py.typed`. Follow python-dev skill conventions. Include `CONTRIBUTING.md`
stating the plugin/seam philosophy (scanner + notifier are extension points).
Licensing moves here (was P4-9): commit `LICENSE` (MPL-2.0 verbatim); MPL
Exhibit A 3-line header wired into the file template so every new file
carries it automatically; `CONTRIBUTING.md` states inbound=outbound MPL-2.0,
DCO `Signed-off-by` required, and "out-of-tree plugins may use any license."
README gains the plain-English licensing paragraph ("use anywhere including
commercially; ship modified *blesentry files* → publish those modifications;
your own plugins/glue are yours").
- **DoD:** `uv run pytest` green (one placeholder test); `uv run ruff check` and ty clean; README stub with architecture diagram; LICENSE present; header template wired; ruff/CI unaffected; DCO check noted in CONTRIBUTING (enforcement tooling optional, deferred).
- **Deps:** none. **Size:** M. **Labels:** `phase:0 epic:scaffold type:infra priority:p0`

**P0-4a · ADR 0004: license choice** *(new, trivial — bundled with P0-4)*
Record MPL-2.0 selection, the Q1–Q7 answer pattern, the Q6 relicensing
trade-off accepted, and the explicit decision to keep Secondary-Licenses
compatibility.
- **DoD:** ADR merged. **Deps:** P0-4. **Size:** S. **Labels:** `phase:0 epic:scaffold type:docs priority:p1`

**P0-5 · CI pipeline: lint + typecheck + tests on push**
GitHub Actions: ruff, ty, pytest (mocked scanner only — no hardware). Cache uv.
Matrix on the Python version targeted by the chosen OS image (from P0-1).
- **DoD:** Green pipeline on main; branch protection notes in CONTRIBUTING.
- **Deps:** P0-1, P0-4. **Size:** S. **Labels:** `phase:0 epic:scaffold type:infra priority:p0`

**P0-6 · ADR: extension-point architecture (Scanner / Notifier / Storage seams)**
Document the modular design: protocols, config-driven selection, how a third
party would add a new scanner transport or notifier backend. This is the
contract that keeps the project general-purpose OSS rather than one-off.
- **DoD:** `docs/adr/0002-extension-points.md` merged; interfaces named and frozen for v1.
- **Deps:** P0-2, P0-3. **Size:** S. **Labels:** `phase:0 epic:scaffold type:docs priority:p1`

#### Epic 0.C — Pi Provisioning (physical-access window; ends 2026-08-30)

**P0-7 · SD card pre-seed procedure (Mac): WiFi, SSH, authorized_keys, hostname**
Documented, repeatable flash procedure using Raspberry Pi Imager or
`firstrun.sh` customization: headless WiFi, SSH enabled, key-only auth,
static-ish identity. Script what can be scripted (`scripts/provision/flash-notes.md`
+ any helper script). Written generically so a second site/board can reuse it.
- **DoD:** Pi boots headless and is SSH-reachable on first boot with keys only.
- **Deps:** P0-1. **Size:** M. **Labels:** `phase:0 epic:provisioning type:ops priority:p0`

**P0-8 · Remote-access verification: DMVPN + client VPN + Tailscale + cellular failover**
Verify SSH to the Pi via each path independently: (a) DMVPN from home, (b)
client VPN, (c) Tailscale (install `tailscaled` on the Pi — access path only;
the blesentry daemon must never depend on it). Then **pull the Starlink WAN**
and confirm at least one path survives on Meraki cellular failover. Document
per-path addresses/instructions in the runbook. Standard hardening (key-only
SSH, no password auth) unchanged.
- **DoD:** All three paths verified; cellular-failover SSH verified with Starlink physically disconnected; runbook section written; **tailscaled-at-boot posture decided and recorded** (optional, access-only — never a daemon dependency); **This is a leave-site gate.**
- **Deps:** P0-7. **Size:** M *(was S)*. **Labels:** `phase:0 epic:provisioning type:ops priority:p0`

**P0-9 · Deploy script: rsync/git push-to-Pi + uv sync**
`scripts/deploy.sh`: rsync (or git pull) working tree to Pi, `uv sync`,
restart service if present. Idempotent, safe to run mid-development. No Docker.
- **DoD:** One command deploys from Mac to Pi; documented in README.
- **Deps:** P0-7. **Size:** S. **Labels:** `phase:0 epic:provisioning type:ops priority:p0`

**P0-10 · Smart plug: remote power-cycle path for the Pi** *(new — window-locked)*
Put the Pi (and ideally its network dependency chain — confirm the plug itself
isn't behind the Pi's power) on the existing smart plug. Verify remote
off/on-cycle over each VPN path. Confirm the Pi boots cleanly and blesentry
auto-starts after an uncommanded power cut (pairs with P4-5 pull-the-plug
testing, but the basic verification happens now, in-window).
- **DoD:** Remote power-cycle executed successfully from off-site network path (simulate via VPN); recovery-to-service documented in runbook; escalation order documented: SSH restart → smart plug cycle → human visit.
- **Deps:** P0-7, P0-8. **Size:** S. **Labels:** `phase:0 epic:provisioning type:ops priority:p0`

**P0-11 · In-window labeled data capture: walk tests + drive-bys** *(new — window-locked)*
While physically present (irreplaceable after the window): run the scanner and
capture **ground-truth-labeled** sessions — (a) walk the perimeter with each
personal device, noting RSSI vs location; (b) drive your own car past on the
road several times at realistic speed; (c) capture a quiet baseline overnight.
These become the real-data fixture corpus that P2-1 (presence thresholds) and
P4-2 (car tuning) are calibrated against — otherwise those issues wait months
for organic data.
- **DoD:** ≥3 labeled walk sessions, ≥5 labeled drive-by passes, 1 overnight baseline committed as fixtures with a capture log; initial RSSI threshold estimate recorded in `docs/tuning.md`.
- **Deps:** P1-3, P1-4 (needs at least the one-shot scan CLI on the Pi). **Size:** M. **Labels:** `phase:0 epic:provisioning type:test priority:p0`

---

### Phase 1 — Core Skeleton POC

Goal: scan → fingerprint → resolve → persist, fully tested against MockScanner,
runnable for real on Mac and Pi.

#### Epic 1.A — Scanner Seam & Models

**P1-1 · `Advertisement` + fingerprint Pydantic models**
Pydantic V2 models (`ConfigDict(frozen=True)` where sensible): `Advertisement`
(mac, rssi, service UUIDs, manufacturer data, adv name, timestamp, adapter id),
`Fingerprint` (derived, hashable). TDD from the P0-3 captured corpus.
- **DoD:** Models parse the entire fixture corpus; edge cases (empty name, no mfr data) tested.
- **Deps:** P0-3, P0-4. **Size:** M. **Labels:** `phase:1 epic:scanner type:feature priority:p0`

**P1-2 · `Scanner` protocol + `MockScanner` fixture replay**
`Scanner` protocol: `async scan(duration: float) -> list[Advertisement]`.
`MockScanner` replays fixture corpus with controllable time/ordering — the
workhorse of all CI tests.
- **DoD:** Protocol documented per P0-6 ADR; MockScanner supports scripted scenarios (device appears/disappears/MAC rotates).
- **Deps:** P1-1. **Size:** M. **Labels:** `phase:1 epic:scanner type:feature priority:p0`

**P1-3 · `BleakScanner` adapter (passive mode, BlueZ + CoreBluetooth)**
Real implementation over bleak. Passive scanning via BlueZ on Linux; works in
active/whatever-CoreBluetooth-allows mode on macOS for dev. Normalizes both
backends into `Advertisement`. Log-and-degrade on D-Bus hiccups.
- **DoD:** Live scan on Mac produces Advertisements; manual run on Pi over SSH does too; backend differences documented.
- **Deps:** P1-2. **Size:** L. **Labels:** `phase:1 epic:scanner type:feature priority:p0`

**P1-4 · CLI: `blesentry scan --duration N` one-shot debug command**
Thin CLI (argparse or typer — smallest footprint wins on 512MB) printing a
scan-result table. Primary remote-debugging tool for the whole project's life.
- **DoD:** Works on Mac and Pi; `--json` output flag for scripting.
- **Deps:** P1-3. **Size:** S. **Labels:** `phase:1 epic:scanner type:feature priority:p1`

#### Epic 1.B — Storage

**P1-5 · SQLite schema v1 + migration runner**
Tables: `devices`, `observations`, `presence_events`, `outbox`, `label_audit` —
all with `site_id`. Lightweight ordered-SQL-file migration runner (no Alembic
dependency unless justified). WAL mode, sane pragmas for SD-card longevity.
- **DoD:** Schema documented in `docs/schema.md`; migrations apply idempotently; WAL confirmed.
- **Deps:** P0-4. **Size:** M. **Labels:** `phase:1 epic:storage type:feature priority:p0`

**P1-6 · Repository layer (aiosqlite) for devices + observations**
Async repository: upsert device, append observation, query recent RSSI window,
list devices. All SQL confined here — this is the storage seam.
- **DoD:** TDD against in-memory/tmpfile SQLite; no raw SQL outside repositories.
- **Deps:** P1-5, P1-1. **Size:** M. **Labels:** `phase:1 epic:storage type:feature priority:p0`

#### Epic 1.C — Fingerprinting & Resolution

**P1-7 · Fingerprint fusion + device resolver**
Given an `Advertisement`, resolve to an existing device or create a new one.
Scoring across MAC (weakest), service UUIDs, manufacturer data, adv name.
Explicit, tested handling for: rotated MAC + stable mfr data (Apple pattern);
identical fingerprints on different MACs; nulls everywhere.
- **DoD:** Resolver test suite covers MAC-rotation scenarios from fixture corpus; matching thresholds configurable.
- **Deps:** P1-1, P1-6. **Size:** L. **Labels:** `phase:1 epic:fingerprint type:feature priority:p0`

**P1-8 · Scan loop: continuous scan → resolve → persist**
The asyncio main loop: scan window, resolve each advertisement, write
observations, log summary. Configurable window/pause. Graceful SIGTERM.
- **DoD:** Runs unattended 1h on Mac with real radio; DB fills; memory stable (measure — 512MB target).
- **Deps:** P1-3, P1-6, P1-7. **Size:** M. **Labels:** `phase:1 epic:core-loop type:feature priority:p0`

**P1-9 · Config system: TOML + pydantic-settings**
Single config file selecting site_id, scanner backend, notifier backend (stub),
thresholds, paths. This is where the modularity contract becomes real: swapping
implementations is a config edit.
- **DoD:** Example config committed; invalid config fails fast with clear errors; all Phase-1 knobs wired.
- **Deps:** P0-6, P1-8. **Size:** M. **Labels:** `phase:1 epic:core-loop type:feature priority:p0`

---

### Phase 2 — Presence, Alerts, Bot, Outbox

Goal: the product. Unknown device → debounced alert → label in chat → learned.

#### Epic 2.A — Presence State Machine

**P2-1 · Presence state machine (ABSENT/PRESENT, consecutive windows, cooldowns)**
Per-device state machine: N consecutive scan windows above RSSI threshold →
PRESENT; M consecutive misses → ABSENT; per-device alert cooldown. **This is a
v1 acceptance criterion, not polish** — the remote road makes passing cars a
real false-positive source. Transitions persisted to `presence_events`.
Thresholds calibrated against the P0-11 ground-truth corpus (see P0-11).
- **DoD:** Exhaustive state-transition tests via MockScanner scripted scenarios, including "car drives past" (brief strong signal, 1–2 windows) producing NO alert.
- **Deps:** P1-7, P1-8. **Size:** L. **Labels:** `phase:2 epic:presence type:feature priority:p0`

**P2-2 · Presence tuning knobs in config + `docs/tuning.md`**
Expose thresholds (RSSI floor, N/M windows, cooldown seconds) in config with
documented defaults and a tuning guide (values will be re-derived from real
site data in Phase 4).
- **DoD:** All knobs config-driven; tuning doc explains each with expected symptoms.
- **Deps:** P2-1, P1-9. **Size:** S. **Labels:** `phase:2 epic:presence type:docs priority:p1`

#### Epic 2.B — Outbox & Delivery

**P2-3 · Outbox table + enqueue API**
Every outbound message (alert, summary, command reply that can be deferred)
written to `outbox` with status, attempt count, next-attempt-at. Enqueue is
synchronous with the triggering event — nothing is ever fire-and-forget.
- **DoD:** Alerts generated during simulated network outage are fully preserved and ordered.
- **Deps:** P1-6. **Size:** M. **Labels:** `phase:2 epic:outbox type:feature priority:p0`

**P2-4 · Drain loop with exponential backoff + jitter**
Async task draining the outbox through the `Notifier`. Exponential backoff
capped at (e.g.) 15 min, jittered; survives multi-day outages; marks delivered;
never drops on repeated failure.
- **DoD:** Test simulating 3-day outage: all messages delivered in order on reconnect; backoff behavior asserted.
- **Deps:** P2-3, P2-5. **Size:** M. **Labels:** `phase:2 epic:outbox type:feature priority:p0`

#### Epic 2.C — Notifier & Bot

**P2-5 · `Notifier` protocol + `TelegramNotifier`** *(platform locked)*
Protocol: `send(message) -> DeliveryResult`; inbound side: async iterator of
`InboundCommand`. Implement Telegram directly (long-poll `getUpdates`, no
webhook). Single-operator auth per P0-2 ADR: configured `chat_id` **and**
`user_id` must both match, hard-rejected otherwise. `MockNotifier` for CI.
Library choice (python-telegram-bot LGPL-3.0 is license-compatible vs aiogram,
MIT) is decided purely on technical merits: async fit, RAM footprint on 512MB,
long-poll ergonomics. Adapter is deliberately thin so the alternative platform
is a future S/M issue (P4-7), not a rewrite.
- **DoD:** Round-trip on real chat account; auth restricted to configured chat/user id pair; `MockNotifier` for CI.
- **Deps:** P0-2, P0-6. **Size:** M *(was L)*. **Labels:** `phase:2 epic:bot type:feature priority:p0`

**P2-6 · Unknown-device alert → interactive label flow**
New unknown device reaches PRESENT → alert with fingerprint summary → operator
replies with label/description (or "ignore") referencing the alert → device
updated, `label_audit` written. Must tolerate replies arriving days later.
- **DoD:** End-to-end test (MockScanner + MockNotifier): unknown device alerted once, labeled via reply, no re-alert after labeling.
- **Deps:** P2-1, P2-4, P2-5. **Size:** L. **Labels:** `phase:2 epic:bot type:feature priority:p0`

**P2-7 · Init mode: bulk-label personal devices on demand**
Chat command (`/init`) enters a labeling session: system lists currently
present devices one at a time; operator labels each; session is resumable and
time-boxed. Also runnable from CLI for the first physical-access setup.
- **DoD:** Full init session over mock bot labels 5 devices; partial session resumes correctly.
- **Deps:** P2-6. **Size:** M. **Labels:** `phase:2 epic:bot type:feature priority:p0`

**P2-8 · Admin commands: status, list-devices, force-scan, label, unlabel, set-description**
Command router with per-command handlers, help text, and auth check (single
operator per P0-2: configured `chat_id` **and** `user_id` must both match).
`status` includes uptime, DB size, outbox depth, last-scan time, present devices.
- **DoD:** Each command has unit tests + one integration test over MockNotifier; unauthorized senders rejected and logged; explicit test: correct chat_id but wrong user_id is rejected.
- **Deps:** P2-5, P1-6. **Size:** M. **Labels:** `phase:2 epic:bot type:feature priority:p0`

**P2-9 · Daily summary message**
Scheduled daily digest: devices seen, new devices, presence transitions,
outbox/delivery health. Goes through the outbox like everything else, so an
outage yields late-but-complete summaries.
- **DoD:** Summary content snapshot-tested; scheduling survives restart (persisted last-sent marker, no double-send).
- **Deps:** P2-3, P2-8. **Size:** M. **Labels:** `phase:2 epic:bot type:feature priority:p1`

---

### Phase 3 — Pi Deployment & Operations

Goal: unattended service on the Pi; leave the site with confidence.

**P3-0 · Early systemd + continuous data collection** *(pulled forward into the window)*
Split from P3-1: get a *minimal* systemd unit running the Phase-1 scan loop
(observations only — no presence, no bot) before leaving the site, so real
winter/road data accumulates from day ~10 onward. Full watchdog/hardening
remains P3-1, deliverable remotely.
- **DoD:** Scan loop running under systemd `Restart=always` on the Pi, writing observations 24/7; survives P0-10 power-cycle test; DB growth rate measured and extrapolated against SD capacity.
- **Deps:** P1-8, P0-9. **Size:** M. **Labels:** `phase:3 epic:deploy type:ops priority:p0`

**P3-1 · systemd unit: Restart=always, watchdog, journald**
Service unit with `Restart=always`, `WatchdogSec` + sd_notify pings from the
main loop, `MemoryMax` guard, journald logging. Non-root user with BLE
capabilities (or documented D-Bus policy).
- **DoD:** `systemctl kill` → auto-restart; hung-loop simulation → watchdog restart; unit file in repo under `deploy/`.
- **Deps:** P1-8, P0-9. **Size:** M. **Labels:** `phase:3 epic:deploy type:ops priority:p0`

**P3-2 · Log rotation + journald limits**
Bound journald disk usage; app-level log levels sane for months of unattended
operation on an SD card.
- **DoD:** Journald caps configured via deploy script; 24h soak shows bounded log growth.
- **Deps:** P3-1. **Size:** S. **Labels:** `phase:3 epic:deploy type:ops priority:p0`

**P3-3 · On-Pi integration test suite (run over SSH)**
`make test-pi` (or script): rsync + run marked `@pytest.mark.hardware` tests on
the Pi — real bleak scan, real DB on SD, service restart check. CI stays
mock-only; this is the manual pre-"leave site" gate.
- **DoD:** Suite green on the Pi; documented in runbook as release checklist step.
- **Deps:** P1-3, P3-1. **Size:** M. **Labels:** `phase:3 epic:deploy type:test priority:p0`

**P3-4 · Resource watchdog: memory/disk/DB-size self-monitoring**
Loop-internal checks: RSS, free disk, DB size, outbox depth. Thresholds emit
warnings through the outbox (so the operator hears about problems from the
system itself). Critical disk pressure triggers safe degradation (pause
observation writes before corrupting).
- **DoD:** Threshold breaches produce chat warnings in test; degradation path tested.
- **Deps:** P2-4, P3-1. **Size:** M. **Labels:** `phase:3 epic:deploy type:feature priority:p1`

**P3-5 · Runbook: remote-only operations & recovery**
`docs/runbook.md`: deploy, roll back, read journald remotely, recover from
BlueZ wedge (`hciconfig`/`bluetoothctl` resets, `systemctl restart bluetooth`),
what to do when SSH is unreachable (cellular path, power-cycle escalation to
the smart plug, power-cycle-by-human instructions for a neighbor). Includes
per-path addresses/instructions from P0-8.
- **DoD:** Runbook covers every failure encountered so far; dry-run of rollback executed.
- **Deps:** P3-1, P3-3. **Size:** M. **Labels:** `phase:3 epic:deploy type:docs priority:p0`

**P3-6 · 72-hour soak on-site + go/no-go checklist**
Run the full system for 72h at the cabin (can be remote). Verify: no memory
creep, no missed watchdog, alerts flow, labels stick, daily summaries arrive,
simulated WAN pull (if remotely safe) drains outbox on recovery. The
collection-only P3-0 service is what soaks in-window; the full-product soak
repeats remotely after Phase 2.
- **DoD:** Soak report committed; go/no-go checklist signed off.
- **Deps:** All P3, P2-6..P2-9. **Size:** M. **Labels:** `phase:3 epic:deploy type:test priority:p0`

---

### Phase 4 — Long-Tail Hardening

Goal: survive winter; tune with real data; generalize.

#### Epic 4.A — Fingerprint & Detection Quality

**P4-1 · MAC-randomization edge cases from real site data**
After ≥2 weeks of real observations: audit resolver decisions, find split
identities (one phone = many device rows) and merged identities. Add a
`/merge` admin command and resolver fixes. Grow the fixture corpus from real
captures.
- **DoD:** Known personal devices each resolve to exactly one row over a week; merge command audited in `label_audit`.
- **Deps:** P3-6. **Size:** L. **Labels:** `phase:4 epic:fingerprint type:feature priority:p1`

**P4-2 · Car / pass-by tuning with real data (+ "car detected" bonus)**
Analyze real drive-by signatures (short dwell, characteristic mfr data — cars
advertise CarPlay/tire sensors/etc.). Tune N/M/cooldown from data — starts
from the P0-11 ground-truth drive-by corpus rather than guesses. Bonus:
classify and alert "car passed" as a distinct, low-priority event type instead
of suppressing it silently.
- **DoD:** One week with zero false "unknown person" alerts from road traffic; car classification precision documented.
- **Deps:** P3-6, P2-2. **Size:** L. **Labels:** `phase:4 epic:detection type:feature priority:p1`

**P4-3 · Spike: raw-HCI scanner backend (bleson head start)**
The documented escape hatch: if bleak/BlueZ passive mode misses transient
advertisements or scan interval/window control proves necessary, prototype an
`HciScanner` implementing the same `Scanner` protocol. Compare capture rates
side-by-side against bleak on the Pi.
- **DoD:** Capture-rate comparison report; go/no-go on adopting HCI backend; either way the protocol seam is validated with a second implementation.
- **Deps:** P1-2, P3-3. **Size:** L. **Labels:** `phase:4 epic:scanner type:spike priority:p2`

#### Epic 4.B — Resilience & Backup

**P4-4 · Off-site backup: restic → S3-compatible bucket**
Periodic encrypted backup of the SQLite DB (proper `sqlite3 .backup`/
`VACUUM INTO` snapshot, not a live-file copy) via restic to an S3-compatible
bucket. Credentials in a root-owned config outside the repo. Opportunistic —
skips cleanly during outages. **Local storage remains the source of truth.**
- **DoD:** Restore drill performed from backup; backup failures reported via chat, never block the daemon; **S3 provider chosen and documented** (in-issue decision).
- **Deps:** P3-4. **Size:** M. **Labels:** `phase:4 epic:resilience type:ops priority:p1`

**P4-5 · Unattended winter recovery: power loss, clock skew, DB integrity**
Pi 3 has no RTC: handle boot-with-wrong-clock (defer timestamp-sensitive logic
until NTP sync or use monotonic ordering + wall-clock backfill). Test
power-cut-mid-write DB recovery (WAL). fsck/ro-remount considerations for SD.
Ensure service start ordering waits for bluetooth.service and network-online
where needed — but starts scanning even with no WAN.
- **DoD:** Pull-the-plug test ×10 with no DB corruption; wrong-clock boot produces sane data; documented in runbook. Pull-the-plug basics are exercised *early* via P0-10/P3-0.
- **Deps:** P3-1, P3-5. **Size:** L. **Labels:** `phase:4 epic:resilience type:feature priority:p0`

**P4-6 · BlueZ wedge auto-recovery**
Detect the known failure mode where scans silently return nothing (BlueZ/D-Bus
wedge): automatic escalation — restart scan, restart bluetooth.service, hci
reset, finally reboot (with rate limit) — each step reported via outbox.
- **DoD:** Injected-wedge test triggers escalation ladder; reboot rate-limited to prevent boot loops.
- **Deps:** P3-4, P3-5. **Size:** M. **Labels:** `phase:4 epic:resilience type:feature priority:p1`

#### Epic 4.C — Generalization (OSS long tail)

**P4-7 · Second notifier adapter (the P0-2 runner-up or ntfy)**
Implement one more `Notifier` backend to prove the seam and give the OSS
project a real choice matrix. Config-selected, feature-matrix documented.
- **DoD:** Alerts + at least `status` command work on second platform; docs updated.
- **Deps:** P2-5. **Size:** M. **Labels:** `phase:4 epic:generalize type:feature priority:p2`

**P4-8 · Multi-site readiness pass + hardware compatibility notes**
Audit that nothing but config hardcodes `site_id` or Pi-3-specific assumptions.
Document tested/expected hardware alternatives (Pi Zero 2 W, Pi 4/5, generic
Linux + USB BLE dongle) and what to check (BlueZ version, adapter passive-scan
support).
- **DoD:** `docs/hardware.md` published; a second `site_id` in config produces correctly-partitioned data in tests.
- **Deps:** P3-6. **Size:** M. **Labels:** `phase:4 epic:generalize type:docs priority:p2`

**P4-9 · OSS polish: README quickstart, example configs, hardware notes, release tags**
README quickstart from blank SD card to first alert, annotated example configs
for each scanner/notifier combo, hardware notes cross-links, `v1.0.0` tag.
License decision is **resolved (MPL-2.0, handled in P0-4/P0-4a)** — nothing
else blocks on it.
- **DoD:** A stranger could deploy from README alone; v1.0.0 tagged after P3-6 sign-off.
- **Deps:** P3-6, P4-8. **Size:** M. **Labels:** `phase:4 epic:generalize type:docs priority:p2`

---

### Phase 5 — Adaptive Detection *(pointer; not a redesign of §3 Phases 0–4)*

Pulled forward from the original Phase-4 detection quality track once
continuous collection showed identity-alert fatigue. Full plan:
[`docs/detection-plan.md`](docs/detection-plan.md). GitHub milestones:

| Milestone | Focus | Status (2026-09-02) |
|---|---|---|
| M1 — Detection foundation & eval harness | F1–F6 replay, seam, features, corpus, eval, familiar | Core closed; F4/F5 open |
| M2 — Approach detector | A1–A5 rising trajectory + vehicle class | A1–A3 (+A2 follow-up #174) closed; A4/A5 open |
| M3 — Crowd anomaly detector | C1–C5 baseline + CUSUM | C1–C4 closed; C5 open |
| M4 — Inside presence detector | I1–I4 sustained adjacent | **Closed** |
| M5 — Learning loop & bake-off | L1–L5 labels, gating, bake-off | All open |

**Boundary vs Phase 4 Hardening:** P4-1 / P4-2 remain under Phase 4
(still Deps: P3-6). Phase 5 owns the adaptive M1–M5 stack; A4 vehicle
class feeds later P4-2 real-data tuning, not an early unblock of P4-2.

Issue IDs are in the Appendix. Snapshot (2026-09-02): next
`agent:eligible` pick was **C5 (#135)** with main CI green —
**AGENTS.md SELECT remains authoritative** if labels change.

---

## 4. Decision Record

All decision gates are closed. The table records the locked decisions; any
future revisit is an ADR-level change.

| # | Decision | Effect on roadmap |
|---|---|---|
| 1 | Repo name = **`unclespeedo/blesentry`** | Alternates dropped. |
| 2 | Physical access window = **3 weeks, ending 2026-08-30** | Epic 0.C + all physically-gated work (P0-8, P0-10, P0-11, P3-0) must complete inside the window. Week-by-week plan in §5. |
| 3 | **Smart plug available** for remote power-cycle | New issue P0-10. Removes the worst-case "bricked until spring" risk. |
| 4 | **Telegram** is sufficient (transport security + private chat OK) | P0-2 downgraded from evaluation to ADR record (S). P2-5 becomes `TelegramNotifier` directly. Matrix/Signal E2EE path dropped. |
| 5 | Network: Starlink → **Meraki WAN w/ cellular SIM failover, DMVPN to home, client VPN, Tailscale on all devices** | P0-8 rewritten: no reverse-tunnel work needed. Three independent remote-access paths. Optional tailscaled on Pi (access path only — daemon has zero runtime dependency on it). |
| 6 | Backup target = **S3-compatible bucket via restic** | P4-4 target confirmed; no design ambiguity remains. |
| 7 | License initially deferred to P4-9 | Superseded by decision #9 (MPL-2.0). |
| 8 | **Single operator** | Bot auth = exactly one `chat_id` + `user_id` pair in config, hard-rejected otherwise. Simplifies P2-5/P2-8; multi-user auth explicitly out of scope. |
| 9 | **License = MPL-2.0**, default Secondary-Licenses provision intact (GPL-compatible), **DCO sign-off** for contributions, **no CLA** | File-level copyleft matches stated policy: core files stay open, adopters' glue and out-of-tree plugins are unencumbered — preserving the platform ambition. Explicit patent grant + retaliation. ADR: `docs/adr/0004-license.md`. |

**Status:** All decision gates closed and recorded (OS image ADR-0001,
Telegram, network/access, smart plug, restic→S3, single operator,
MPL-2.0). Phases 0–2 and Leave-Site Gate are closed on GitHub; Phase 5
detection is in progress. §5 (Weeks 1–3) is the **historical** window
plan (ended 2026-08-30); **live** sequencing is the §3 Post-window
status blurb + Phase 5 pointer above.

---

## 5. Three-Week Window Plan (ends 2026-08-30)

> **Historical.** Physical-access window ended 2026-08-30. Weeks 1–3
> below are archive. For current priority, use the §3 Post-window status
> blurb and the Phase 5 pointer — not this section’s Post-window bullets
> alone.

**Principle:** the window gates *physical* work, not software completion.
Phase 2 (bot, presence, alerts) deploys fine over SSH later. What cannot be
done remotely: provisioning, network/failover verification, smart plug wiring,
antenna/placement decisions, and **ground-truth data capture (P0-11)** —
prioritize those ruthlessly.

### Week 1 — Provision & scaffold (parallel tracks)
- **Hardware track:** P0-1 (OS decision — decide fast, default 64-bit Bookworm) → P0-7 (flash + first boot) → P0-8 (verify all 3 VPN paths + cellular failover) → P0-10 (smart plug) → P0-9 (deploy script).
- **Software track (Mac):** P0-3 (bleak spike + first fixture corpus), P0-4 (scaffold + licensing), P0-5 (CI), P0-2 (Telegram ADR), P0-6 (extension-points ADR).
- **Week-1 exit gate:** Pi reachable via 3 paths, power-cyclable remotely, one-command deploy works.

### Week 2 — Core pipeline on real hardware
- P1-1 → P1-2 → P1-3 → P1-4 (scanner seam + CLI) and P1-5 → P1-6 (storage) in parallel; then P1-7 (resolver), P1-8 (scan loop), P1-9 (config).
- Deploy to Pi mid-week; **P3-0** by end of week: continuous collection running under systemd.
- Begin P0-11 capture sessions as soon as P1-4 lands on the Pi.
- **Week-2 exit gate:** observations accumulating 24/7 on the Pi; memory/DB growth measured.

### Week 3 — Verify, capture, soak — then leave
- Finish P0-11 (all labeled sessions). Adjust antenna/Pi placement based on walk-test RSSI while you still can.
- P3-1 (full watchdog unit), P3-2 (log caps), P3-3 (on-Pi test suite), P3-5 runbook draft.
- Start P3-6 soak in final 72h on-site (the collection-only service is what soaks; full-product soak repeats remotely after Phase 2).
- Stretch (only if weeks 1–2 ran clean): P2-3/P2-4 outbox + P2-5 Telegram so basic alerts exist before departure. **Do not trade capture/verification time for this** — it's fully remote-deliverable.
- **Leave-site gate (hard checklist):** ☐ 3 VPN paths ☐ cellular failover ☐ smart plug cycle ☐ systemd auto-start after power cut ☐ P0-11 corpus committed ☐ 72h collection soak clean ☐ runbook covers power-cycle escalation.

### Post-window (remote, unhurried) — superseded 2026-09-02

*Original bullets retained for archive; superseded by §3 Post-window
status. Current reality:*

- Phase 2 **done** (milestones closed on GitHub).
- Phase 3 remote remainder (P3-1…P3-6, plus follow-ons such as #165);
  remote P3-6 re-soak when ready.
- Phase 5 detection pulled forward in parallel (see Phase 5 pointer /
  `docs/detection-plan.md`); **not** gated on P3-6.
- Phase 4 Hardening still after P3-6 (P4-1 / P4-2 remain gated; A4
  owns vehicle-passed class before P4-2 consumes real-data tuning).

---

## 6. Sequencing / Dependency Overview

```
Phase 0:  P0-1 ─┬─▶ P0-5          P0-2 ─┐
          P0-3 ─┤                 P0-3 ─┼─▶ P0-6
          P0-4 ─┴─▶ P0-5          P0-1 ─▶ P0-7 ─▶ P0-8 ─▶ P0-10
          P0-4 ─▶ P0-4a                  P0-7 ─▶ P0-9
                                   P1-3/P1-4 ─▶ P0-11

Phase 1:  P1-1 ─▶ P1-2 ─▶ P1-3 ─▶ P1-4
          P1-5 ─▶ P1-6 ─┬─▶ P1-7 ─▶ P1-8 ─▶ P1-9
          (P1-1 feeds P1-6, P1-7)

Phase 2:  P1-7/P1-8 ─▶ P2-1 ─▶ P2-2
          P1-6 ─▶ P2-3 ─▶ P2-4          P0-2 ─▶ P2-5
          P2-1 + P2-4 + P2-5 ─▶ P2-6 ─▶ P2-7
          P2-5 ─▶ P2-8 ─▶ P2-9

Phase 3:  P1-8 + P0-9 ─▶ P3-0 ─┬─▶ P3-1 ─▶ P3-2, P3-3, P3-4 ─▶ P3-5 ─▶ P3-6 (gate)
          (P3-0 is the in-window minimal collection unit)

Phase 4:  P3-6 gates P4-1, P4-2, P4-8; rest parallel as capacity allows.

Phase 5 (detection): parallel with Phase 3 remote ops; not gated on
          P3-6 — see Phase 5 pointer / docs/detection-plan.md.
```

**Critical path through the window (ends 2026-08-30):**
P0-1 → P0-7 → P0-8/P0-10 → P0-9 → (P1-1…P1-8) → P3-0 → P0-11 → leave-site gate.
Everything Phase 2+ is off the critical path.

Parallelization notes: Epic 0.C (Pi provisioning) is independent of Epics 0.A/0.B
and should happen during the **physical-access window regardless of software
progress**. Phase 1 storage (1.B) and scanner (1.A) tracks are parallel. P2-5
(notifier) can start the moment P0-2 lands, in parallel with all of Phase 1.

---

## 7. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **BLE MAC randomization** (esp. Apple) fragments device identity | Duplicate devices, missed known-device recognition, false unknown alerts | Fingerprint fusion from day 1 (P1-7); real-data audit + `/merge` (P4-1); P0-11 ground-truth corpus grows fixtures from real captures |
| **BlueZ passive-scan quirks**: D-Bus dedup filtering, no interval/window control, silent wedges | Missed transient advertisements; silent blindness | Raw-HCI escape hatch behind `Scanner` seam (P0-3 risk doc, P4-3 spike); wedge auto-recovery ladder (P4-6) |
| **Multi-day WAN outages** (winter Starlink + cellular loss) | Lost alerts, undebuggable state | Outbox-first delivery (P2-3/4); all state local; system fully functional offline; late-but-complete summaries |
| **Connectivity**: any single WAN path down | No remote access, no alerts | **Mitigated:** DMVPN + client VPN + Tailscale are redundant access paths; Meraki cellular failover covers Starlink loss. Outbox design still required — cellular may also fail in deep winter |
| **Single-core / 512MB RAM** | OOM kills, GC pauses stalling scan windows | Lean deps, measure RSS in P1-8, `MemoryMax` + watchdog (P3-1), self-monitoring alerts (P3-4) |
| **Remote-only debugging after setup window** | A bricking bug strands the device until spring | **Substantially mitigated:** smart plug (P0-10) + 3 independent VPN paths + cellular failover (P0-8). Escalation ladder: SSH restart → power cycle → human. Residual: SD corruption so severe the Pi won't boot — mitigated by P4-5 pull-the-plug testing done *early* via P0-10/P3-0 |
| **SD-card wear / power-cut corruption** | DB loss = total memory loss | WAL + pragmas (P1-5), snapshot-based backups (P4-4), pull-the-plug testing (P4-5, early via P0-10/P3-0), bounded journald (P3-2) |
| **No RTC → wrong clock at boot** | Corrupt timelines, broken presence logic | Clock-skew handling (P4-5): monotonic ordering, defer wall-clock logic until NTP sync |
| **False positives from road traffic** | Alert fatigue → user ignores real alerts | Consecutive-window + cooldown as v1 acceptance criterion (P2-1); P0-11 labeled drive-by corpus; real-data tuning + car classification (P4-2) |
| **Detection tuning starved of real data** | Thresholds guessed, alert fatigue | **New mitigation:** P0-11 labeled corpus + P3-0 continuous collection mean P2-1 thresholds and P4-2 car tuning start from ground truth, not guesses |
| **`uv` support on chosen arch** | Broken toolchain on 32-bit | P0-1 verifies before OS choice; default recommendation is 64-bit Bookworm |
| **Chat platform outage/policy change** | Alerts undeliverable | Notifier seam + second adapter (P4-7); outbox preserves messages meanwhile |
| **Window overrun** | Physically-gated work slips past departure | Cut scope in order: (1) drop week-3 stretch goals, (2) simplify P1-7 resolver to MAC+name matching with fusion completed remotely, (3) P3-0 may even run the raw P0-3 spike loop temporarily. **Never cut:** P0-8, P0-10, P0-11, P3-0 |

---

## 8. Out of Scope / Deferred

- **Not revisiting** any locked decision (Python/uv/ruff/ty/pytest, Pydantic V2, aiosqlite, no Docker, no runtime cloud deps, MPL-2.0/DCO).
- Multiple simultaneous sites / central aggregation (schema is ready via `site_id`; deployment of a second site is post-v1).
- Web dashboard / UI of any kind (chat is the only interface in v1).
- WiFi/802.11 device detection, cameras, PIR sensors, or any non-BLE sensing (the `Scanner` seam intentionally leaves room).
- BLE *connection*/GATT interrogation of devices (passive advertisement observation only — privacy and power posture).
- Distance/trilateration, multiple-antenna positioning.
- Observation pruning/retention policies (observations kept indefinitely by decision; revisit only if disk pressure demands).
- Home Assistant / MQTT integration (plausible future notifier/consumer, not v1).
- Multi-user bot auth (single operator locked, decision #8).
- Webhook-mode Telegram (long-poll only — no inbound exposure through Meraki).
- E2EE chat platforms (Matrix/Signal) — Telegram transport security accepted (decision #4).
- Watchdog HAT (hardware watchdog add-on) — the smart plug (P0-10) is in scope; the HAT remains out.
- Encrypted DB at rest (physical theft of the Pi is out of the v1 threat model).

---

## 9. Recommended First Step

**Start P0-1 + P0-3 today.** Treat P0-1 as a **same-day decision** (64-bit
Bookworm unless the check fails) so the SD card is flashed and P0-7/P0-8/P0-10
complete **within the first 3–4 days**. P0-3 needs only the dev Mac, produces
the fixture corpus that every Phase-1 test depends on, and surfaces the
project's biggest technical unknown (passive-scan fidelity) before any
architecture is set in stone. P0-1 unblocks the Pi provisioning track
(P0-7→P0-9). The window clock is running (ends **2026-08-30**); hardware
access items front-load, software follows. First commit to
`unclespeedo/blesentry` is P0-4 — which now ships the LICENSE (MPL-2.0) on
day one, exactly when it costs nothing and preserves the most optionality.

---

## Appendix: Issue Map

Refreshed 2026-09-02 against GitHub issue/milestone state (alignment pass).
Original 46 issues seeded 2026-08-15; Phase 5 (F/A/C/I/L) added later,
plus selected follow-ons (#165, #174). Project-board columns may lag —
trust issue `state` + milestone here. Phase milestones may also carry
other closed follow-ons not listed (e.g. Phase 2 #58/#148/#151).

| ID | Issue | Milestone | Status |
|---|---|---|---|
| P0-1 | #1 | Phase 0 — Provisioning & Gates | Closed |
| P0-2 | #2 | Phase 0 — Provisioning & Gates | Closed |
| P0-3 | #3 | Phase 0 — Provisioning & Gates | Closed |
| P0-4 | #4 | Phase 0 — Provisioning & Gates | Closed |
| P0-4a | #7 | Phase 0 — Provisioning & Gates | Closed |
| P0-5 | #5 | Phase 0 — Provisioning & Gates | Closed |
| P0-6 | #6 | Phase 0 — Provisioning & Gates | Closed |
| P0-7 | #8 | Leave-Site Gate | Closed |
| P0-8 | #9 | Leave-Site Gate | Closed |
| P0-9 | #10 | Leave-Site Gate | Closed |
| P0-10 | #11 | Leave-Site Gate | Closed |
| P0-11 | #16 | Leave-Site Gate | Closed |
| P1-1 | #12 | Phase 1 — Core Skeleton | Closed |
| P1-2 | #13 | Phase 1 — Core Skeleton | Closed |
| P1-3 | #14 | Phase 1 — Core Skeleton | Closed |
| P1-4 | #15 | Phase 1 — Core Skeleton | Closed |
| P1-5 | #17 | Phase 1 — Core Skeleton | Closed |
| P1-6 | #18 | Phase 1 — Core Skeleton | Closed |
| P1-7 | #19 | Phase 1 — Core Skeleton | Closed |
| P1-8 | #20 | Phase 1 — Core Skeleton | Closed |
| P1-9 | #21 | Phase 1 — Core Skeleton | Closed |
| P2-1 | #22 | Phase 2 — Presence/Alerts/Bot | Closed |
| P2-2 | #23 | Phase 2 — Presence/Alerts/Bot | Closed |
| P2-3 | #24 | Phase 2 — Presence/Alerts/Bot | Closed |
| P2-4 | #26 | Phase 2 — Presence/Alerts/Bot | Closed |
| P2-5 | #25 | Phase 2 — Presence/Alerts/Bot | Closed |
| P2-6 | #27 | Phase 2 — Presence/Alerts/Bot | Closed |
| P2-7 | #28 | Phase 2 — Presence/Alerts/Bot | Closed |
| P2-8 | #29 | Phase 2 — Presence/Alerts/Bot | Closed |
| P2-9 | #30 | Phase 2 — Presence/Alerts/Bot | Closed |
| P3-0 | #31 | Leave-Site Gate | Closed |
| P3-1 | #32 | Phase 3 — Pi Deployment | Open |
| P3-2 | #33 | Phase 3 — Pi Deployment | Open |
| P3-3 | #34 | Phase 3 — Pi Deployment | Open |
| P3-4 | #35 | Phase 3 — Pi Deployment | Open |
| P3-5 | #36 | Phase 3 — Pi Deployment | Open |
| P3-6 | #37 | Phase 3 — Pi Deployment | Open |
| P3-journal | #165 | Phase 3 — Pi Deployment | Open |
| P4-1 | #38 | Phase 4 — Hardening | Open |
| P4-2 | #39 | Phase 4 — Hardening | Open |
| P4-3 | #40 | Phase 4 — Hardening | Open |
| P4-4 | #41 | Phase 4 — Hardening | Open |
| P4-5 | #42 | Phase 4 — Hardening | Open |
| P4-6 | #43 | Phase 4 — Hardening | Open |
| P4-7 | #44 | Phase 4 — Hardening | Open |
| P4-8 | #45 | Phase 4 — Hardening | Open |
| P4-9 | #46 | Phase 4 — Hardening | Open |
| F1 | #120 | M1 — Detection foundation & eval harness | Closed |
| F2 | #121 | M1 — Detection foundation & eval harness | Closed |
| F3 | #122 | M1 — Detection foundation & eval harness | Closed |
| F4 | #123 | M1 — Detection foundation & eval harness | Open |
| F5 | #124 | M1 — Detection foundation & eval harness | Open |
| F6 | #125 | M1 — Detection foundation & eval harness | Closed |
| A1 | #126 | M2 — Approach detector | Closed |
| A2 | #127 | M2 — Approach detector | Closed |
| A2-followup | #174 | M2 — Approach detector | Closed |
| A3 | #128 | M2 — Approach detector | Closed |
| A4 | #129 | M2 — Approach detector | Open |
| A5 | #130 | M2 — Approach detector | Open |
| C1 | #131 | M3 — Crowd anomaly detector | Closed |
| C2 | #132 | M3 — Crowd anomaly detector | Closed |
| C3 | #133 | M3 — Crowd anomaly detector | Closed |
| C4 | #134 | M3 — Crowd anomaly detector | Closed |
| C5 | #135 | M3 — Crowd anomaly detector | Open |
| I1 | #136 | M4 — Inside presence detector | Closed |
| I2 | #137 | M4 — Inside presence detector | Closed |
| I3 | #138 | M4 — Inside presence detector | Closed |
| I4 | #139 | M4 — Inside presence detector | Closed |
| L1 | #140 | M5 — Learning loop & bake-off | Open |
| L2 | #141 | M5 — Learning loop & bake-off | Open |
| L3 | #142 | M5 — Learning loop & bake-off | Open |
| L4 | #143 | M5 — Learning loop & bake-off | Open |
| L5 | #144 | M5 — Learning loop & bake-off | Open |
