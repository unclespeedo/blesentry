# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""SQLite storage: connection bootstrap, migration runner, and v1 schema."""

from blesentry.storage.database import (
    DEFAULT_MIGRATIONS_DIR,
    MigrationError,
    apply_migrations,
    connect,
    connect_readonly,
    transaction,
)
from blesentry.storage.repository import (
    DeviceRepository,
    InitSessionRepository,
    ObservationRepository,
    OutboxRepository,
    PresenceEventRepository,
    SiteStateRepository,
)

__all__ = [
    "DEFAULT_MIGRATIONS_DIR",
    "DeviceRepository",
    "InitSessionRepository",
    "MigrationError",
    "ObservationRepository",
    "OutboxRepository",
    "PresenceEventRepository",
    "SiteStateRepository",
    "apply_migrations",
    "connect",
    "connect_readonly",
    "transaction",
]
