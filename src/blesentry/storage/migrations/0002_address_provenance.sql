-- This Source Code Form is subject to the terms of the Mozilla Public
-- License, v. 2.0. If a copy of the MPL was not distributed with this
-- file, You can obtain one at https://mozilla.org/MPL/2.0/.

-- #56: mac -> address (the field is a peripheral UUID on CoreBluetooth,
-- not a MAC), and authoritative per-observation address provenance.

ALTER TABLE devices RENAME COLUMN mac TO address;

DROP INDEX idx_devices_mac;
CREATE INDEX idx_devices_address ON devices (address);

ALTER TABLE observations ADD COLUMN address_type TEXT;
ALTER TABLE observations ADD COLUMN adv_type TEXT;
