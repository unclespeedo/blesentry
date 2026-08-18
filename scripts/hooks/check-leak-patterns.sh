#!/bin/bash
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# Leak-pattern guard (AGENTS.md public-repo hygiene, #83).
# Blocks colon-hex MAC addresses and RFC1918 IPs from being committed
# outside tests/ (test files use synthetic identifiers; fixture
# corpora are governed by tests/fixtures/README.md — sanitized
# fixtures remap to TEST-NET addresses, which pass this guard).
# Known limits: cannot detect SSIDs or hex-embedded IPs.
# Allowlist: the repo's canonical synthetic example address, and any
# line carrying a deliberate "leak-ok" pragma.
ALLOW='AA:BB:CC:DD:EE:FF|leak-ok'
set -euo pipefail
status=0
for f in "$@"; do
  case "$f" in
    tests/* | *.lock | scripts/hooks/check-leak-patterns.sh) continue ;;
  esac
  [ -f "$f" ] || continue
  hits=$(grep -nIE '([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}' "$f" |
    grep -vE "$ALLOW" || true)
  if [ -n "$hits" ]; then
    printf '%s\n' "$hits"
    echo "leak-pattern: colon-hex MAC in $f — redact per AGENTS.md" >&2
    status=1
  fi
  hits=$(grep -nIE \
    '\b(192\.168|10\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01]))(\.[0-9]{1,3}){2}\b' \
    "$f" | grep -vE "$ALLOW" || true)
  if [ -n "$hits" ]; then
    printf '%s\n' "$hits"
    echo "leak-pattern: RFC1918 address in $f — redact per AGENTS.md" >&2
    status=1
  fi
done
exit "$status"
