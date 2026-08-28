# Detection plan — adaptive alerting (approach / crowd / inside)

**Status: proposal (pre-issue).** Draft plan for the next detection epic, to
be split into the milestones and issues below. Motivated by a review of real
continuous-collection logs; all figures here are anonymized aggregates (see
*Data & anonymization*). Technical constraints surfaced in review are
captured once in *Cross-cutting design constraints* (DC-1…DC-9) and
referenced by issue.

## Why

The shipped alert primitive is **per-identity**: an unlabeled device that
reaches PRESENT (`appear_windows` consecutive windows at or above the RSSI
gate) alerts. Against real logs this fatigues for two independent reasons,
only one of which threshold tuning can touch:

1. **Identity fragmentation.** BLE addresses rotate (RPA). The fusion
   resolver (P1-7) joins only within a temporally-local window and is
   conservative by design, so a lingering phone throws off many short-lived
   identities — each unlabeled, each a candidate alert. One device becomes
   many events.
2. **Density + re-nag.** Even with perfect fusion, every unlabeled device
   that lingers alerts, and a device that leaves and returns re-nags each
   visit (by design). Any site with foot/road traffic is a firehose no gate
   removes.

### Evidence baseline (anonymized)

From ~8 days of continuous passive collection (~293k observations):

- ~9.7k resolved identities, but only **a few dozen** persist longer than
  30 min; ~⅔ live 5–30 min (an RPA rotation lifetime), ~¼ live under 1 min.
  The persistent population is small; the rest is rotation shards and
  pass-bys.
- Address mix ≈ **73% RPA** plus non-resolvable and a handful of stable
  fixtures. Stable-address fixtures still fragment into multiple identity
  rows across restarts/payload changes — a resolver-dedup defect (feeds
  P4-1), independent of this plan.
- Per-window **near count (RSSI ≥ −70): mean ≈ 4**, smooth and unimodal;
  above 12 in only ~3% of windows — a baselineable signal.
- In the target regime (a low-traffic site) the **adjacent-to-Pi band
  (RSSI ≥ −55) is normally near-empty**, so a *sustained* device there is a
  high-SNR signal. This is the design assumption the Inside detector
  exploits, not a measured property of any specific deployment.
- A worked **approach** example (anonymized, untimed): a rotating address
  climbed **≈ −99 → −72 dBm over ~2 min, then faded** — a walk-by. It never
  crossed −55, and did not produce 3 consecutive above-−80 windows, so it
  fell below the *alerting* gate and debounce. Part of that is genuine
  capture loss (BlueZ duplicate-filtering drops windows, `docs/risks.md`),
  which the approach detector is *more* exposed to, not less (DC-7) — a
  caveat, not a free win.

### The thesis

Stop deciding alerts on *who* a device is and start deciding on *what
changed* in the RF environment. That is rotation-proof (no identity needed)
and splits the observation net (keep it wide, lose nothing far away) from
the alert decision (an anomaly/trend layer over band features). Concretely,
three cheap detectors — each catching a case the others miss — plus a shared
harness so all three can be measured against the same real logs and the data
picks the default stack.

## Threat model & limits

State the ceiling honestly; these detectors are a *smoke alarm, not a
tripwire*:

- They detect BLE devices that **advertise**. An intruder who disables
  Bluetooth, uses airplane mode, or leaves a phone in a vehicle emits
  nothing and the RF environment does not change — a hard recall ceiling,
  trivially defeated by an aware adversary.
- The Inside alert therefore means *"a BLE device is sustained
  adjacent-to-Pi,"* **not** *"a person is inside."* An idle screen-locked
  phone also advertises sparsely; sustained detection is not guaranteed.
- No positioning/distance (out of ROADMAP scope): alerts report coarse
  proximity bands (far / near / very-close), never a metre figure (DC-6).
- RSSI is noisy and per-install; band edges are not portable across sites
  without calibration (DC-6).
- These layers **complement** the presence machine (P2-1), which still
  handles the transient-passer-by case; they are additional detectors into
  the same outbox, not a replacement.

## Design at a glance

| Detector | Question | Core method | Case from the logs |
|---|---|---|---|
| **Approach** | "Something unfamiliar is coming closer" | large rising **per-address** RSSI span above the noise band, ending close; pre-fusion (DC-7) | the ≈ −99→−72 walk-by |
| **Crowd** | "The site got unusually busy" | robust baseline (scale-floored MAD / count model) + CUSUM on band counts (DC-3, DC-4) | busy vs quiet days |
| **Inside** | "A BLE device is sustained adjacent-to-Pi" | sustained adjacent-to-Pi (≥ −55) count, own-gear excluded (DC-6) | near-empty target regime |

The observation gate stays wide (−80 or wider); RSSI bands are *features*,
never a wall. A far approach is caught by its band rising above baseline and
by the per-address trend climbing, before anything crosses −55.

## Cross-cutting design constraints (DC)

These emerged from review; every issue that touches them must honor them, and
the spec issues (A1/C1/I1) must resolve the open numbers.

- **DC-1 · Seam & hot path.** Detectors implement the F2 Detector seam, run
  *inside* `run_cycle` fed the in-memory advertisement/`heard` batch (no
  per-cycle `observations` reads in the scan path), and any alert enqueues
  *within the existing cycle transaction* (single writer — mirror
  `alerter.handle` in `loop.py`). Config-selected via a `[detection]` section
  with lazy-imported backends (ADR-0002).
- **DC-2 · Bounded memory.** Any per-address structure has a hard cap **and**
  an eviction policy (evict-on-fade after N missed windows / LRU, mirroring
  `presence._should_prune`) and fixed-size rolling buffers (bounded `deque`,
  not a growing list). The persistent familiar set (F6, a few dozen devices)
  is a separate bounded structure from the RPA cloud. Prefer reused bounded
  structures to hold GC churn flat on the single core.
- **DC-3 · Robust stats on low counts.** Count series are near-zero and
  skewed; MAD degenerates to 0 at the floor. Floor the scale (e.g.
  `max(MAD, k)`) or use a count-appropriate (Poisson/negative-binomial)
  deviation — never a bare Gaussian z on MAD — and set thresholds by a
  *validated empirical* per-bucket false-alarm rate, not an assumed sigma.
- **DC-4 · Clockless operation.** The Pi has no RTC and runs days without NTP
  (offline-first). Seasonal (hour-of-week) baselines timestamp with monotonic
  offsets and **hold-and-backfill** (don't commit a bucket until wall-clock
  is NTP-confirmed); fall back to a rolling/relative baseline when wall-clock
  is unreliable. Acknowledge the multi-week cold start (168 buckets fill once
  per week). EWMA horizon ≫ the longest legitimate episode, and the baseline
  is frozen within an episode (else a sustained crowd is absorbed into
  "normal" before CUSUM accumulates).
- **DC-5 · Scan health.** A BlueZ silent wedge collapses all counts to 0 —
  that is a *health* anomaly (P4-6), not "all clear." Detectors consume
  scan-liveness and suppress/flag output on degraded health; an abrupt
  population collapse is treated as health, not silence. Define "heard this
  window" precisely under duplicate-filtering; make slope/CUSUM tolerant of
  irregular sampling.
- **DC-6 · RSSI realism.** RSSI is ±several dB (multipath/body-shadow) and
  per-install (adapter/antenna/placement). Band edges (−55/−70/−80) need a
  per-install calibration step; F6 should also learn the RSSI distribution so
  gates self-scale (supports P4-8). Report coarse bands, never a distance.
  −55 is relabelled **adjacent-to-Pi** (~1–2 m per `docs/tuning.md`), not
  "in-room"; accept the recall trade (someone in the room but 4–5 m away sits
  at −70…−75 and is not "adjacent").
- **DC-7 · Trajectory feasibility.** `resolver.py` measures a ~0.3 min median
  rotated-row lifetime, so the *median* raw address yields ~1 window and
  cannot support a W-window slope. The approach detector is structurally
  limited to addresses persisting ≥ W windows (bounded above by the ~15 min
  RPA cap ≈ 60 windows); A1 must quantify the fraction of real approaches
  this covers and accept blindness to sub-W approaches. The trigger is
  **magnitude-based** (see A1), not novelty-based.
- **DC-8 · Lean deps.** Any learned model (L3) is pure-Python or numpy-only
  (no sklearn/scipy) and trains *offline on label-change*, never in the scan
  cycle (ADR-0002 lazy-import posture; 512 MB target).
- **DC-9 · Offline replay.** F1 runs off-device, read-only, against an
  immutable snapshot or sanitized fixture — never the live WAL DB in place
  (a long reader pins WAL checkpointing and the run isn't reproducible).

## Precondition

This epic assumes **Phase 2 complete**: the outbox (P2-3/P2-4) for alert
enqueue and the notifier + command router (P2-5/P2-8) for the feedback
commands. Alert-emitting issues (A3/C4/I3) enqueue via the outbox per DC-1;
L1 extends the P2-8 command router.

## Milestones and issues

Provisional IDs (`F/A/C/I/L`) are kept as a title prefix when filed (e.g.
"F1 · Offline replay harness") and mapped to real numbers in a table in this
doc on filing, so `Deps:` stay resolvable (see *Provisional IDs & filing*).
All issues carry `phase:5` (new — "Adaptive Detection") in addition to the
labels shown. Where an acceptance number is a placeholder (W, Y, K, N, M,
target), it is **set by the linked spec issue and recorded there before the
validation issue becomes `agent:eligible`** — so no issue lands with an
ambiguous DoD.

### M1 — Detection R&D foundation & eval harness *(shared enabler)*

- **F1 · Offline replay harness.** Feed a historical `observations` snapshot
  or sanitized fixture through any Detector; emit would-be alerts with
  timestamps — clock-free, deterministic, off-device, read-only (DC-9).
  **DoD:** replays N days reproducibly; golden-file test. **Deps:** F2
  **Size:** M **Labels:** `epic:detection type:feature priority:p1`
  **Shipped:** `docs/replay.md`, `blesentry replay`,
  `blesentry.detection.replay` (#120). Snapshot path feeds `heard`
  only; fixture path feeds `advertisements` only.
- **F2 · Detector seam (protocol) + ADR.** One interface all three implement
  (per-window `heard` + observation stream → `DetectionEvent`), selected via
  a `[detection]` config section with lazy-imported backends (DC-1). Because
  this adds an architectural seam, it extends ADR-0002 (or a dedicated ADR),
  not a bare feature. **DoD:** protocol + null/mock impls; ADR committed.
  **Deps:** — **Size:** S **Labels:** `epic:detection type:docs priority:p1`
- **F3 · Feature extractor (offline batch).** Per-window aggregate features
  (counts by band, per-address `max_rssi`, churn) and per-address trajectory
  features (rolling RSSI slope, dwell, first-seen/familiarity) as the
  canonical definitions the eval harness uses. A2 (online tracker) reuses
  these definitions; this issue owns them (DC-2 applies to A2's live form).
  **DoD:** unit-tested feature vectors. **Deps:** F2 **Size:** M
  **Labels:** `epic:detection type:feature priority:p1`
  **Shipped:** `docs/features.md`, `blesentry.detection.features` (#122).
  Inclusive band counts (−55/−70/−80); OLS slope over last W heard
  samples; familiarity proxy is `duty` (`windows_seen / age_windows`;
  F6 owns `is_familiar`).
- **F4 · Labeled event corpus + schema.** Ground-truth episodes (type,
  start/end, cause) as sanitized fixtures. **Gating:** if the walk-by /
  adjacent-to-Pi / rotation episodes already exist as committed sanitized
  fixtures, this is an agent-doable relabel; if a fresh capture is needed it
  is a human on-site step and the issue is `needs:hardware` (AGENTS.md Data &
  fixtures). **DoD:** corpus format + ≥1 positive + ≥2 benign committed per
  `tests/fixtures/README.md`. **Deps:** — **Size:** S
  **Labels:** `epic:detection type:test priority:p1`
- **F5 · Evaluation report.** Precision/recall, alerts-per-day, detection
  lead-time vs the corpus — one command, comparable across detectors.
  **DoD:** metrics table; consumed by the bake-off. **Deps:** F1, F4
  **Size:** M **Labels:** `epic:detection type:feature priority:p1`
- **F6 · Familiar/resident baseline.** Auto-learn devices (fingerprint
  classes) seen across ≥K days; expose `is_familiar` — the allow-list every
  detector subtracts. Built at startup/periodically, not per-cycle (DC-1);
  bounded, separate from the RPA cloud (DC-2); may also learn the per-install
  RSSI distribution (DC-6). Must handle own *rotating*-address gear, not just
  stable fixtures (resolver under-join is a live FP source — see I2).
  **DoD:** familiar-set built from history; stable fixtures classified
  familiar; K pinned. **Deps:** F3 **Size:** M
  **Labels:** `epic:detection type:feature priority:p1`

### M2 — Approach detector *(magnitude-based rising trajectory)*

- **A1 · Spec / ADR.** Trigger = a **large, mostly-monotonic RSSI span**
  (Δ ≥ ~15–20 dB, above the noise band) over W windows **AND** a minimum
  peak/terminal RSSI (a genuine approach ends close; a stationary rotation
  shard stays in a narrow band around its path-loss level). Novelty /
  unfamiliarity are at most **weak features, not gates** (novelty-by-address
  is ~always true under RPA — DC-7). Define W, Δ, the peak floor, and the
  covered fraction of real approaches (DC-7); relabel −55 adjacent-to-Pi
  (DC-6). The trigger needs the full span to accumulate, so the alert fires
  once the device is already close — inherently late lead time, which the
  coverage bound must state honestly. **DoD:** ADR committed with
  W/Δ/peak/coverage bound. **Deps:** F2
  **Size:** S **Labels:** `epic:detection type:docs priority:p1`
  **Shipped:** ADR-0007 (Accepted), `docs/approach.md`,
  `blesentry.detection.approach.is_rising_approach` (#126).
  Pinned: W = 8 heard samples, Δ = 18 dB, peak/terminal = −75 dBm,
  far start = −85 dBm, kind `approaching`, FAR ≤ 1/day on benign
  corpus. Coverage class = last-W tail matching the predicate
  (span ≥ 18, min ≤ −85, peak **and** terminal ≥ −75, slope > 0,
  net rise ≥ ½ span); sub-W / drive-by / already-near / aged-out far
  samples / rotation / short span are accepted misses. −55 remains
  adjacent-to-Pi, not the peak floor. Not a `[detection]` backend
  (A3). Gates are last-W only (visit-min is A2).
- **A2 · Per-address trajectory tracker (online).** Bounded rolling
  per-address RSSI (fixed-size `deque`) → span, slope, dwell; hard address
  cap + evict-on-fade (DC-2), reusing F3 feature definitions.
  **DoD:** unit tests incl. rise/fall; stated numeric memory cap asserted
  under RPA churn. **Deps:** F3, A1 **Size:** M
  **Labels:** `epic:detection type:feature priority:p1`
  **Shipped:** `docs/approach.md` (A2 section),
  `blesentry.detection.trajectory` (#127). Deque maxlen = A1 W (8);
  cap **256** addresses / **2048** samples; fade after **12** missed
  indexes; `visit_min` is metadata, not an A1 gate. A3 wraps this;
  A2 is not itself a `[detection]` backend.
- **A3 · Approach trigger + alert.** Emit an "approaching" event (current
  RSSI + trend + coarse proximity band, never a distance — DC-6) inside the
  cycle transaction (DC-1). **DoD:** replay flags the labeled approach near
  its peak; alert text snapshot-tested. **Deps:** A2, A1, F1 **Size:** M
  **Labels:** `epic:detection type:feature priority:p1`
  **Shipped:** `docs/approach.md` (A3 section),
  `blesentry.detection.approach_detector.ApproachDetector` (#128).
  `[detection] backend = "approach"`; fire-once per visit; alert
  text uses F3 `proximity_band` of the terminal RSSI. Replay:
  `tests/fixtures/replay/walkby.json` + golden. Default remains
  `none`.
- **A4 · Walk vs drive-by discrimination + "vehicle passed" class.** Dwell/
  slope heuristic (walk ≈ minutes, vehicle ≈ seconds); a slow or idling
  vehicle dwells like a walk, so lean on the mfr-data signature
  (CarPlay/TPMS), not dwell alone, to separate them; tolerant of dedup gaps
  (DC-5). **This issue owns** the
  low-priority "vehicle passed" event class; P4-2 consumes it.
  **DoD:** labeled walk → "person"; synthetic fast pass classified vehicle.
  **Deps:** A3 **Size:** M **Labels:** `epic:detection type:feature priority:p2`
- **A5 · Replay validation + tuning.** Flags the walk-by while quiet on
  own-gear and the rotation cloud on quiet days.
  **DoD:** recall = 1 on approach positives, false pings/day ≤ the target
  **set in A1**, harness-reproduced. **Deps:** A3, A1, F5 **Size:** M
  **Labels:** `epic:detection type:test priority:p1`

### M3 — Crowd anomaly detector *(robust baseline + CUSUM)*

- **C1 · Spec / ADR.** Features (near count, all count), a robust baseline
  with a **floored scale or count model** (DC-3), EWMA adaptation with a
  horizon ≫ episode length and in-episode freeze (DC-4), CUSUM for sustained
  shift; clockless hold-and-backfill + rolling fallback + cold-start
  acknowledgement (DC-4); scan-health suppression (DC-5). Define the
  per-bucket false-alarm target. **DoD:** ADR with the FAR target + scale/
  model choice. **Deps:** F2 **Size:** S
  **Labels:** `epic:detection type:docs priority:p1`
  **Shipped:** ADR-0008 (Proposed), `docs/crowd.md`,
  `blesentry.detection.crowd` (#131). Pinned: source `heard`;
  primary `count_near`; floored MAD floor **1.5**; CUSUM **k=0.5**,
  **h=5.0**; EWMA span **56**; seasonal **168** buckets; rolling
  **40320** windows; cold start **168 h**; kind `crowd-busy`, FAR
  ≤ 1/day on benign corpus. Not a `[detection]` backend (C4).
- **C2 · Rolling aggregate persistence.** One band-count row per cycle,
  written **inside the existing cycle transaction on the shared connection**
  (DC-1) — a replay/baseline cache derivable from `observations`, not a new
  source of truth. Retention by incremental time-window `DELETE` (once/day),
  **no `VACUUM`** (SD wear). **DoD:** migration + writer share the cycle txn;
  retention tested. **Deps:** F3 **Size:** S
  **Labels:** `epic:detection type:feature priority:p1`
- **C3 · Robust baseline model.** Per-bucket floored-MAD / count-model,
  EWMA-adaptive, with a **capped per-bucket sample window** (or streaming
  approximate scale) so steady-state RSS is fixed (DC-2, DC-3); robust to an
  outlier day by construction. **DoD:** stable under an injected outlier;
  MAD-floor / count model unit-tested at the zero floor. **Deps:** C2
  **Size:** M **Labels:** `epic:detection type:feature priority:p1`
- **C4 · CUSUM detector + alert-with-roster.** Accumulate signed deviation;
  fire one coalesced alert listing contributors, inside the cycle transaction
  (DC-1); "heard this window" defined under dedup (DC-5).
  **DoD:** replay catches a busy episode, ignores single-window blips.
  **Deps:** C3, C1 **Size:** M **Labels:** `epic:detection type:feature priority:p1`
- **C5 · Replay validation.** Busy vs quiet on the corpus; an outlier day
  excluded once feedback-labeled (M5).
  **DoD:** alerts/day ≤ the target **set in C1**; metrics logged.
  **Deps:** C4, C1, F5 **Size:** M **Labels:** `epic:detection type:test priority:p1`

### M4 — Inside presence detector *(sustained adjacent-to-Pi)*

- **I1 · Spec.** Sustained adjacent-to-Pi (≥ −55) count with own-gear
  exclusion and a sustained-presence rule (CUSUM/threshold). Frame the alert
  as *"a BLE device is sustained adjacent-to-Pi"* and state the recall
  ceiling / threat-model boundary (Threat model & limits; DC-6). Define N
  devices / M windows. **DoD:** spec with N/M + the stated boundary.
  **Deps:** F2 **Size:** S **Labels:** `epic:detection type:docs priority:p1`
- **I2 · Own-gear exclusion.** Subtract familiar/fixed devices *and own
  rotating-address gear* (the hard case — the resolver may not fuse your own
  phone across rotations, so it can sit adjacent-to-Pi and fire) before
  counting (DC-6, F6). **DoD:** a sanitized own stable-fixture *and* an own
  rotating-address device do not trigger; both tested. **Deps:** F6, I1
  **Size:** S **Labels:** `epic:detection type:feature priority:p1`
- **I3 · Sustained trigger + alert** (inside the cycle transaction, DC-1).
  **DoD:** replay fires on a sustained adjacent dwell, silent on transient
  close passes. **Deps:** I2, I1 **Size:** M
  **Labels:** `epic:detection type:feature priority:p1`
- **I4 · Replay validation + tuning.**
  **DoD:** precision/recall vs corpus meet the N/M target **set in I1**.
  **Deps:** I3, I1, F5 **Size:** S **Labels:** `epic:detection type:test priority:p1`

### M5 — Learning loop & bake-off *(feedback → compare → decide)*

- **L1 · Episode-label commands.** `/confirm`, `/expected <reason>`,
  `/false-alarm <reason>` → store `(features, label)` as ground truth
  (label_audit-style provenance). Extends the P2-8 command router.
  **DoD:** round-trip; episode persisted. **Deps:** F4, P2-8 **Size:** M
  **Labels:** `epic:detection epic:bot type:feature priority:p1`
- **L2 · Baseline feedback-gating.** Labeled-benign episodes excluded from
  baselines (e.g. an outlier day); labeled positives added to the corpus.
  **DoD:** labeling benign removes it from baseline; tested. **Deps:** L1, C3
  **Size:** M **Labels:** `epic:detection type:feature priority:p1`
- **L3 · Interpretable supervised gate (optional).** Pure-Python or
  numpy-only logistic / decision-stump (no sklearn/scipy — DC-8), trained
  offline on label-change; gates alerts to raise precision.
  **DoD:** improves precision on held-out labels with no new misses; decision
  auditable. **Deps:** L1, F5 **Size:** L
  **Labels:** `epic:detection type:feature priority:p2`
- **L4 · Rotation-false-alarm → fusion feedback.** Mine `/false-alarm
  rotation` to tune fusion weights or feed `/merge` (P4-1).
  **DoD:** flagged under-joins surfaced with an asserted count on a fixture.
  **Deps:** L1 **Size:** M
  **Labels:** `epic:detection epic:fingerprint type:feature priority:p2`
- **L5 · Bake-off & decision.** Run A/B/C (and combinations) on the corpus
  via F5; comparison report (precision, recall, alerts/day, lead-time); pick
  the default detector stack and a `[detection]` site profile; record the
  decision. **DoD:** comparison report committed; default stack + config
  chosen (in-issue decision). **Deps:** A5, C5, I4 **Size:** M
  **Labels:** `epic:detection type:docs priority:p1`

## Sequencing

M1 is the shared enabler. The three detectors then proceed in parallel; only
the **L5 bake-off gates on all three validations** (A5, C5, I4). L1/L3/L4
interleave with the detectors (L1 needs F4 + P2-8; L2 needs C3). Recommended
first slice: F2 + F1 + F3, then M2 (the case the logs prove nothing existing
catches).

## Provisional IDs & filing

Each issue keeps its letter-prefix in the title so `Deps:` remain
resolvable. Filed (GitHub milestones M1–M5 = `#7`–`#11`; label `phase:5`):

- **M1** (`#7`): F1 `#120` · F2 `#121` · F3 `#122` · F4 `#123` · F5 `#124` · F6 `#125`
- **M2** (`#8`): A1 `#126` · A2 `#127` · A3 `#128` · A4 `#129` · A5 `#130`
- **M3** (`#9`): C1 `#131` · C2 `#132` · C3 `#133` · C4 `#134` · C5 `#135`
- **M4** (`#10`): I1 `#136` · I2 `#137` · I3 `#138` · I4 `#139`
- **M5** (`#11`): L1 `#140` · L2 `#141` · L3 `#142` · L4 `#143` · L5 `#144`

The `phase:5` label was added to the taxonomy so the AGENTS.md SELECT
tie-break ("lowest phase") stays well-ordered against `phase:4`. Issues are
filed **without** `agent:eligible` — the epic is human-gated until approved.

## Relationship to the existing roadmap

- **P2-1 (presence state machine)** stays; its consecutive-window debounce is
  still right for the transient-passer-by case. These detectors are
  additional layers into the same outbox, not a replacement.
- **P4-1 (MAC-randomization / `/merge`)** — the stable-address fragmentation
  noted above is P4-1's territory; L4 feeds it. The aggregate detectors are
  robust to it regardless (counts absorb duplication).
- **P4-2 (car / pass-by tuning)** — A4 **owns** the "vehicle passed" event
  class and drive-by discriminator; P4-2 consumes it and supplies the
  drive-by corpus.

## Data & anonymization

This document commits to a public repo. Per AGENTS.md and
`tests/fixtures/README.md`, only aggregate, non-identifying figures appear
here: counts, durations, RSSI values, and distribution shapes — never a site
id, address, device name/inventory, IP/host, timestamped movement, or
occupancy state of a specific deployment (the near-empty adjacent-to-Pi band
is stated as a *design assumption for the target regime*, not a measurement
of any site). The labeled corpus (F4) is a sanitized derivative; raw captures
never leave the capture machine.
