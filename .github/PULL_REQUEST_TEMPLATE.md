## Linked issue
Closes #

## DoD evidence
<!-- One row per DoD checkbox from the linked issue. Evidence = test names,
command output, file paths. "Done" without evidence will be rejected. -->
| DoD item | Evidence |
|---|---|
|  |  |

## Self-review checklist
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean
- [ ] `uv run ty check` clean
- [ ] `uv run pytest` green; new behavior has tests written first (TDD)
- [ ] MPL-2.0 header on all new source files
- [ ] DCO sign-off on all commits
- [ ] No SQL outside repository modules; seams (ADR-0002) respected
- [ ] No secrets, tokens, or credentials anywhere in the diff
- [ ] Evidence redacted per the AGENTS.md hygiene rule (no SSIDs, MACs,
      IPs, hostnames, inventories, or occupancy times)
- [ ] Scope limited to the linked issue
- [ ] HUMAN VERIFY checkboxes left unchecked and called out below

## Human-verification handoff (if any)
<!-- List DoD items requiring human action (hardware, secrets, live chat). -->
