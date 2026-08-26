# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Async repository layer for devices, observations, and the outbox.

All SQL is confined to this module — this is the storage seam per
ADR-0002.  Callers never touch the database directly.  Devices and
observations are P1-6; the outbox enqueue API is P2-3; device_aliases
(ADR-0005) are DeviceRepository-only.
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


class DeviceAliasRow(TypedDict):
    """Shape of a row returned by alias methods (ADR-0005)."""

    id: int
    site_id: str
    fingerprint: str
    device_id: int
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


class PresenceEventRow(TypedDict):
    """Shape of a row returned by :class:`PresenceEventRepository`."""

    id: int
    site_id: str
    device_id: int
    event_type: str
    occurred_at: str


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

        Raises:
            ValueError: If ``fingerprint`` is already an alias for this
                site (cross-table uniqueness, ADR-0005 / #148).
        """
        async with transaction(self._conn):
            cur = await self._conn.execute(
                "SELECT 1 FROM device_aliases "
                "WHERE site_id = ? AND fingerprint = ?",
                (self._site, fingerprint),
            )
            taken = await cur.fetchone()
            await cur.close()
            if taken is not None:
                raise ValueError(
                    "cannot create device: fingerprint is already an alias"
                )
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

    async def record_alias(self, *, fingerprint: str, device_id: int) -> int:
        """Bind a fused fingerprint to a device (ADR-0005).

        Site-scoped: an unknown or other-site ``device_id`` raises.
        Re-binding the same fingerprint to a different device raises
        (alias conflict). Same device is idempotent (no write).
        A fingerprint that is already a founding ``devices.fingerprint``
        on this site raises (cross-table uniqueness, ADR-0005 / #148).

        Returns the alias row ``id``.
        """
        async with transaction(self._conn):
            cur = await self._conn.execute(
                "SELECT id FROM devices WHERE id = ? AND site_id = ?",
                (device_id, self._site),
            )
            owner = await cur.fetchone()
            await cur.close()
            if owner is None:
                raise ValueError(f"device {device_id} not found for this site")
            cur = await self._conn.execute(
                "SELECT id FROM devices WHERE site_id = ? AND fingerprint = ?",
                (self._site, fingerprint),
            )
            founding = await cur.fetchone()
            await cur.close()
            if founding is not None:
                raise ValueError(
                    "alias conflict: fingerprint is a founding key"
                )
            cur = await self._conn.execute(
                "SELECT id, device_id FROM device_aliases "
                "WHERE site_id = ? AND fingerprint = ?",
                (self._site, fingerprint),
            )
            existing = await cur.fetchone()
            await cur.close()
            if existing is not None:
                if int(existing[1]) != device_id:
                    raise ValueError(
                        "alias conflict: fingerprint already bound "
                        f"to device {existing[1]}"
                    )
                # Same binding: no write. Bumping updated_at here would
                # WAL-churn the SD on every future persist-on-resolve
                # of a known alias (the #84 / touch_address lesson).
                return int(existing[0])
            cur = await self._conn.execute(
                "INSERT INTO device_aliases "
                "(site_id, fingerprint, device_id) "
                "VALUES (?, ?, ?) RETURNING id",
                (self._site, fingerprint, device_id),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise RuntimeError("RETURNING produced no row")
            return int(row[0])

    async def get_by_alias(self, fingerprint: str) -> DeviceRow | None:
        """Return the device owning this alias fingerprint, if any.

        The row is the device's *founding-key* ``DeviceRow``, not a
        projection of the queried alias — ``row["fingerprint"]`` is
        ``devices.fingerprint``, which generally differs from the
        argument.
        """
        cur = await self._conn.execute(
            "SELECT d.id, d.site_id, d.fingerprint, d.address, "
            "d.label, d.description, d.created_at, d.updated_at "
            "FROM device_aliases AS a "
            "JOIN devices AS d ON d.id = a.device_id "
            "AND d.site_id = a.site_id "
            "WHERE a.site_id = ? AND a.fingerprint = ?",
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

    async def list_aliases(self, device_id: int) -> list[DeviceAliasRow]:
        """Return fused fingerprints bound to this device, oldest first."""
        cur = await self._conn.execute(
            "SELECT id, site_id, fingerprint, device_id, "
            "created_at, updated_at "
            "FROM device_aliases "
            "WHERE site_id = ? AND device_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (self._site, device_id),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [
            DeviceAliasRow(
                id=r[0],
                site_id=r[1],
                fingerprint=r[2],
                device_id=r[3],
                created_at=r[4],
                updated_at=r[5],
            )
            for r in rows
        ]

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

    async def set_label(
        self,
        device_id: int,
        *,
        label: str | None,
        actor: str,
    ) -> bool:
        """Set (or clear, ``label=None``) a device's label; audit it.

        Reads the previous label, updates the device, and appends a
        ``label_audit`` row (who/what/when) atomically. Returns ``False``
        if no device with that id exists for this site (a no-op, no
        audit row). ``actor`` identifies the operator making the change.
        """
        async with transaction(self._conn):
            cur = await self._conn.execute(
                "SELECT label FROM devices WHERE id = ? AND site_id = ?",
                (device_id, self._site),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                return False
            previous = row[0]
            await self._conn.execute(
                "UPDATE devices SET label = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ? AND site_id = ?",
                (label, device_id, self._site),
            )
            await self._conn.execute(
                "INSERT INTO label_audit "
                "(site_id, device_id, actor, previous_label, new_label) "
                "VALUES (?, ?, ?, ?, ?)",
                (self._site, device_id, actor, previous, label),
            )
            return True

    async def set_description(
        self,
        device_id: int,
        *,
        description: str | None,
    ) -> bool:
        """Set a device's free-text description.

        Not audited (``label_audit`` tracks labels only). Returns
        ``False`` if no device with that id exists for this site.
        """
        async with transaction(self._conn):
            cur = await self._conn.execute(
                "UPDATE devices SET description = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') "
                "WHERE id = ? AND site_id = ? RETURNING id",
                (description, device_id, self._site),
            )
            row = await cur.fetchone()
            await cur.close()
            return row is not None


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

    async def count_pending(self) -> int:
        """Return how many PENDING messages this site has (outbox depth)."""
        cur = await self._conn.execute(
            "SELECT COUNT(*) FROM outbox "
            "WHERE site_id = ? AND status = 'PENDING'",
            (self._site,),
        )
        row = await cur.fetchone()
        await cur.close()
        return int(row[0]) if row is not None else 0

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


class PresenceEventRepository:
    """Async repository for the ``presence_events`` table (P2-1).

    Append-only log of ABSENT/PRESENT transitions the presence state
    machine emits. The alert layer (P2-6) joins these to device labels
    to decide what warrants an operator alert; this repository only
    records them.
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
        event_type: str,
        occurred_at: str,
    ) -> int:
        """Record one presence transition; return its new id.

        ``event_type`` is ``"PRESENT"`` or ``"ABSENT"`` (the schema's
        CHECK constraint rejects anything else). ``occurred_at`` is the
        fixed-width UTC time of the scan window that produced it.
        """
        async with transaction(self._conn):
            cur = await self._conn.execute(
                "INSERT INTO presence_events "
                "(site_id, device_id, event_type, occurred_at) "
                "VALUES (?, ?, ?, ?) RETURNING id",
                (self._site, device_id, event_type, occurred_at),
            )
            row = await cur.fetchone()
            await cur.close()
            if row is None:
                raise RuntimeError("RETURNING produced no row")
            return int(row[0])

    async def list_for_device(self, device_id: int) -> list[PresenceEventRow]:
        """Return a device's transitions for this site, oldest first."""
        cur = await self._conn.execute(
            "SELECT id, site_id, device_id, event_type, occurred_at "
            "FROM presence_events WHERE site_id = ? AND device_id = ? "
            "ORDER BY occurred_at, id",
            (self._site, device_id),
        )
        rows = await cur.fetchall()
        await cur.close()
        return [
            PresenceEventRow(
                id=r[0],
                site_id=r[1],
                device_id=r[2],
                event_type=r[3],
                occurred_at=r[4],
            )
            for r in rows
        ]
