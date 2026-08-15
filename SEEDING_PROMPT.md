You are a release engineer seeding the GitHub project for unclespeedo/blesentry.
Source of truth: ROADMAP.md (v1.2, all amendments merged) in the repo root.
Do NOT write application code. Do NOT alter ROADMAP.md except as instructed in
step 6. Be idempotent: skip anything that already exists (match by title).

1. LABELS — create:
   phase:0..4 · epic:<from roadmap> · type:{feature,infra,spike,docs,test,ops}
   priority:{p0,p1,p2} · size:{S,M,L}
   Workflow labels: agent:eligible · needs:hardware · needs:human-decision ·
   needs:secrets · blocked · in-progress · needs-human

2. MILESTONES — create:
   "Phase 0 — Provisioning & Gates", "Phase 1 — Core Skeleton",
   "Phase 2 — Presence/Alerts/Bot", "Phase 3 — Pi Deployment",
   "Phase 4 — Hardening", and "Leave-Site Gate" (due: <WINDOW_END_DATE>).
   Assign window-critical issues (P0-7..P0-11, P3-0, and the leave-site
   checklist items from §3 of the v1.1 amendment) to Leave-Site Gate in
   "Leave-Site Gate" milestone due date: 2026-08-30.
   ADDITION to their phase milestone where GitHub allows only one — prefer
   Leave-Site Gate for those.

3. ISSUES — one per roadmap ID (P0-1 … P4-9, incl. P0-4a, P0-10, P0-11, P3-0):
   - Title: "<ID>: <roadmap title>"
   - Body sections: Description (verbatim from roadmap) · Definition of Done
     (as a markdown task list, one checkbox per DoD clause) · Blocked by:
     (#refs — see step 4) · Size · Roadmap link (anchor into ROADMAP.md).
   - Apply phase/epic/type/priority/size labels from the roadmap.
   - Create in topological dependency order so #refs resolve.

4. DEPENDENCY & ELIGIBILITY TAGGING:
   - Add label `blocked` to any issue with an open dependency.
   - Add `needs:hardware` to: P0-7, P0-8, P0-10, P0-11, P1-3 (live-scan DoD
     portion), P3-0..P3-6, P4-1, P4-2, P4-3, P4-5, P4-6.
   - Add `needs:human-decision` to: P0-1, P0-2, P0-6, P0-4a (ADRs/gates).
   - Add `needs:secrets` to: P2-5, P2-6..P2-9 (live-verification portions), P4-4.
   - Add `agent:eligible` to every issue that is NOT needs:hardware or
     needs:human-decision. (needs:secrets issues are agent:eligible for
     implementation; the live-verification DoD checkbox is annotated
     "HUMAN VERIFY".)

5. PROJECT BOARD — create Project "blesentry v1" with columns:
   Backlog / Ready (unblocked) / In Progress / In Review / Human Verify / Done.
   Add all issues; place unblocked agent:eligible issues in Ready.

6. TRACEABILITY — append an "Issue Map" appendix to ROADMAP.md (ID → issue #)
   via a PR (do not push to main).

7. OUTPUT — a summary table (ID, issue #, milestone, labels, blocked-by) for
   human verification. Flag any roadmap ambiguity you had to interpret rather



8. SANITY GATE: before creating anything, verify ROADMAP.md
contains the string "Appendix: Issue Map" and issue IDs P0-1 through P4-9
including P0-4a, P0-10, P0-11, P3-0. If any are missing, STOP and report —
the roadmap merge (BOOTSTRAP.md step 1) is incomplete.
   than silently resolving it.
