# Contributing to blesentry

## Licensing
- Inbound = outbound: all contributions are accepted under MPL-2.0.
- Every commit requires DCO sign-off (`git commit -s`). No CLA.
- Out-of-tree plugins (custom Scanner/Notifier/Detector implementations
  against our protocols) may use ANY license — that boundary is
  intentional (ADR-0002, ADR-0004, ADR-0006).
- Every new source file carries this header (template wired at repo root
  in `mpl-header.txt`):

    # This Source Code Form is subject to the terms of the Mozilla Public
    # License, v. 2.0. If a copy of the MPL was not distributed with this
    # file, You can obtain one at https://mozilla.org/MPL/2.0/.

## Toolchain & conventions (locked — see ROADMAP.md "Locked technical decisions")
- Python 3, uv-managed. `uv run <tool>` for everything.
- ruff, line length 79 (check + format). ty for type checking. pytest +
  pytest-asyncio for tests. TDD: the failing test lands before the fix.
- Pydantic V2 with ConfigDict for all data models. src/ layout, py.typed.
- Finite floats on duration / rate / threshold knobs: ``math.isfinite(x)``
  **and** the sign/range check. ``argparse`` ``type=float`` and Pydantic
  ``Field(gt=0)`` / ``ge=0`` both admit ``inf`` / ``nan`` (``inf > 0``
  is true); those collapse ``//`` buckets to zero and ``json.dumps``
  emits non-JSON ``Infinity``. CLI tests for ``type=float`` knobs
  include ``inf`` and ``nan``, not only ``0`` and negatives. Integers
  are unaffected.
- All SQL lives in repository modules; nothing else touches the database.
- Respect the seams: Scanner and Notifier protocols per ADR-0002;
  Detector protocol per ADR-0006; Resolver lifecycle
  (`resolve`/`commit`/`abort`/`seed`) per ADR-0005 — one instance
  across cycles, resolve inside the cycle transaction, commit only
  after COMMIT. `run_loop` fail-fasts
  before `seed()` if a caller-supplied resolver's connection or
  `site_id` differs from the cycle `devices` repo; `run_cycle`
  checks the same (#149). New external dependencies
  require justification in the PR (512MB RAM target).
- CI never touches a radio. Drive the Scanner seam with ``MockScanner``:
  batch scenarios for appear / disappear / MAC rotation, and
  ``MockScanner.from_rssi_sequences`` when a test needs a per-device
  RSSI profile (near-threshold flicker, gradual approach, brief spike).
  Drive the Detector seam with ``MockDetector`` / ``NullDetector``
  (ADR-0006); never a live detector backend in CI. Offline replay
  (``blesentry replay``, ``blesentry.detection.replay``) is the F1
  harness: synthetic fixtures under ``tests/fixtures/replay/``, a
  read-only observations snapshot, golden-file JSON — never a live
  WAL and never a capture corpus dumped to CI logs (``docs/replay.md``).
  Canonical detection feature vectors (F3) live in
  ``blesentry.detection.features``; formulas are pinned in
  ``docs/features.md``. A2 reuses ``rssi_slope`` / ``rssi_span`` /
  ``max_rssi_by_identity`` — do not invent a second slope. Crowd/
  Inside own ``band_counts``. A3 alert text uses ``proximity_band``
  (exclusive F3 label of the terminal RSSI). The A1 approach
  trigger (ADR-0007, ``docs/approach.md``)
  is ``blesentry.detection.approach.is_rising_approach``; A2/A3 call
  it and do not fork W/Δ/peak. The A2 online tracker is
  ``blesentry.detection.trajectory.TrajectoryTracker`` (cap 256,
  fade-after 12, deque maxlen = A1 W). The A3 backend is
  ``blesentry.detection.approach_detector.ApproachDetector``
  (``[detection] backend = "approach"``, ``kind="approaching"``);
  default remains ``none``. The I1 inside spec (ADR-0009,
  ``docs/inside.md``) is ``blesentry.detection.inside.inside_count``
  / ``inside_sustain_step``. I3 reserves ``detector="inside"`` /
  ``kind="inside-adjacent"``; ``[detection] backend = "inside"`` is
  not yet a legal union member (load-time ``ConfigError`` until I3).
  F6 ``is_familiar`` / ``FamiliarSet`` live in
  ``blesentry.detection.familiar`` (``docs/familiar.md``); I2 wires
  exclusion into detectors. Alert text never uses metres
  or a raw address. Do not dump feature vectors of a real capture into CI
  logs (same hygiene as replay).

## Workflow
- One issue per PR; `Closes #N` in the description. No scope creep — file a
  new issue instead.
- Branches: feat/<ID>-<slug>, fix/…, docs/…, ops/…. Conventional commits.
- CI must be green; PR template's DoD evidence table must be complete.
- Branch protection on `main` requires the `checks` and `dco` CI jobs to
  pass on every PR. `dco` fails any commit without a DCO sign-off.
- **Read [SECURITY.md](SECURITY.md) before contributing.** This repo
  is fully public and the software observes real sites: operational
  environment details (SSIDs, MACs, device inventories, IPs,
  schedules, raw advertisement payloads) must never appear in
  issues, PRs, commits, branch names, or CI logs — they are a
  security exposure AND they mislead end users who read site
  specifics as product documentation. Keep site facts in gitignored
  `*.local.md` / `notes/` files; fixture corpora follow
  `tests/fixtures/README.md`. This binds humans and agents equally.
- Agents: AGENTS.md is your operating contract and overrides ambiguity here.

## Humans vs agents
Issues labeled `needs:hardware`, `needs:human-decision`: human-only.
`needs:secrets`: agents implement; humans run the live-verification steps.
