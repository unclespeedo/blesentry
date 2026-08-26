-- This Source Code Form is subject to the terms of the Mozilla Public
-- License, v. 2.0. If a copy of the MPL was not distributed with this
-- file, You can obtain one at https://mozilla.org/MPL/2.0/.

-- P2-7 / #28: at-most-one bulk-label session per site. /init and
-- `blesentry init` share this row so a partial session survives
-- restart and can finish on the other surface. device_ids is a JSON
-- snapshot taken at start (not a live present-set). All access is
-- InitSessionRepository-only.

CREATE TABLE init_sessions (
    id INTEGER PRIMARY KEY,
    site_id TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('ACTIVE', 'DONE', 'CANCELLED', 'EXPIRED')),
    cursor INTEGER NOT NULL DEFAULT 0,
    device_ids TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE UNIQUE INDEX idx_init_sessions_one_active
    ON init_sessions (site_id) WHERE status = 'ACTIVE';

CREATE INDEX idx_init_sessions_site ON init_sessions (site_id);
