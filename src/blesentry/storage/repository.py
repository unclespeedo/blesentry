# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Async repository layer for devices and observations (P1-6).

All SQL is confined to this module — this is the storage seam per
ADR-0002.  Callers never touch the database directly.
"""

from __future__ import annotations

from typing import TypedDict

import aiosqlite

from blesentry.storage.database import transaction


class DeviceRow(TypedDict):
    """Shape of a row returned by :class:`DeviceRepository`."""

    id: int
    site_id: str
    fingerprint: str
    address: str | None
    label: str | None
    description: str | None
    created_at: str
    updated_at: str


class ObservationRow(TypedDict):
    """Shape of a row returned by :class:`ObservationRepository`."""

    id: int
    site_id: str
    device_id: int
    rssi: int
    observed_at: str
    adapter_id: str | None
    address_type: str | None
    adv_type: str | None


class DeviceRepository:
    """Async repository for the ``devices`` table.

    Operations are scoped to a single ``site_id`` passed at construction
    time so callers never need to remember to filter by site.
    """

    def __init__(self, conn: aiosqlite.Connection, site_id: str) -> None:
        """Initialise with an open connection and target site."""
        self._conn = conn
        self._site = site_id

    @property
    def connection(self) -> aiosqlite.Connection:
        """The underlying connection, for ambient transactions (#84)."""
        return self._conn

    async def upsert(
        self,
        *,
        fingerprint: str,
        address: str | None = None,
        label: str | None = None,
        description: str | None = None,
    ) -> int:
        """Insert a device or update on fingerprint match.

        On conflict the address is always replaced.  ``label`` and
        ``description`` use ``COALESCE`` so omitting them preserves
        existing operator-assigned metadata.

        Returns the device ``id`` (new or existing).
        """
        async with transaction(self._conn):
            cur = await self._conn.execute(
                "INSERT INTO devices "
                "(site_id, fingerprint, address, label, "
                "description) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (site_id, fingerprint) "
                "DO UPDATE SET "
                "address = COALESCE("
                "excluded.address, devices.address"
                "), "
                "label = COALESCE("
                "excluded.label, devices.label"
                "), "
                "description = COALESCE("
                "excluded.description, "
                "devices.description"
                "), "
                "updated_at = strftime("
                "'%Y-%m-%dT%H:%M:%fZ', 'now'"
                ") "
                "RETURNING id",
                (
                    self._site,
                    fingerprint,
                    address,
                    label,
                    description,
                ),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise RuntimeError("RETURNING produced no row")
            return int(row[0])

    async def get_by_fingerprint(self, fingerprint: str) -> DeviceRow | None:
        """Return the device owning this exact fingerprint key, if any."""
        cur = await self._conn.execute(
            "SELECT id, site_id, fingerprint, address, label, "
            "description, created_at, updated_at "
            "FROM devices WHERE site_id = ? AND fingerprint = ?",
            (self._site, fingerprint),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return DeviceRow(
            id=row[0],
            site_id=row[1],
            fingerprint=row[2],
            address=row[3],
            label=row[4],
            description=row[5],
            created_at=row[6],
            updated_at=row[7],
        )

    async def touch_address(self, device_id: int, address: str) -> None:
        """Record a fused rotation's current address (#19).

        Semantically consistent with ``updated_at`` = identity/metadata
        changed: a rotation IS an identity event, and it is per-rotation
        (~minutes), not per-sighting, so #84's write savings hold.
        """
        await self._conn.execute(
            "UPDATE devices SET address = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "WHERE id = ? AND site_id = ?",
            (address, device_id, self._site),
        )

    async def get(self, device_id: int) -> DeviceRow | None:
        """Return a device row, or ``None`` if not found."""
        cur = await self._conn.execute(
            "SELECT id, site_id, fingerprint, address, label, "
            "description, created_at, updated_at "
            "FROM devices WHERE id = ? AND site_id = ?",
            (device_id, self._site),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return DeviceRow(
            id=row[0],
            site_id=row[1],
            fingerprint=row[2],
            address=row[3],
            label=row[4],
            description=row[5],
            created_at=row[6],
            updated_at=row[7],
        )

    async def list_devices(self) -> list[DeviceRow]:
        """Return all devices for this site, ordered by id."""
        cur = await self._conn.execute(
            "SELECT id, site_id, fingerprint, address, label, "
            "description, created_at, updated_at "
            "FROM devices WHERE site_id = ? "
            "ORDER BY id",
            (self._site,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [
            DeviceRow(
                id=r[0],
                site_id=r[1],
                fingerprint=r[2],
                address=r[3],
                label=r[4],
                description=r[5],
                created_at=r[6],
                updated_at=r[7],
            )
            for r in rows
        ]


class ObservationRepository:
    """Async repository for the ``observations`` table.

    Observations are append-only RSSI records.  The repository scopes
    all queries to a single ``site_id``.
    """

    def __init__(self, conn: aiosqlite.Connection, site_id: str) -> None:
        """Initialise with an open connection and target site."""
        self._conn = conn
        self._site = site_id

    @property
    def connection(self) -> aiosqlite.Connection:
        """The underlying connection, for ambient transactions (#84)."""
        return self._conn

    async def append(
        self,
        *,
        device_id: int,
        rssi: int,
        observed_at: str,
        adapter_id: str | None = None,
        address_type: str | None = None,
        adv_type: str | None = None,
    ) -> int:
        """Append one RSSI observation for a device.

        Caller contract: ``device_id`` must come from this site's
        :class:`DeviceRepository` (the FK enforces existence; the
        redundant per-append ownership probe was dropped in #84 —
        cross-site misuse is a caller bug, not a runtime check).
        Returns the new observation ``id``.
        """
        async with transaction(self._conn):
            cur = await self._conn.execute(
                "INSERT INTO observations "
                "(site_id, device_id, rssi, "
                "observed_at, adapter_id, "
                "address_type, adv_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "RETURNING id",
                (
                    self._site,
                    device_id,
                    rssi,
                    observed_at,
                    adapter_id,
                    address_type,
                    adv_type,
                ),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise RuntimeError("RETURNING produced no row")
            return int(row[0])

    async def query_recent_rssi(
        self,
        *,
        device_id: int,
        since: str,
    ) -> list[tuple[str, int]]:
        """Return ``(observed_at, rssi)`` pairs since a timestamp.

        ``since`` must use the fixed-width UTC format
        ``%Y-%m-%dT%H:%M:%fZ`` — lexical ordering is used
        for filtering and sorting.
        """
        cur = await self._conn.execute(
            "SELECT observed_at, rssi FROM observations "
            "WHERE device_id = ? AND site_id = ? "
            "AND observed_at >= ? "
            "ORDER BY observed_at ASC",
            (device_id, self._site, since),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [(r[0], r[1]) for r in rows]

    async def get(self, observation_id: int) -> ObservationRow | None:
        """Return an observation row, or ``None``."""
        cur = await self._conn.execute(
            "SELECT id, site_id, device_id, rssi, "
            "observed_at, adapter_id, "
            "address_type, adv_type "
            "FROM observations "
            "WHERE id = ? AND site_id = ?",
            (observation_id, self._site),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return ObservationRow(
            id=row[0],
            site_id=row[1],
            device_id=row[2],
            rssi=row[3],
            observed_at=row[4],
            adapter_id=row[5],
            address_type=row[6],
            adv_type=row[7],
        )
