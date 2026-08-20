# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Outbox drain loop (P2-4): deliver queued messages, with backoff.

An async task that empties the ``outbox`` through the Notifier seam.
Its guarantees, from the roadmap:

* **Strict FIFO.** It always works the queue *head* (the oldest PENDING
  message) and never delivers a later message before an earlier one.
* **Never drops.** A retriable failure reschedules the head with a
  capped, jittered exponential backoff and leaves it PENDING — it keeps
  retrying across a multi-day outage. A *permanent* failure (or an
  unusable payload) is dead-lettered to FAILED so it can never block
  the messages behind it — the one sanctioned way a message leaves the
  queue without being delivered.
* **In order on reconnect.** Because it is strictly head-first, once
  the channel is back every message drains in the order it was enqueued.

Delivery goes through the outbox (ADR-0003): the message is already
durably written (P2-3); the drain reads ``payload`` as a serialized
:class:`~blesentry.notifier.models.OutboundMessage`. Terminal rows are
*marked* (DELIVERED / FAILED), never deleted, so ids stay monotonic and
ordering holds; reclaiming terminal rows is a separate retention pass.

Wiring contract — the daemon integration (a follow-up) MUST honor this:

* **Dedicated connection.** The drain's :class:`OutboxRepository` runs
  on its *own* aiosqlite connection, never the scan loop's. The
  ``transaction()`` nesting guard is connection-global, so two tasks
  sharing one connection interleave BEGIN/COMMIT and corrupt each
  other's units of work (the #91 hazard).
* **Fail loud.** Like ``run_loop``, an unexpected error propagates out
  of ``run_drain``; the supervisor (systemd, P3-1) restarts the process
  rather than letting delivery die silently while scanning continues.
* **At-least-once.** A crash between a successful ``send`` and the
  ``mark_delivered`` commit re-sends on restart — a duplicate alert,
  never a lost one (the right trade for a sentinel). The schema's
  IN_FLIGHT status is unused: a single-task drain needs no claim/lease.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from enum import Enum

from pydantic import ValidationError

from blesentry.loop import iso_utc
from blesentry.notifier.models import OutboundMessage
from blesentry.notifier.protocol import Notifier
from blesentry.storage.repository import OutboxRepository

logger = logging.getLogger(__name__)

# Backoff defaults: 1s, doubling, capped at 15 minutes (roadmap P2-4).
DEFAULT_BASE = 1.0
DEFAULT_CAP = 900.0
# Poll cadence when idle or waiting on a backing-off head.
DEFAULT_POLL = 30.0

# After this many consecutive failures the head is treated as wedged and
# logged loudly each retry — nothing is dropped ("never drops"), but a
# stalled delivery channel becomes observable rather than silent.
WEDGED_ATTEMPTS = 10

# Beyond this attempt the exponential has long since saturated the cap;
# the guard keeps ``2 ** attempt`` from overflowing float on a
# months-long outage where attempt reaches the thousands.
_MAX_EXP_ATTEMPT = 64

__all__ = [
    "DEFAULT_BASE",
    "DEFAULT_CAP",
    "DEFAULT_POLL",
    "WEDGED_ATTEMPTS",
    "DrainResult",
    "backoff_delay",
    "drain_once",
    "run_drain",
]


class DrainResult(Enum):
    """The outcome of one :func:`drain_once` tick."""

    IDLE = "idle"  # nothing PENDING
    WAITING = "waiting"  # head not due yet (backing off)
    DELIVERED = "delivered"  # head delivered, marked DELIVERED
    RETRY_SCHEDULED = "retry_scheduled"  # retriable failure, backed off
    DEAD_LETTERED = "dead_lettered"  # permanent failure/bad payload → FAILED


def _equal_jitter(delay: float) -> float:
    """Equal jitter: keep half the delay, randomise the other half.

    Spreads retries without letting the effective wait collapse toward
    zero (full jitter can), so a wedged channel is not hammered.
    """
    return delay / 2 + random.uniform(0, delay / 2)


def backoff_delay(
    attempt: int,
    *,
    base: float = DEFAULT_BASE,
    cap: float = DEFAULT_CAP,
    jitter: Callable[[float], float] = _equal_jitter,
) -> float:
    """Return the backoff for a 0-based *attempt*, capped and jittered.

    ``base * 2**attempt`` clamped to ``cap``, then passed through
    *jitter*. The attempt guard means an arbitrarily long outage can
    never overflow the exponential.
    """
    attempt = max(attempt, 0)
    if attempt >= _MAX_EXP_ATTEMPT:
        capped = cap
    else:
        capped = min(base * (2**attempt), cap)
    return jitter(capped)


async def drain_once(
    outbox: OutboxRepository,
    notifier: Notifier,
    *,
    now: Callable[[], float] = time.time,
    base: float = DEFAULT_BASE,
    cap: float = DEFAULT_CAP,
    jitter: Callable[[float], float] = _equal_jitter,
) -> DrainResult:
    """Attempt to deliver the queue head once; return what happened.

    Args:
        outbox: The site's outbox repository.
        notifier: The Notifier to deliver through.
        now: Clock returning epoch seconds (injectable for tests).
        base: Backoff base seconds.
        cap: Backoff ceiling seconds.
        jitter: Backoff jitter function.

    Returns:
        A :class:`DrainResult` describing the tick.
    """
    head = await outbox.head_pending()
    if head is None:
        return DrainResult.IDLE

    now_epoch = now()
    due = head["next_attempt_at"]
    # Fixed-width UTC strings compare lexically (docs/schema.md).
    if due is not None and due > iso_utc(now_epoch):
        return DrainResult.WAITING

    try:
        message = OutboundMessage.model_validate_json(head["payload"])
    except ValidationError:
        logger.warning(
            "outbox %s: unusable payload, dead-lettering", head["id"]
        )
        await outbox.mark_failed(head["id"], "malformed-payload")
        return DrainResult.DEAD_LETTERED

    result = await notifier.send(message)
    if result.ok:
        await outbox.mark_delivered(head["id"])
        return DrainResult.DELIVERED

    if not result.retriable:
        await outbox.mark_failed(
            head["id"], result.error or "permanent-failure"
        )
        return DrainResult.DEAD_LETTERED

    delay = backoff_delay(
        head["attempt_count"], base=base, cap=cap, jitter=jitter
    )
    attempts = head["attempt_count"] + 1
    if attempts >= WEDGED_ATTEMPTS:
        # Head-of-line: this message blocks everything behind it. We
        # never drop it, but a wedged head means alerting is stalled —
        # surface it loudly so a growing backlog is not silent.
        logger.warning(
            "outbox %s wedged: %d consecutive failures, delivery stalled",
            head["id"],
            attempts,
        )
    await outbox.reschedule(
        head["id"],
        next_attempt_at=iso_utc(now_epoch + delay),
        error=result.error or "transient-failure",
    )
    return DrainResult.RETRY_SCHEDULED


async def run_drain(
    outbox: OutboxRepository,
    notifier: Notifier,
    *,
    poll: float = DEFAULT_POLL,
    max_ticks: int | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    base: float = DEFAULT_BASE,
    cap: float = DEFAULT_CAP,
    jitter: Callable[[float], float] = _equal_jitter,
) -> int:
    """Drain continuously: attempt the head, pause only when idle.

    Runs until the task is cancelled (SIGTERM) or ``max_ticks`` ticks
    have run (bounded runs are for tests, mirroring ``run_loop``).

    Pausing is result-driven: after a DELIVERED or DEAD_LETTERED tick
    the *next* message is now the head and may be due immediately, so we
    loop straight on — a reconnect backlog drains back-to-back rather
    than one message per ``poll``. We sleep ``poll`` only when there is
    nothing to do right now: an empty queue (IDLE), a backing-off head
    (WAITING), or a just-scheduled retry (RETRY_SCHEDULED).

    ``outbox`` must be built on a connection dedicated to this task (see
    the module docstring's wiring contract). An unexpected error
    propagates (fail-loud, like ``run_loop``) — the daemon should
    supervise the scan and drain tasks together and treat either's death
    as fatal.

    Returns:
        The number of ticks completed.
    """
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        result = await drain_once(
            outbox, notifier, now=now, base=base, cap=cap, jitter=jitter
        )
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        if result in (DrainResult.DELIVERED, DrainResult.DEAD_LETTERED):
            continue  # more may be due now — drain the backlog
        await sleep(poll)
    return ticks
