# blesentry — Day-0 Bootstrap (human runbook; delete this file once complete)

Order matters. Steps 1–4 are human-only.

1. MERGE ROADMAP.md to v1.2 (see checklist in this repo's commit message or
   below) and place it in the repo root. The seeding agent parses it — it is
   the single source of truth.
2. CREATE the GitHub repo `unclespeedo/blesentry` (public — MPL-2.0 project;
   private-until-P0-4 is acceptable if preferred). Do NOT initialize with any
   files.
3. PUSH this working directory as the initial commit:
     git init && git add -A
     git commit -s -m "chore: bootstrap repo scaffold, governance, and agent contract"
     git branch -M main && git remote add origin <url> && git push -u origin main
   (-s = DCO sign-off; required on every commit from here on.)
4. BRANCH PROTECTION on main: require PR, require `ci / checks` status,
   dismiss stale approvals, no force pushes. This is what makes Level-1
   agent autonomy safe.
5. SEED: give an agent with `gh` access the contents of SEEDING_PROMPT.md.
   Review its output summary table (ID → issue# → milestone → labels →
   blocked-by) before proceeding. Approve its Issue Map PR into ROADMAP.md.
6. FIRST AGENT SESSION: "Review AGENTS.md and tackle the next priority."
   Expected selection: P0-4. Review that PR carefully — it calibrates the bar.
7. SECRETS: none exist yet and none go to GitHub. The Telegram token and S3
   credentials live only on the Pi (root-owned config, outside the repo).
   Agents never see them; that is by design (AGENTS.md prohibitions).
8. Delete BOOTSTRAP.md in a final cleanup PR.

## ROADMAP merge checklist (v1.0 + v1.1 + v1.2 → one document)
- Base: v1.0 full roadmap.
- Apply v1.1: decision record table; rewritten P0-2, P0-8; revised P2-5,
  P2-8, P4-4, P4-9; new P0-10, P0-11, P3-0; 3-week window plan section;
  updated risks; leave-site gate checklist.
- Apply v1.2: license decision row (MPL-2.0, DCO, no CLA); P0-4 licensing
  scope; new P0-4a; delete "Remaining Open Items" (fold tailscaled posture
  into P0-8 DoD, S3 provider into P4-4 DoD).
- Add empty "## Appendix: Issue Map" section (seeding agent fills via PR).
- Window end date everywhere: 2026-08-30.

## Session log (delete with this file)
Updated: 2026-08-15 — idempotent run-through #2 in progress.

| Step | Status | Notes |
|---|---|---|
| 1. ROADMAP.md → v1.2 | DONE | Verified 2026-08-15: ROADMAP.md (11:46) is the merged v1.2 doc. License row (dec. #9), P0-4 licensing scope, P0-4a, tailscaled folded into P0-8 DoD, S3 provider into P4-4 DoD, "Remaining Open Items" gone, empty "Appendix: Issue Map" present, window end 2026-08-30 everywhere. Grep confirmed no leftover section. |
| 2. Create repo | BLOCKED | `unclespeedo/blesentry` does not exist. gh authed (repo+workflow scope). Human-only per runbook — awaiting human decision on creation + visibility. |
| 3. Initial commit/push | PENDING | Not a git repo yet. Can proceed once repo exists (git init/commit/branch/push). |
| 4. Branch protection | PENDING | Requires repo (step 2). Human-only. |
| 5. Seed issues | PENDING | Blocked by 2–3. Agent work (SEEDING_PROMPT.md). |
| 6. First agent session | PENDING | Blocked by 5. |
| 7. Secrets check | DONE | Scanned working tree 2026-08-15: no tokens/keys/creds (only doc mentions). `.env`, `config.local.toml`, `*.db*` gitignored. Re-verify post-commit at step 3. |
| 8. Delete BOOTSTRAP.md | PENDING | Final cleanup PR after step 6 review. |
