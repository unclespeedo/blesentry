# Detection plan — adaptive alerting (approach / crowd / inside)

**Status: proposal (pre-issue).** Draft plan for the next detection epic,
to be split into the milestones and issues below. Motivated by a review of
real continuous-collection logs; all figures here are anonymized aggregates
(see *Data & anonymization*).

## Why

The shipped alert primitive is **per-identity**: an unlabeled device that
reaches PRESENT (`appear_windows` consecutive windows at or above the RSSI
gate) alerts. Against a real site's logs this fatigues for two independent
reasons, only one of which threshold tuning can touch:

1. **Identity fragmentation.** BLE addresses rotate (RPA). The fusion
   resolver (P1-7) joins only within a temporally-local window and is
   conservative by design, so a lingering phone throws off many
   short-lived identities — each unlabeled, each a candidate alert. One
   device becomes many events.
2. **Density + re-nag.** Even with perfect fusion, every unlabeled device
   that lingers alerts, and a device that leaves and returns re-nags each
   visit (by design). Any site with foot/road traffic is a firehose no
   gate removes.

### Evidence baseline (anonymized)

From ~8 days of continuous passive collection (~293k observations):

- ~9.7k resolved identities, but only **~38 persist longer than 30 min**;
  ~68% live 5–30 min (an RPA rotation lifetime), ~26% live under 1 min.
  The real persistent population is tiny; the rest is rotation shards and
  pass-bys.
- Address mix ≈ **73% RPA** plus non-resolvable and a handful of stable
  fixtures. Stable-address fixtures still fragment into multiple identity
  rows across restarts/payload changes — a resolver-dedup defect
  (feeds P4-1), independent of this plan.
- Per-window **near count (RSSI ≥ −70): mean ≈ 4**, smooth and unimodal;
  above 12 in only ~3% of windows — a baselineable signal.
- Per-window **in-room count (RSSI ≥ −55): floor ≈ 0.06** (essentially
  zero) with rare episodes — a near-ideal sentinel signal for an
  unoccupied site.
- A worked **approach** example: a *novel* RPA address climbed
  **≈ −99 → −72 dBm over ~2 min, then faded** — a walk-by. It never
  crossed −55 and never produced 3 consecutive above-−80 windows, so
  **both** a tighter gate *and* the shipped consecutive-window debounce
  miss it. The signal was real; only a trajectory-aware detector catches
  it.

### The thesis

Stop deciding alerts on *who* a device is and start deciding on *what
changed* in the RF environment. That is rotation-proof (no identity
needed) and splits the observation net (keep it wide, lose nothing far
away) from the alert decision (an anomaly/approach layer over band
features). Concretely, three cheap detectors — each catching a case the
others miss — plus a shared harness so we can measure all three against
the same real logs and let the data pick the default stack.

## Design at a glance

| Detector | Question it answers | Core method | Case from the logs |
|---|---|---|---|
| **Approach** | "Something unfamiliar is coming closer" | novelty + rising `max_rssi` trend, pre-fusion, no hard gate | the ~−99→−72 walk-by |
| **Crowd** | "The site got unusually busy" | robust per-time-of-day baseline (median/MAD) + CUSUM on band counts | busy vs quiet days |
| **Inside** | "Someone is *in* the space" | sustained in-room (≥ −55) count, own-gear excluded | in-room floor ≈ 0 |

The observation gate stays wide (−80 or wider); RSSI bands become
*features*, never a wall. A far approach is caught by the far band rising
above its baseline and by the max-RSSI trend climbing, before anything
crosses −55.

## Milestones and issues

Issue IDs below are provisional (`F/A/C/I/L`), to be assigned real numbers
when filed under `epic:detection`. Sizes S/M/L follow ROADMAP convention.

### M1 — Detection R&D foundation & eval harness *(shared enabler)*

Make every detector measurable against the same real logs, offline and
deterministic — the prerequisite for "try everything and compare."

- **F1 · Offline replay harness.** Feed a historical `observations` DB or
  fixture corpus through any Detector; emit would-be alerts with
  timestamps — clock-free, deterministic, no live radio.
  **DoD:** replays N days reproducibly; golden-file test. **Deps:** —
  **Size:** M **Labels:** `epic:detection type:feature priority:p1`
- **F2 · Detector seam (protocol).** One interface all three implement
  (per-window `heard` + observation stream → `DetectionEvent`), mirroring
  the Scanner/Notifier seams (ADR-0002).
  **DoD:** protocol + null/mock impls; seam documented. **Deps:** —
  **Size:** S **Labels:** `epic:detection type:feature priority:p1`
- **F3 · Feature extractor.** Per-window aggregate features (counts by
  RSSI band, `max_rssi`, churn/novelty) and per-address trajectory
  features (rolling RSSI slope, dwell, first-seen/familiarity). Shared by
  M2 (trajectory) and M3/M4 (aggregate).
  **DoD:** unit-tested feature vectors. **Deps:** F2 **Size:** M
  **Labels:** `epic:detection type:feature priority:p1`
- **F4 · Labeled event corpus + schema.** Ground-truth episodes (type,
  start/end, cause) as sanitized fixtures. Seed with the anonymized
  walk-by (positive) plus an own-fixture in-room blip and a rotation
  burst (benign).
  **DoD:** corpus format + ≥1 positive + ≥2 benign committed per
  `tests/fixtures/README.md`. **Deps:** — **Size:** S
  **Labels:** `epic:detection type:test priority:p1`
- **F5 · Evaluation report.** Precision/recall, alerts-per-day, detection
  lead-time vs the corpus — one command, comparable across detectors.
  **DoD:** metrics table; consumed by the bake-off. **Deps:** F1, F4
  **Size:** M **Labels:** `epic:detection type:feature priority:p1`
- **F6 · Familiar/resident baseline.** Auto-learn devices (fingerprint
  classes) seen across ≥K days; expose `is_familiar` — the allow-list
  every detector subtracts.
  **DoD:** familiar-set built from history; stable fixtures classified
  familiar; tested. **Deps:** F3 **Size:** M
  **Labels:** `epic:detection type:feature priority:p1`

### M2 — Approach detector *(novelty + rising RSSI)*

Ping "unfamiliar device approaching" from a rising-RSSI trajectory of a
novel address — pre-fusion, no hard gate, no consecutive-window debounce.

- **A1 · Spec / ADR-0005.** Trigger = novel AND not-familiar AND rising
  `max_rssi` slope over W windows; document why the gate and debounce
  miss it (evidence baseline).
  **DoD:** ADR committed. **Deps:** F2 **Size:** S
  **Labels:** `epic:detection type:docs priority:p1`
- **A2 · Per-address trajectory tracker.** Bounded rolling per-address
  RSSI window → slope, dwell, novelty; RAM-bounded for the Pi target.
  **DoD:** unit tests incl. rise/fall; memory bound asserted. **Deps:** F3
  **Size:** M **Labels:** `epic:detection type:feature priority:p1`
- **A3 · Approach trigger + alert.** Emit an "approaching" event (current
  RSSI + trend + rough distance) to the outbox.
  **DoD:** replay flags the labeled approach near its peak; alert text
  snapshot-tested. **Deps:** A2, F1 **Size:** M
  **Labels:** `epic:detection type:feature priority:p1`
- **A4 · Walk vs drive-by discrimination.** Dwell/slope heuristic (walk ≈
  minutes, vehicle ≈ seconds; optional mfr-data signature); low-priority
  "vehicle passed" class. Converges with P4-2.
  **DoD:** labeled walk → "person"; synthetic fast pass suppressed or
  classified vehicle. **Deps:** A3 **Size:** M
  **Labels:** `epic:detection type:feature priority:p2`
- **A5 · Replay validation + tuning.** Flags the walk-by while quiet on
  own-gear and the rotation cloud on quiet days; tune slope/window.
  **DoD:** recall = 1 on approach positives, ≤ Y false pings/day on quiet
  days (harness-reproduced). **Deps:** A3, F5 **Size:** M
  **Labels:** `epic:detection type:test priority:p1`

### M3 — Crowd anomaly detector *(aggregate baseline + CUSUM)*

Alert on a population deviation from a robust time-of-day baseline —
identity-free, rotation-proof.

- **C1 · Spec / ADR.** Features (near count, all count), robust per
  hour-of-week median/MAD baseline, EWMA adaptation, CUSUM for sustained
  shift; cold-start and no-RTC/clock guards.
  **DoD:** ADR. **Deps:** F2 **Size:** S
  **Labels:** `epic:detection type:docs priority:p1`
- **C2 · Rolling aggregate persistence.** Small per-window band-count
  table for baseline + replay; bounded retention.
  **DoD:** migration + writer; retention tested. **Deps:** F3 **Size:** S
  **Labels:** `epic:detection type:feature priority:p1`
- **C3 · Robust baseline model.** Per-bucket median/MAD, EWMA-adaptive —
  robust to outlier days (e.g. an install day) by construction.
  **DoD:** baseline stable under an injected outlier; tests. **Deps:** C2
  **Size:** M **Labels:** `epic:detection type:feature priority:p1`
- **C4 · CUSUM detector + alert-with-roster.** Accumulate signed
  deviation; fire one coalesced alert listing contributors.
  **DoD:** replay catches a busy episode, ignores single-window blips.
  **Deps:** C3 **Size:** M **Labels:** `epic:detection type:feature priority:p1`
- **C5 · Replay validation.** Busy vs quiet on the corpus; confirm an
  outlier day is excluded once feedback-labeled (M5).
  **DoD:** metrics logged; alerts/day within target. **Deps:** C4, F5
  **Size:** M **Labels:** `epic:detection type:test priority:p1`

### M4 — Inside presence detector *(in-room CUSUM)*

High-precision alert on sustained in-room (≥ −55) presence, with own gear
excluded.

- **I1 · Spec.** In-room count signal + familiar/own-gear exclusion +
  sustained-presence rule. The ≈0 floor makes this high-precision, but
  fixtures that occasionally touch −55 must be removed.
  **DoD:** spec. **Deps:** F2 **Size:** S
  **Labels:** `epic:detection type:docs priority:p1`
- **I2 · Own-gear exclusion.** Subtract familiar/fixed devices (incl.
  stable-address fixtures that hit −55) before counting.
  **DoD:** a sanitized own-fixture in-room blip does not trigger; test.
  **Deps:** F6 **Size:** S **Labels:** `epic:detection type:feature priority:p1`
- **I3 · Sustained in-room trigger + alert.**
  **DoD:** replay fires on an inside-dwell, silent on transient close
  passes. **Deps:** I2 **Size:** M
  **Labels:** `epic:detection type:feature priority:p1`
- **I4 · Replay validation + tuning** (N devices / M windows).
  **DoD:** metrics vs corpus. **Deps:** I3, F5 **Size:** S
  **Labels:** `epic:detection type:test priority:p1`

### M5 — Learning loop & bake-off *(feedback → compare → decide)*

Close the human-in-the-loop learning loop, then let the data pick the
default stack.

- **L1 · Episode-label commands.** `/confirm`, `/expected <reason>`,
  `/false-alarm <reason>` → store `(features, label)` as ground truth
  (label_audit-style provenance).
  **DoD:** round-trip; episode persisted. **Deps:** F4 **Size:** M
  **Labels:** `epic:detection epic:bot type:feature priority:p1`
- **L2 · Baseline feedback-gating.** Labeled-benign episodes excluded from
  baselines (e.g. an install day); labeled positives added to the corpus.
  **DoD:** labeling benign removes it from baseline; tested. **Deps:** L1,
  C3 **Size:** M **Labels:** `epic:detection type:feature priority:p1`
- **L3 · Interpretable supervised gate (optional).** Logistic or
  decision-stump on features once ≥K labels — explainable and Pi-cheap;
  gates alerts to raise precision.
  **DoD:** improves precision on held-out labels with no new misses;
  decision auditable. **Deps:** L1, F5 **Size:** L
  **Labels:** `epic:detection type:feature priority:p2`
- **L4 · Rotation-false-alarm → fusion feedback.** Mine
  `/false-alarm rotation` to tune fusion weights or feed `/merge` (P4-1).
  **DoD:** flagged under-joins surfaced. **Deps:** L1 **Size:** M
  **Labels:** `epic:detection epic:fingerprint type:feature priority:p2`
- **L5 · Bake-off & decision.** Run A/B/C (and combinations) on the corpus
  via F5; comparison report (precision, recall, alerts/day, lead-time);
  pick the default detector stack and a site config profile; record the
  decision.
  **DoD:** comparison report committed; default stack + config chosen
  (in-issue decision). **Deps:** A5, C5, I4 **Size:** M
  **Labels:** `epic:detection type:docs priority:p1`

## Sequencing

Critical path: **M1 → (M2 ∥ M3 ∥ M4) → M5.** The three detectors are fully
parallel once the foundation lands — which is the point: build once, then
try everything on shared rails. Recommended first slice: F1 + F2 + F3, then
M2 (the approach detector — the case the logs prove nothing existing
catches).

## Relationship to the existing roadmap

- **P2-1 (presence state machine)** stays. Its consecutive-window debounce
  is still the right tool for the transient-passer-by case; these
  detectors are additional layers into the same outbox, not a
  replacement.
- **P4-1 (MAC-randomization edge cases / `/merge`)** — the stable-address
  fragmentation noted above is P4-1's territory; L4 feeds it. The
  aggregate detectors are robust to it regardless (counts absorb
  duplication).
- **P4-2 (car / pass-by tuning)** — A4 (walk vs drive-by) converges with
  P4-2; share the drive-by corpus and the "vehicle passed" event class.

## Data & anonymization

This document commits to a public repo. Per AGENTS.md and
`tests/fixtures/README.md`, only aggregate, non-identifying figures appear
here: counts, durations, RSSI values, and distribution shapes — never a
site id, address, device name/inventory, IP/host, or timestamped
movement. The labeled corpus (F4) is a sanitized derivative; raw captures
never leave the capture machine.
