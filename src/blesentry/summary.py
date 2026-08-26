# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Daily summary (P2-9): scheduled digest through the outbox.

Once per UTC day, at or after ``hour_utc``, a digest of devices seen,
new devices, presence transitions, and outbox health is enqueued as a
plain ``OutboundMessage``. A ``site_state`` marker
(``daily_summary.last_sent``) is written in the same transaction, so a
restart cannot double-send for that UTC day. A WAN outage delays
delivery via the existing drain; nothing is dropped.

The digest names devices by id and operator label only — no addresses,
fingerprints, or advertisement payloads (SECURITY.md).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import NamedTuple

from blesentry.loop import iso_utc
from blesentry.notifier.models import OutboundMessage
from blesentry.storage.database import transaction
from blesentry.storage.repository import (
    DeviceRepository,
    DeviceRow,
    ObservationRepository,
    OutboxRepository,
    PresenceEventRepository,
    SiteStateRepository,
)

logger = logging.getLogger(__name__)

LAST_SENT_KEY = "daily_summary.last_sent"
DEFAULT_POLL = 60.0
_DAY = 86400.0
_LIST_CAP = 20

__all__ = [
    "DEFAULT_POLL",
    "LAST_SENT_KEY",
    "SummaryDeps",
    "is_due",
    "render_summary",
    "run_summary_loop",
    "tick",
]


class SummaryDeps(NamedTuple):
    """Everything one summary tick needs, on a dedicated connection."""

    devices: DeviceRepository
    observations: ObservationRepository
    presence_events: PresenceEventRepository
    outbox: OutboxRepository
    state: SiteStateRepository
    now: Callable[[], float]
    hour_utc: int
    enabled: bool


def is_due(now: float, *, hour_utc: int, last_sent: str | None) -> bool:
    """Return whether a digest should fire at ``now``.

    Due when the UTC hour has reached ``hour_utc`` and ``last_sent`` is
    either missing or on a previous UTC calendar day. ``last_sent`` is
    the schema's fixed-width UTC timestamp (or ``None``).
    """
    dt = datetime.fromtimestamp(now, tz=UTC)
    if dt.hour < hour_utc:
        return False
    if last_sent is None:
        return True
    last_dt = datetime.fromisoformat(last_sent)
    return dt.date() != last_dt.date()


def _one_line(text: str) -> str:
    """Collapse whitespace so a label cannot forge digest rows."""
    return " ".join(text.split())


def _device_line(device: DeviceRow) -> str:
    label = _one_line(device["label"]) if device["label"] else "(unlabeled)"
    return f"  #{device['id']} {label}"


def _capped(lines: list[str], *, total: int) -> list[str]:
    if total <= _LIST_CAP:
        return lines
    extra = total - _LIST_CAP
    return [*lines[:_LIST_CAP], f"  …and {extra} more"]


def _header(sent_at: float) -> str:
    dt = datetime.fromtimestamp(sent_at, tz=UTC)
    return f"daily summary {dt.strftime('%Y-%m-%d %H:%MZ')}"


async def render_summary(
    devices: DeviceRepository,
    presence_events: PresenceEventRepository,
    outbox: OutboxRepository,
    *,
    window_start: str,
    window_end: str,
    sent_at: float,
) -> str:
    """Compose the digest text for ``[window_start, window_end)``.

    Pending/failed counts are a *current* snapshot, not windowed.
    """
    seen = await devices.list_observed_between(window_start, window_end)
    created = await devices.list_created_between(window_start, window_end)
    events = await presence_events.list_between(window_start, window_end)
    pending = await outbox.count_pending()
    failed = await outbox.count_failed()

    present_n = sum(1 for e in events if e["event_type"] == "PRESENT")
    absent_n = sum(1 for e in events if e["event_type"] == "ABSENT")

    lines = [
        _header(sent_at),
        f"window: {window_start} .. {window_end}",
        f"devices seen: {len(seen)}",
        *_capped([_device_line(d) for d in seen], total=len(seen)),
        f"new devices: {len(created)}",
        *_capped([_device_line(d) for d in created], total=len(created)),
        f"presence: {present_n} PRESENT, {absent_n} ABSENT",
    ]
    roster = [
        f"  #{event['device_id']} {event['event_type']}"
        for event in events[:_LIST_CAP]
    ]
    if len(events) > _LIST_CAP:
        roster.append(f"  …and {len(events) - _LIST_CAP} more")
    lines.extend(roster)
    lines.append(f"outbox: {pending} pending, {failed} failed")
    return "\n".join(lines)


def _require_affinity(deps: SummaryDeps) -> None:
    conn = deps.devices.connection
    if deps.observations.connection is not conn:
        raise ValueError("summary repos must share one connection")
    if deps.presence_events.connection is not conn:
        raise ValueError("summary repos must share one connection")
    if deps.outbox.connection is not conn:
        raise ValueError("summary repos must share one connection")
    if deps.state.connection is not conn:
        raise ValueError("summary repos must share one connection")


async def tick(deps: SummaryDeps) -> bool:
    """Enqueue today's digest if due; persist the last-sent marker.

    Returns True when a digest was enqueued. The enqueue and the marker
    share one transaction (join an ambient one if the caller opened it).
    """
    if not deps.enabled:
        return False
    _require_affinity(deps)
    now = deps.now()
    last = await deps.state.get(LAST_SENT_KEY)
    if not is_due(now, hour_utc=deps.hour_utc, last_sent=last):
        return False

    async with transaction(deps.devices.connection):
        last = await deps.state.get(LAST_SENT_KEY)
        if not is_due(now, hour_utc=deps.hour_utc, last_sent=last):
            return False
        end = iso_utc(now)
        start = last if last is not None else iso_utc(now - _DAY)
        text = await render_summary(
            deps.devices,
            deps.presence_events,
            deps.outbox,
            window_start=start,
            window_end=end,
            sent_at=now,
        )
        await deps.outbox.enqueue(
            payload=OutboundMessage(text=text).model_dump_json()
        )
        await deps.state.set(LAST_SENT_KEY, end)
    logger.info("enqueued daily summary")
    return True


async def run_summary_loop(
    deps: SummaryDeps,
    *,
    poll: float = DEFAULT_POLL,
    max_ticks: int | None = None,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> int:
    """Poll until cancelled (or ``max_ticks``, for tests).

    ``deps`` must be built on a connection dedicated to this task
    (#91). Unexpected errors propagate (fail-loud, like ``run_loop``).

    Returns:
        The number of ticks completed.
    """
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        await tick(deps)
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        await sleep(poll)
    return ticks
