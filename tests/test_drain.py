# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the outbox drain loop (P2-4).

The drain delivers PENDING outbox messages through the Notifier seam in
strict FIFO order, backs off retriable failures (capped exponential,
jittered), dead-letters permanent/unusable ones, and never drops a
message on repeated failure. The DoD test simulates a multi-day outage
and asserts every message is delivered, in order, on reconnect.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiosqlite
import pytest

from blesentry.drain import (
    DEFAULT_BASE,
    DEFAULT_CAP,
    DrainResult,
    backoff_delay,
    drain_once,
    run_drain,
)
from blesentry.notifier.models import (
    DeliveryResult,
    InboundCommand,
    OutboundMessage,
)
from blesentry.storage import OutboxRepository, apply_migrations, connect

SITE = "test-site"


def _no_jitter(delay: float) -> float:
    return delay


def _payload(text: str) -> str:
    return OutboundMessage(text=text).model_dump_json()


class _Clock:
    """Manually advanced monotonic clock (epoch seconds)."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _FakeNotifier:
    """Notifier double with an offline/online toggle.

    Records the text of every successfully delivered message in order.
    Offline sends fail; ``retriable`` controls transient vs permanent.
    """

    def __init__(self, *, online: bool = True, retriable: bool = True) -> None:
        self.online = online
        self.retriable = retriable
        self.delivered: list[str] = []
        self.attempts = 0

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        self.attempts += 1
        if self.online:
            self.delivered.append(message.text)
            return DeliveryResult(ok=True, message_id=len(self.delivered))
        return DeliveryResult(
            ok=False, error="transport-error", retriable=self.retriable
        )

    async def commands(self) -> AsyncIterator[InboundCommand]:
        return
        yield

    async def aclose(self) -> None:
        pass


class _PickyNotifier:
    """Delivers everything except texts in ``blocked`` (retriable fail).

    Lets a test create *partial* connectivity: the head fails while a
    later message would succeed.
    """

    def __init__(self, blocked: set[str]) -> None:
        self.blocked = set(blocked)
        self.delivered: list[str] = []

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        if message.text in self.blocked:
            return DeliveryResult(ok=False, error="transient", retriable=True)
        self.delivered.append(message.text)
        return DeliveryResult(ok=True, message_id=len(self.delivered))

    async def commands(self) -> AsyncIterator[InboundCommand]:
        return
        yield

    async def aclose(self) -> None:
        pass


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


# --- backoff ---------------------------------------------------------


def test_backoff_grows_exponentially() -> None:
    delays = [
        backoff_delay(a, base=1.0, cap=1e9, jitter=_no_jitter)
        for a in range(5)
    ]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_backoff_is_capped() -> None:
    assert backoff_delay(20, base=1.0, cap=900.0, jitter=_no_jitter) == 900.0


def test_backoff_huge_attempt_returns_cap_without_overflow() -> None:
    # A multi-day outage drives attempt into the hundreds; float math
    # must not overflow.
    assert backoff_delay(10_000, base=1.0, cap=900.0, jitter=_no_jitter) == (
        900.0
    )


def test_backoff_default_jitter_stays_within_equal_jitter_bounds() -> None:
    for attempt in range(20):
        delay = backoff_delay(attempt)
        capped = min(DEFAULT_BASE * 2**attempt, DEFAULT_CAP)
        assert capped / 2 <= delay <= capped


# --- drain_once branches --------------------------------------------


async def test_drain_once_idle_when_empty(
    outbox: OutboxRepository,
) -> None:
    notifier = _FakeNotifier()
    assert await drain_once(outbox, notifier, now=_Clock()) is DrainResult.IDLE
    assert notifier.attempts == 0


async def test_drain_once_delivers_and_marks(
    outbox: OutboxRepository,
) -> None:
    outbox_id = await outbox.enqueue(payload=_payload("hi"))
    notifier = _FakeNotifier(online=True)
    result = await drain_once(outbox, notifier, now=_Clock())
    assert result is DrainResult.DELIVERED
    assert notifier.delivered == ["hi"]
    row = await outbox.get(outbox_id)
    assert row is not None and row["status"] == "DELIVERED"


async def test_drain_once_waits_when_head_not_due(
    outbox: OutboxRepository,
) -> None:
    outbox_id = await outbox.enqueue(payload=_payload("later"))
    clock = _Clock()
    # Push the head's next attempt into the future.
    await drain_once(
        outbox, _FakeNotifier(online=False), now=clock, jitter=_no_jitter
    )
    notifier = _FakeNotifier(online=True)
    # Same instant: head is not due yet → no send.
    assert (
        await drain_once(outbox, notifier, now=clock, jitter=_no_jitter)
        is DrainResult.WAITING
    )
    assert notifier.attempts == 0
    row = await outbox.get(outbox_id)
    assert row is not None and row["status"] == "PENDING"


async def test_drain_once_reschedules_retriable_failure(
    outbox: OutboxRepository,
) -> None:
    outbox_id = await outbox.enqueue(payload=_payload("x"))
    clock = _Clock()
    result = await drain_once(
        outbox,
        _FakeNotifier(online=False, retriable=True),
        now=clock,
        base=1.0,
        cap=900.0,
        jitter=_no_jitter,
    )
    assert result is DrainResult.RETRY_SCHEDULED
    row = await outbox.get(outbox_id)
    assert row is not None
    assert row["status"] == "PENDING"
    assert row["attempt_count"] == 1
    # next_attempt_at == now + backoff(attempt=0): the drain uses the
    # backoff function, tying scheduling to the asserted policy.
    from blesentry.loop import iso_utc

    expected = iso_utc(
        clock() + backoff_delay(0, base=1.0, cap=900.0, jitter=_no_jitter)
    )
    assert row["next_attempt_at"] == expected


async def test_drain_once_dead_letters_permanent_failure(
    outbox: OutboxRepository,
) -> None:
    dead = await outbox.enqueue(payload=_payload("blocked"))
    nxt = await outbox.enqueue(payload=_payload("next"))
    result = await drain_once(
        outbox,
        _FakeNotifier(online=False, retriable=False),
        now=_Clock(),
    )
    assert result is DrainResult.DEAD_LETTERED
    dead_row = await outbox.get(dead)
    assert dead_row is not None and dead_row["status"] == "FAILED"
    # The dead-letter does not block the message behind it.
    head = await outbox.head_pending()
    assert head is not None and head["id"] == nxt


async def test_drain_once_dead_letters_malformed_payload(
    outbox: OutboxRepository,
) -> None:
    bad = await outbox.enqueue(payload="not json at all")
    good = await outbox.enqueue(payload=_payload("fine"))
    result = await drain_once(outbox, _FakeNotifier(online=True), now=_Clock())
    assert result is DrainResult.DEAD_LETTERED
    bad_row = await outbox.get(bad)
    assert bad_row is not None and bad_row["status"] == "FAILED"
    head = await outbox.head_pending()
    assert head is not None and head["id"] == good


# --- DoD: multi-day outage, in-order on reconnect -------------------


async def test_three_day_outage_delivers_in_order_on_reconnect(
    outbox: OutboxRepository,
) -> None:
    texts = ["alert-0", "alert-1", "alert-2"]
    for text in texts:
        await outbox.enqueue(payload=_payload(text))
    clock = _Clock()
    notifier = _FakeNotifier(online=False)

    # Outage: retry the head across ~3 days. Advancing past the 15-min
    # cap each step guarantees the head is due, so every tick attempts.
    steps = (3 * 24 * 60) // 15  # 15-min cap over 3 days
    for _ in range(steps):
        clock.advance(1000.0)  # > cap, so the head is always due
        result = await drain_once(
            outbox, notifier, now=clock, jitter=_no_jitter
        )
        assert result is DrainResult.RETRY_SCHEDULED

    head = await outbox.head_pending()
    assert head is not None
    assert head["payload"] == _payload("alert-0")  # nothing dropped
    assert head["attempt_count"] == steps
    assert notifier.delivered == []  # nothing delivered during outage

    # Reconnect: drain until empty.
    notifier.online = True
    for _ in range(len(texts)):
        clock.advance(1000.0)
        assert (
            await drain_once(outbox, notifier, now=clock, jitter=_no_jitter)
            is DrainResult.DELIVERED
        )

    assert notifier.delivered == texts  # every message, in order
    assert await outbox.head_pending() is None
    assert await outbox.list_pending() == []


async def test_head_of_line_preserves_order_under_partial_connectivity(
    outbox: OutboxRepository,
) -> None:
    await outbox.enqueue(payload=_payload("first"))
    await outbox.enqueue(payload=_payload("second"))
    clock = _Clock()
    # "second" would succeed, but the head "first" fails.
    notifier = _PickyNotifier(blocked={"first"})
    assert (
        await drain_once(outbox, notifier, now=clock, jitter=_no_jitter)
        is DrainResult.RETRY_SCHEDULED
    )
    # Head-of-line: "second" is never attempted ahead of "first".
    assert notifier.delivered == []

    notifier.blocked.clear()  # channel fully back
    clock.advance(1000.0)
    assert (
        await drain_once(outbox, notifier, now=clock, jitter=_no_jitter)
        is DrainResult.DELIVERED
    )
    assert (
        await drain_once(outbox, notifier, now=clock, jitter=_no_jitter)
        is DrainResult.DELIVERED
    )
    assert notifier.delivered == ["first", "second"]


# --- run_drain -------------------------------------------------------


async def test_run_drain_stops_at_max_ticks(
    outbox: OutboxRepository,
) -> None:
    sleeps: list[float] = []

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    ticks = await run_drain(
        outbox,
        _FakeNotifier(online=True),
        poll=5.0,
        max_ticks=3,
        now=_Clock(),
        sleep=_sleep,
        jitter=_no_jitter,
    )
    assert ticks == 3
    assert sleeps == [5.0, 5.0]  # between ticks, not after the last


async def test_run_drain_delivers_all_pending(
    outbox: OutboxRepository,
) -> None:
    texts = ["a0", "a1", "a2"]
    for text in texts:
        await outbox.enqueue(payload=_payload(text))

    async def _sleep(seconds: float) -> None:
        return None

    notifier = _FakeNotifier(online=True)
    await run_drain(
        outbox,
        notifier,
        poll=0.0,
        max_ticks=3,
        now=_Clock(),
        sleep=_sleep,
        jitter=_no_jitter,
    )
    assert notifier.delivered == texts
    assert await outbox.head_pending() is None
