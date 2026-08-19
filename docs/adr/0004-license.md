<!--
  SPDX-License-Identifier: MPL-2.0
  This Source Code Form is subject to the terms of the Mozilla Public
  License, v. 2.0. If a copy of the MPL was not distributed with this
  file, You can obtain one at https://mozilla.org/MPL/2.0/.
-->
# ADR-0004: License — MPL-2.0

- **Status:** Accepted
- **Date:** 2026-08-18
- **Deciders:** Ryan Speed

## Context

blesentry is modular OSS by design (ADR-0002): a core the project
maintains, plus extension points (Scanner/Notifier backends) that
third parties are expected to implement out of tree. The license had
to (a) keep improvements to *these files* open, (b) leave plugins,
glue code, and out-of-tree backends entirely to their authors, and
(c) stay friction-free for commercial deployment of the whole.

## Decision

**MPL-2.0**, applied file-level, already implemented repo-wide:

- `LICENSE` verbatim at the root; the Exhibit A three-line header on
  every source file (template in `mpl-header.txt`).
- Inbound = outbound: contributions are accepted under MPL-2.0 with
  DCO sign-off; no CLA.
- Out-of-tree code implementing the public protocols may use ANY
  license — that boundary is the point of the seam architecture.
- **Secondary-Licenses compatibility is retained** (no "Incompatible
  With Secondary Licenses" notice): MPL-covered files may be combined
  into (A)GPL works per MPL §3.3, keeping the codebase usable by
  copyleft downstream projects.

## Trade-offs accepted

- File-level copyleft is the deliberate middle ground: stronger than
  permissive (shipped modifications to these files must be
  published), weaker than project-level copyleft (linking and
  out-of-tree extension impose nothing).
- **Relicensing is hard by construction** (the recorded Q6
  trade-off): with no CLA, changing license later requires the
  consent of every contributor. Accepted — inbound=outbound with DCO
  is the lower-friction, higher-trust posture for a small OSS
  project, and MPL-2.0 is a stable destination, not a stepping stone.

## Consequences

- README carries the plain-English summary (use anywhere including
  commercially; ship modified blesentry files → publish those
  modifications; your plugins are yours).
- CONTRIBUTING states the inbound=outbound + DCO rule; CI enforces
  sign-offs; the header ships on every new file via the template.
