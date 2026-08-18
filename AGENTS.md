# AGENTS.md — Operating Contract for blesentry

## Context
Local-first BLE presence sentinel. Read before any work: ROADMAP.md,
docs/adr/*, CONTRIBUTING.md. Conventions: Python 3 + uv, ruff (line 79), ty,
pytest (TDD — test first), Pydantic V2 ConfigDict, MPL-2.0 headers on every
new file, DCO sign-off on every commit. The local pre-commit gate is the
fast feedback loop; CI enforces the same checks on every PR (branch
protection requires the `checks` and `dco` jobs) and on main.

## The Loop: "tackle the next priority"
0. HEALTH CHECK: if CI on main is red, fixing it IS the next priority —
   nothing else is selectable until main is green.
1. SELECT: from issues labeled `agent:eligible` AND NOT `blocked` AND NOT
   `in-progress`, pick highest priority (p0>p1>p2), tie-break by lowest
   phase, then smallest size, then lowest ID. Comment your selection
   rationale on the issue; add `in-progress`; move board card.
2. PLAN: restate the DoD in your own words in an issue comment. If the DoD is
   ambiguous or conflicts with an ADR → STOP, label `needs-human`, pick next.
3. IMPLEMENT: branch `feat/<ID>-<slug>` (or fix/, docs/, ops/). TDD: failing
   test → implementation → green. Conventional commits, one logical change
   each, `Signed-off-by` on all.
4. SELF-REVIEW: run the full local gate — `uvx pre-commit run --all-files`
   (the single source of truth; never a hand-typed subset of its hooks).
   Re-read the diff as a hostile reviewer. Check: MPL header on new files,
   no secrets, no SQL outside repositories, no code outside the seam
   contracts (ADR-0002).
5. PR: one issue per PR. Template: DoD checklist with EVIDENCE per checkbox
   (test names, command output). Checkboxes annotated "HUMAN VERIFY" are left
   unchecked with a note. Link `Closes #N`.
6. MERGE: only after the local pre-commit gate passes AND per current
   autonomy level (see below).
   Never force-push. Never merge with unchecked non-HUMAN-VERIFY boxes.
7. CLOSE OUT: remove `in-progress`; remove `blocked` from issues this
   unblocked and move them to Ready; move card to Done or Human Verify.
8. If nothing is selectable: report why (all blocked / all needs:hardware)
   and list what human action unblocks the most work. Do not invent work.

## Hard Prohibitions
- Never claim issues labeled `needs:hardware` or `needs:human-decision`.
- Never handle, request, or commit secrets (tokens, S3 creds).
- This repo is PUBLIC. Never post site-identifying operational data to
  issues, PRs, or commits: WiFi SSIDs, device MAC addresses, household
  device inventories, IP addresses, clock-time schedules, coordinates.
  Evidence uses counts, durations, and redacted identifiers; full
  fidelity stays local. Scrub mistakes by delete-and-repost, never
  edit (edit history is public).
- Never SSH to or target the Pi. Never create tags or GitHub Releases.
- Never modify ROADMAP.md, ADRs, or this file without a `needs-human` PR.
- Never widen an issue's scope. Found adjacent work? Open a new issue,
  label it, link it, move on.

## Autonomy Level (human-adjusted; current: LEVEL 1)
- LEVEL 1: agent opens PR; human reviews and merges every PR.
- LEVEL 2: agent merges S-size PRs on a passing local pre-commit gate;
  human reviews M/L.
- LEVEL 3: agent merges all sizes on a passing local pre-commit gate;
  human audits weekly.
Releases at every level: agent may PREPARE a release PR (changelog, version
bump); a human tags.

## Escalation
Blocked, ambiguous, or DoD-unachievable: comment specifics on the issue,
label `needs-human`, select the next issue. Never guess on ADR-level matters.
