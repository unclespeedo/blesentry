-- This Source Code Form is subject to the terms of the Mozilla Public
-- License, v. 2.0. If a copy of the MPL was not distributed with this
-- file, You can obtain one at https://mozilla.org/MPL/2.0/.

-- #19: resolver startup seeding reads the newest devices
-- (ORDER BY updated_at DESC). Without this index that is a full site
-- scan plus a temp B-tree sort at every process start — seconds-to-
-- minutes on Pi/SD at winter scale, on the crash-recovery path.
-- With it, a backward index scan serves the query directly.

CREATE INDEX idx_devices_site_updated ON devices (site_id, updated_at);
