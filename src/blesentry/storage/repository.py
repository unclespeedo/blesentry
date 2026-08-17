# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Async repository layer for devices and observations (P1-6).

All SQL is confined to this module — this is the storage seam per
ADR-0002.  Callers never touch the database directly.
"""

from __future__ import annotations

from typing import Any

import aiosqlite


class DeviceRepository:
    """Async repository for the ``devices`` table.

    Operations are scoped to a single ``site_id`` passed at construction
    time so callers never need to remember to filter by site.
    """

    def __init__(
        self, conn: aiosqlite.Connection, site_id: str
    ) -> None:
        """Initialise with an open connection and target site."""
        self._conn = conn
        self._site = site_id

    async def upsert(
        self,
        *,
        fingerprint: str,
        mac: str | None = None,
        label: str | None = None,
        description: str | None = None,
    ) -> int:
        """Insert a device or update its MAC on fingerprint match.

        Returns the device ``id`` (new or existing).
        """
        cur = await self._conn.execute(
            "INSERT INTO devices (site_id, fingerprint, mac, label, "
            "description) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (site_id, fingerprint) DO UPDATE SET "
            "mac = excluded.mac, label = excluded.label, "
            "description = excluded.description, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
            "RETURNING id",
            (self._site, fingerprint, mac, label, description),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            raise RuntimeError("RETURNING produced no row")
        return int(row[0])

    async def get(
        self, device_id: int
    ) -> dict[str, Any] | None:
        """Return a device row as a dict, or ``None`` if not found."""
        cur = await self._conn.execute(
            "SELECT id, site_id, fingerprint, mac, label, "
            "description, created_at, updated_at "
            "FROM devices WHERE id = ? AND site_id = ?",
            (device_id, self._site),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return {
            "id": row[0],
            "site_id": row[1],
            "fingerprint": row[2],
            "mac": row[3],
            "label": row[4],
            "description": row[5],
            "created_at": row[6],
            "updated_at": row[7],
        }

    async def list_devices(self) -> list[dict[str, Any]]:
        """Return all devices for this site."""
        cur = await self._conn.execute(
            "SELECT id, site_id, fingerprint, mac, label, "
            "description, created_at, updated_at "
            "FROM devices WHERE site_id = ?",
            (self._site,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [
            {
                "id": r[0],
                "site_id": r[1],
                "fingerprint": r[2],
                "mac": r[3],
                "label": r[4],
                "description": r[5],
                "created_at": r[6],
                "updated_at": r[7],
            }
            for r in rows
        ]


class ObservationRepository:
    """Async repository for the ``observations`` table.

    Observations are append-only RSSI records.  The repository scopes
    all queries to a single ``site_id``.
    """

    def __init__(
        self, conn: aiosqlite.Connection, site_id: str
    ) -> None:
        """Initialise with an open connection and target site."""
        self._conn = conn
        self._site = site_id

    async def append(
        self,
        *,
        device_id: int,
        rssi: int,
        observed_at: str,
        adapter_id: str | None = None,
    ) -> int:
        """Append one RSSI observation for a device.

        Returns the new observation ``id``.
        """
        cur = await self._conn.execute(
            "INSERT INTO observations "
            "(site_id, device_id, rssi, observed_at, adapter_id) "
            "VALUES (?, ?, ?, ?, ?) "
            "RETURNING id",
            (
                self._site,
                device_id,
                rssi,
                observed_at,
                adapter_id,
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

        Results are ordered chronologically.
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
