-- This Source Code Form is subject to the terms of the Mozilla Public
-- License, v. 2.0. If a copy of the MPL was not distributed with this
-- file, You can obtain one at https://mozilla.org/MPL/2.0/.

-- P2-9 / #30: restart-stable per-site markers (daily-summary last-sent)
-- and indexes for the digest window queries. All site_state access is
-- SiteStateRepository-only.

CREATE TABLE site_state (
    id INTEGER PRIMARY KEY,
    site_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (site_id, key)
);

CREATE INDEX idx_presence_events_site_time
    ON presence_events (site_id, occurred_at, id);

CREATE INDEX idx_devices_site_created
    ON devices (site_id, created_at);

CREATE INDEX idx_outbox_site_status
    ON outbox (site_id, status);
