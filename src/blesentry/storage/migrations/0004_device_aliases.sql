-- This Source Code Form is subject to the terms of the Mozilla Public
-- License, v. 2.0. If a copy of the MPL was not distributed with this
-- file, You can obtain one at https://mozilla.org/MPL/2.0/.

-- ADR-0005 / #96: durable fusion aliases. Later fingerprints joined
-- to an existing device persist here (audit trail + restart lookup
-- once DeviceResolver wires persist-inside-resolve). Founding keys
-- stay on devices.fingerprint. All access is DeviceRepository-only.

CREATE TABLE device_aliases (
    id INTEGER PRIMARY KEY,
    site_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    created_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (site_id, fingerprint)
);

CREATE INDEX idx_device_aliases_device
    ON device_aliases (site_id, device_id);
