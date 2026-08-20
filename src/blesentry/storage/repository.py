# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Async repository layer for devices, observations, and the outbox.

All SQL is confined to this module — this is the storage seam per
ADR-0002.  Callers never touch the database directly.  Devices and
observations are P1-6; the outbox enqueue API is P2-3.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypedDict

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


class OutboxRow(TypedDict):
    """Shape of a row returned by :class:`OutboxRepository`."""

    id: int
    site_id: str
    status: str
    attempt_count: int
    next_attempt_at: str | None
    payload: str
    last_error: str | None
    created_at: str


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

    async def list_recent(self, limit: int) -> list[DeviceRow]:
        """Return the most recently updated devices, newest first."""
        cur = await self._conn.execute(
            "SELECT id, site_id, fingerprint, address, label, "
            "description, created_at, updated_at "
            "FROM devices WHERE site_id = ? "
            "ORDER BY updated_at DESC, id DESC LIMIT ?",
            (self._site, limit),
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

    async def touch_address(self, device_id: int, address: str) -> None:
        """Record a fused rotation's current address (#19).

        Semantically consistent with ``updated_at`` = identity/metadata
        changed: the UPDATE is conditional on the address actually
        changing (``IS NOT`` is NULL-safe), so a chatty device minting
        new payload keys at a fixed address costs zero writes — the
        #84 savings hold per-rotation, not per-sighting. A wrong-site
        or stale ``device_id`` is a silent no-op (caller contract, as
        with append).
        """
        async with transaction(self._conn):
            await self._conn.execute(
                "UPDATE devices SET address = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ? AND site_id = ? AND address IS NOT ?",
                (address, device_id, self._site, address),
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


def _to_outbox_row(row: Sequence[Any]) -> OutboxRow:
    """Map a full-column ``outbox`` result row to :class:`OutboxRow`.

    Rows are positional tuples (``connect`` sets no ``row_factory``);
    the ``Sequence`` annotation keeps access index-only, not by name.
    """
    return OutboxRow(
        id=row[0],
        site_id=row[1],
        status=row[2],
        attempt_count=row[3],
        next_attempt_at=row[4],
        payload=row[5],
        last_error=row[6],
        created_at=row[7],
    )


class OutboxRepository:
    """Async repository for the ``outbox`` table (P2-3).

    The outbox is the durability boundary: every outbound message is
    written here with status ``PENDING`` synchronously with the event
    that produced it, before any delivery attempt — nothing is ever
    fire-and-forget.  Enqueue joins the caller's ambient transaction
    (#84), so a message and its triggering event commit or roll back as
    one unit.  Claiming, backoff, and status transitions belong to the
    drain loop (P2-4); this class owns enqueue and the ordered reads.

    FIFO ordering relies on SQLite's monotonically increasing rowid
    (``id``), which holds while rows are only appended.  P2-4 keeps this
    invariant: it *marks* terminal rows (DELIVERED / FAILED) rather than
    deleting them, so rowids are never reused and no ``AUTOINCREMENT``
    migration is needed.  A future retention pass that deletes terminal
    rows must revisit this (switch ``id`` to ``AUTOINCREMENT``) before a
    reused rowid can rewind the order or alias a live message id.
    """

    def __init__(self, conn: aiosqlite.Connection, site_id: str) -> None:
        """Initialise with an open connection and target site."""
        self._conn = conn
        self._site = site_id

    @property
    def connection(self) -> aiosqlite.Connection:
        """The underlying connection, for ambient transactions (#84)."""
        return self._conn

    async def enqueue(self, *, payload: str) -> int:
        """Append one message as ``PENDING``; return its new id.

        ``status``/``attempt_count``/``created_at`` take their schema
        defaults (PENDING / 0 / now).  Ordering is by id, so the drain
        loop (P2-4) delivers messages in the order they were enqueued.

        Caller contract: wrap the enqueue in the *same* ``transaction()``
        as the triggering write when the two must be atomic — the alert
        and the event that raised it then commit together, or not at all
        (never a fire-and-forget row outliving a failed event).
        """
        async with transaction(self._conn):
            cur = await self._conn.execute(
                "INSERT INTO outbox (site_id, payload) "
                "VALUES (?, ?) RETURNING id",
                (self._site, payload),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise RuntimeError("RETURNING produced no row")
            return int(row[0])

    async def list_pending(self) -> list[OutboxRow]:
        """Return this site's PENDING messages, oldest first (FIFO)."""
        cur = await self._conn.execute(
            "SELECT id, site_id, status, attempt_count, "
            "next_attempt_at, payload, last_error, created_at "
            "FROM outbox WHERE site_id = ? AND status = 'PENDING' "
            "ORDER BY id",
            (self._site,),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [_to_outbox_row(r) for r in rows]

    async def get(self, outbox_id: int) -> OutboxRow | None:
        """Return one outbox row, or ``None`` if not found."""
        cur = await self._conn.execute(
            "SELECT id, site_id, status, attempt_count, "
            "next_attempt_at, payload, last_error, created_at "
            "FROM outbox WHERE id = ? AND site_id = ?",
            (outbox_id, self._site),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return _to_outbox_row(row)

    async def head_pending(self) -> OutboxRow | None:
        """Return the oldest PENDING message, or ``None`` if none.

        The strict FIFO head the drain loop (P2-4) works from — returned
        regardless of ``next_attempt_at`` so the drain, not a query,
        decides whether the head is due yet.  Terminal rows (DELIVERED,
        FAILED) and IN_FLIGHT are skipped.
        """
        cur = await self._conn.execute(
            "SELECT id, site_id, status, attempt_count, "
            "next_attempt_at, payload, last_error, created_at "
            "FROM outbox WHERE site_id = ? AND status = 'PENDING' "
            "ORDER BY id LIMIT 1",
            (self._site,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return _to_outbox_row(row)

    async def mark_delivered(self, outbox_id: int) -> None:
        """Mark a message DELIVERED (terminal success)."""
        async with transaction(self._conn):
            await self._conn.execute(
                "UPDATE outbox SET status = 'DELIVERED' "
                "WHERE id = ? AND site_id = ?",
                (outbox_id, self._site),
            )

    async def mark_failed(self, outbox_id: int, error: str) -> None:
        """Mark a message FAILED (terminal dead-letter).

        For permanent delivery failures and unusable payloads — removed
        from the PENDING queue so it can never block delivery of the
        messages behind it, with ``last_error`` kept for diagnosis.
        """
        async with transaction(self._conn):
            await self._conn.execute(
                "UPDATE outbox SET status = 'FAILED', last_error = ? "
                "WHERE id = ? AND site_id = ?",
                (error, outbox_id, self._site),
            )

    async def reschedule(
        self,
        outbox_id: int,
        *,
        next_attempt_at: str,
        error: str,
    ) -> None:
        """Defer a retriable failure: bump attempt, set the next time.

        The message stays PENDING (nothing is dropped on repeated
        failure); ``attempt_count`` increments and ``next_attempt_at``
        gates when the drain will try again (backoff is the caller's).
        """
        async with transaction(self._conn):
            await self._conn.execute(
                "UPDATE outbox SET "
                "attempt_count = attempt_count + 1, "
                "next_attempt_at = ?, last_error = ? "
                "WHERE id = ? AND site_id = ?",
                (next_attempt_at, error, outbox_id, self._site),
            )
