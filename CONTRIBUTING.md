# Contributing to blesentry

## Licensing
- Inbound = outbound: all contributions are accepted under MPL-2.0.
- Every commit requires DCO sign-off (`git commit -s`). No CLA.
- Out-of-tree plugins (custom Scanner/Notifier implementations against our
  protocols) may use ANY license — that boundary is intentional (ADR-0002,
  ADR-0004).
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
- All SQL lives in repository modules; nothing else touches the database.
- Respect the seams: Scanner and Notifier protocols per ADR-0002;
  Resolver lifecycle (`resolve`/`commit`/`abort`/`seed`) per
  ADR-0005 — one instance across cycles, resolve inside the cycle
  transaction, commit only after COMMIT. New external dependencies
  require justification in the PR (512MB RAM target).
- CI never touches a radio. Drive the Scanner seam with ``MockScanner``:
  batch scenarios for appear / disappear / MAC rotation, and
  ``MockScanner.from_rssi_sequences`` when a test needs a per-device
  RSSI profile (near-threshold flicker, gradual approach, brief spike).

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
