# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for OutboxRepository (P2-3): durable, ordered enqueue.

The outbox is the durability boundary — every outbound message is
written PENDING, synchronously with the event that produced it, before
any delivery attempt. These pin the DoD: alerts generated while
delivery is down are fully preserved and FIFO-ordered, and an enqueue is
atomic with its triggering transaction (never fire-and-forget). The
drain side (claim/backoff/status transitions) is P2-4, out of scope.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiosqlite
import pytest

from blesentry.storage import apply_migrations, connect
from blesentry.storage.database import transaction
from blesentry.storage.repository import OutboxRepository

SITE = "test-site"


class _TriggerFailed(RuntimeError):
    """Stand-in for a triggering event that fails after enqueue."""


@pytest.fixture
async def db() -> AsyncIterator[aiosqlite.Connection]:
    conn = await connect(":memory:")
    try:
        await apply_migrations(conn)
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def outbox(db: aiosqlite.Connection) -> OutboxRepository:
    return OutboxRepository(db, SITE)


async def test_enqueue_returns_id_and_persists_pending(
    outbox: OutboxRepository,
) -> None:
    outbox_id = await outbox.enqueue(payload="hello")
    assert outbox_id >= 1
    row = await outbox.get(outbox_id)
    assert row is not None
    assert row["payload"] == "hello"
    assert row["status"] == "PENDING"
    assert row["attempt_count"] == 0
    assert row["next_attempt_at"] is None
    assert row["last_error"] is None


async def test_list_pending_is_fifo(outbox: OutboxRepository) -> None:
    ids = [await outbox.enqueue(payload=f"m{i}") for i in range(3)]
    pending = await outbox.list_pending()
    assert [r["id"] for r in pending] == ids
    assert [r["payload"] for r in pending] == ["m0", "m1", "m2"]


async def test_outage_preserves_all_alerts_in_order(
    outbox: OutboxRepository,
) -> None:
    # Simulate a network outage: delivery is down, so alerts pile up in
    # the outbox. Every one must survive, in the order it was raised.
    payloads = [f"alert-{i:03d}" for i in range(50)]
    for payload in payloads:
        await outbox.enqueue(payload=payload)
    pending = await outbox.list_pending()
    assert [r["payload"] for r in pending] == payloads
    assert all(r["status"] == "PENDING" for r in pending)


async def test_enqueue_is_atomic_with_triggering_event(
    db: aiosqlite.Connection, outbox: OutboxRepository
) -> None:
    # "Synchronous with the triggering event": if the surrounding unit
    # of work rolls back, the enqueue rolls back with it — no
    # fire-and-forget row outlives a failed event.
    with pytest.raises(_TriggerFailed):
        async with transaction(db):
            await outbox.enqueue(payload="should-not-survive")
            raise _TriggerFailed
    assert await outbox.list_pending() == []


async def test_enqueue_and_list_are_site_scoped(
    db: aiosqlite.Connection,
) -> None:
    site_a = OutboxRepository(db, "site-a")
    site_b = OutboxRepository(db, "site-b")
    await site_a.enqueue(payload="for-a")
    assert [r["payload"] for r in await site_a.list_pending()] == ["for-a"]
    assert await site_b.list_pending() == []


@pytest.mark.parametrize(
    "payload",
    [
        "",  # NOT NULL permits empty; min_length is a notifier concern
        "a" * 5000,  # multi-KB
        'quote\'s and "double" and\nnewline\ttab',
        "unicode ☃ é 日本語 🚨",
    ],
)
async def test_enqueue_preserves_payload_verbatim(
    outbox: OutboxRepository, payload: str
) -> None:
    # "Fully preserved" means byte-exact through the parameterized
    # INSERT — no truncation, escaping, or mangling.
    outbox_id = await outbox.enqueue(payload=payload)
    row = await outbox.get(outbox_id)
    assert row is not None
    assert row["payload"] == payload


async def test_list_pending_excludes_non_pending(
    db: aiosqlite.Connection, outbox: OutboxRepository
) -> None:
    # P2-4 will produce DELIVERED/FAILED rows; list_pending must skip
    # them. Seed one directly since P2-3 only ever writes PENDING.
    pending_id = await outbox.enqueue(payload="still-queued")
    await db.execute(
        "INSERT INTO outbox (site_id, status, payload) "
        "VALUES (?, 'DELIVERED', ?)",
        (SITE, "already-sent"),
    )
    await db.commit()
    pending = await outbox.list_pending()
    assert [r["id"] for r in pending] == [pending_id]


async def test_interleaved_sites_keep_per_site_fifo(
    db: aiosqlite.Connection,
) -> None:
    site_a = OutboxRepository(db, "site-a")
    site_b = OutboxRepository(db, "site-b")
    await site_a.enqueue(payload="a0")
    await site_b.enqueue(payload="b0")
    await site_a.enqueue(payload="a1")
    await site_b.enqueue(payload="b1")
    assert [r["payload"] for r in await site_a.list_pending()] == ["a0", "a1"]
    assert [r["payload"] for r in await site_b.list_pending()] == ["b0", "b1"]


async def test_get_unknown_returns_none(outbox: OutboxRepository) -> None:
    assert await outbox.get(999) is None


async def test_get_is_site_scoped(db: aiosqlite.Connection) -> None:
    site_a = OutboxRepository(db, "site-a")
    site_b = OutboxRepository(db, "site-b")
    outbox_id = await site_a.enqueue(payload="x")
    assert await site_b.get(outbox_id) is None
