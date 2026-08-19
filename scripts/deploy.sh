#!/bin/bash
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# One-command deploy: Mac working tree -> Pi (P0-9, #10).
#
#   scripts/deploy.sh [user@host]
#
# Target defaults to $BLESENTRY_HOST, then $USER@blesentry-pi.local.
# Remote directory defaults to ~/blesentry ($BLESENTRY_DIR overrides).
# Idempotent and safe mid-development: rsync --delete mirrors the
# working tree (never git state), uv sync reconciles the venv, and the
# service restart is a guarded no-op until P3-1 ships a unit.
set -euo pipefail

HOST="${1:-${BLESENTRY_HOST:-$USER@blesentry-pi.local}}"
REMOTE_DIR="${BLESENTRY_DIR:-blesentry}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Non-interactive ssh sessions do not source ~/.profile, so uv is not
# on PATH remotely; resolve it explicitly with a PATH fallback.
UV='UV=$(command -v uv || echo "$HOME/.local/bin/uv")'

echo "deploy: $REPO_ROOT -> $HOST:~/$REMOTE_DIR"

rsync -az --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '.claude' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.ruff_cache' \
  --exclude '*.db' \
  --exclude '*.db-wal' \
  --exclude '*.db-shm' \
  "$REPO_ROOT/" "$HOST:$REMOTE_DIR/"

ssh "$HOST" "$UV; cd '$REMOTE_DIR' && \"\$UV\" sync"

ssh "$HOST" '
  if systemctl list-unit-files blesentry.service --no-legend \
      2>/dev/null | grep -q blesentry; then
    if sudo -n systemctl restart blesentry.service 2>/dev/null; then
      echo "deploy: blesentry.service restarted"
    else
      echo "deploy: WARNING: blesentry.service exists but passwordless" \
           "sudo for restart is not configured (P3-1)" >&2
    fi
  else
    echo "deploy: no blesentry.service yet (P3-1); skipping restart"
  fi
'

ssh "$HOST" "$UV; cd '$REMOTE_DIR' && \"\$UV\" run python -c \
  'import blesentry; print(\"deploy: OK,\", blesentry.__version__)'"
