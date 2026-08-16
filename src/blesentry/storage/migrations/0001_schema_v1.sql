-- This Source Code Form is subject to the terms of the Mozilla Public
-- License, v. 2.0. If a copy of the MPL was not distributed with this
-- file, You can obtain one at https://mozilla.org/MPL/2.0/.

CREATE TABLE devices (
    id INTEGER PRIMARY KEY,
    site_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    mac TEXT,
    label TEXT,
    description TEXT,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (site_id, fingerprint)
);

CREATE INDEX idx_devices_mac ON devices (mac);

CREATE TABLE observations (
    id INTEGER PRIMARY KEY,
    site_id TEXT NOT NULL,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    rssi INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    adapter_id TEXT
);

CREATE INDEX idx_observations_site_time
    ON observations (site_id, observed_at);

CREATE INDEX idx_observations_device_time
    ON observations (device_id, observed_at);

CREATE TABLE presence_events (
    id INTEGER PRIMARY KEY,
    site_id TEXT NOT NULL,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    event_type TEXT NOT NULL CHECK (event_type IN ('PRESENT', 'ABSENT')),
    occurred_at TEXT NOT NULL
);

CREATE INDEX idx_presence_events_device_time
    ON presence_events (device_id, occurred_at);

CREATE TABLE outbox (
    id INTEGER PRIMARY KEY,
    site_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'IN_FLIGHT', 'DELIVERED', 'FAILED')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    payload TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_outbox_claim ON outbox (status, next_attempt_at);

CREATE TABLE label_audit (
    id INTEGER PRIMARY KEY,
    site_id TEXT NOT NULL,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    actor TEXT NOT NULL,
    previous_label TEXT,
    new_label TEXT,
    changed_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_label_audit_device_time
    ON label_audit (device_id, changed_at);
