<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# Security policy

blesentry is a **fully public** OSS project whose running instances
observe **real physical sites**. That combination creates a rule that
binds every contributor — human and agent alike.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting (Security → Report a
vulnerability) on this repository. Do not open a public issue for
exploitable defects. Expect an acknowledgment within a week.

## Operational-data hygiene (binding on all contributors)

Every deployment of this software watches somebody's home or site.
The repository must never describe any *particular* site — for two
independent reasons:

1. **Security.** SSIDs geolocate via wardriving databases; MACs are
   persistent physical identifiers; device inventories, IP layouts,
   and occupancy schedules together are a reconnaissance report.
   Public events (comments, commits) are archived by scrapers at
   creation — deletion is damage limitation, not recall.
2. **Correctness of the docs.** Site-specific details in a public
   repo read as *product documentation*. An end user who sees a
   particular network layout, device brand, or schedule in the docs
   will reasonably assume it is a requirement or a default, and
   build confusion on top of it.

### Never publish — in issues, PRs, commits, branch names, or CI logs

- WiFi SSIDs; hostnames and usernames beyond the product defaults
- Device identifiers: MAC/BLE addresses, advertised device names
- **Raw advertisement payloads** — hex is not redaction; payloads
  embed IPs, ports, serial numbers, contact-info hashes, and keys
- Household or site device inventories (brands, models, counts)
- IP addresses and network topology of any real site
- Occupancy or observation schedules; coordinates

### Evidence style

Counts, durations, and redacted identifiers. "20 devices in a 12 s
window" is good evidence; a device table is a leak. The repository's
canonical synthetic example address is `AA:BB:CC:DD:EE:FF`; test
identifiers are always synthetic.

### Where environment details DO belong

Local development and testing genuinely need site facts. Keep them in
files the repository cannot ingest:

- `*.local.md` and `notes/` are gitignored — e.g. `site.local.md`
  next to your checkout for device inventories, addresses, network
  facts, capture logs.
- Agent contributors keep site facts in their private memory stores,
  never in tracker text.
- Raw captures stay on the capture machine (`capture*.json` and
  `*.log` are gitignored); only sanitized derivatives are committed.

### Fixture corpora

The one sanctioned path for real-capture data is
`tests/fixtures/README.md`: keyed pseudonyms, TEST-NET address
remaps, epoch-shifted timestamps, generic labels. Nothing else from a
real site enters the tree.

### Enforcement

- `scripts/hooks/check-leak-patterns.sh` blocks colon-hex MACs and
  RFC1918 addresses at pre-commit and in CI (outside `tests/`).
- The PR template carries an evidence-redaction self-review checkbox.
- The AGENTS.md Hard Prohibitions bind agent contributors to this
  policy verbatim; this file is the canonical statement for everyone.

### If something leaks

Comments/descriptions: delete and repost redacted — never edit (edit
history is public) — and treat the leaked value as burned. A leak in
a commit or pushed branch: stop and escalate to the maintainer
(history rewriting is maintainer-only). Assume scrapers archived the
original at creation.
