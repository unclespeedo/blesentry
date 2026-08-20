# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Continuous scan loop (P1-8): scan window -> resolve -> persist.

Identity comes from the fusion resolver (#19, ``resolver.py``):
weighted signal scoring with a stable-address mismatch veto,
conservative by design — see resolver.py and docs/risks.md for the
fusion limits (payload-variance under-joining, impersonation
surface, window bounds).

Error contract (ADR-0002): scanner failures propagate out of the loop
— the process exits non-zero and the supervisor (systemd, P3-1)
restarts it. A sentinel that cannot scan must never look like a quiet
site.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NamedTuple

from blesentry.presence import PresenceTracker
from blesentry.resolver import DeviceResolver, fingerprint_key
from blesentry.scanner.protocol import Scanner
from blesentry.storage.database import transaction
from blesentry.storage.repository import (
    DeviceRepository,
    ObservationRepository,
    PresenceEventRepository,
)

logger = logging.getLogger(__name__)


class CycleStats(NamedTuple):
    """Summary of one scan cycle."""

    heard: int
    devices: int
    observations: int


def iso_utc(timestamp: float) -> str:
    """Epoch seconds -> the schema's fixed-width UTC format.

    ``docs/schema.md``: ``%Y-%m-%dT%H:%M:%fZ`` with millisecond
    precision, lexically sortable.
    """
    dt = datetime.fromtimestamp(timestamp, tz=UTC)
    millis = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


__all__ = ["CycleStats", "fingerprint_key", "iso_utc", "run_cycle", "run_loop"]


async def run_cycle(
    scanner: Scanner,
    devices: DeviceRepository,
    observations: ObservationRepository,
    duration: float,
    resolver: DeviceResolver | None = None,
    *,
    presence: PresenceTracker | None = None,
    presence_events: PresenceEventRepository | None = None,
    now: Callable[[], float] = time.time,
) -> CycleStats:
    """Run one scan window and persist everything heard atomically.

    The whole cycle commits in ONE transaction (#84): on process
    death at most one cycle (~15s) of observations is lost — at or
    inside the durability trade already accepted by docs/schema.md
    (power loss can additionally lose commits since the last WAL
    checkpoint, as schema.md documents).

    Identity comes from the fusion resolver (#19). Its staged ids
    publish only after COMMIT and are discarded on failure — a
    rolled-back cycle can never poison resolver memory (the #84
    lesson). Pass one resolver across cycles (run_loop does) or
    rotation fusion resets every call.

    Presence (P2-1): when a ``presence`` tracker and ``presence_events``
    repository are given (both or neither), each window's per-device
    best RSSI advances the tracker and its ABSENT/PRESENT transitions
    are written to ``presence_events`` *inside the cycle transaction* —
    a transition and the observations that caused it commit together.

    The tracker is in-memory and ``update`` mutates it before COMMIT
    (unlike the resolver, which stages). This is safe ONLY because the
    contract is fail-loud: a rolled-back cycle re-raises out of
    ``run_loop``, the process exits, and it restarts with a fresh
    tracker — it never continues from a half-applied window. A caller
    that catches ``run_cycle`` exceptions to keep scanning MUST discard
    the tracker, or a transition emitted in memory but rolled back in
    the DB would be lost. On a fresh start the tracker is empty (not
    seeded), so a device that was PRESENT before a restart re-emits
    PRESENT once it re-clears ``appear_windows`` — an at-least-once
    duplicate the alert layer (P2-6) must tolerate; seeding is a
    follow-up (#112).
    """
    advertisements = await scanner.scan(duration=duration)
    if observations.connection is not devices.connection:
        raise ValueError(
            "repositories must share one connection for cycle atomicity"
        )
    if (presence is None) != (presence_events is None):
        # Fail loud rather than silently tracking nothing (ADR-0002).
        raise ValueError("presence and presence_events must be given together")
    if (
        presence_events is not None
        and presence_events.connection is not devices.connection
    ):
        raise ValueError(
            "presence_events must share the cycle connection for atomicity"
        )
    r = resolver if resolver is not None else DeviceResolver(devices)
    device_ids: set[int] = set()
    heard: dict[int, int] = {}
    persisted = 0
    try:
        async with transaction(devices.connection):
            for ad in advertisements:
                device_id = await r.resolve(ad)
                await observations.append(
                    device_id=device_id,
                    rssi=ad.rssi,
                    observed_at=iso_utc(ad.timestamp),
                    adapter_id=ad.adapter_id,
                    address_type=ad.address_type,
                    adv_type=ad.adv_type,
                )
                device_ids.add(device_id)
                if device_id not in heard or ad.rssi > heard[device_id]:
                    heard[device_id] = ad.rssi
                persisted += 1
            if presence is not None and presence_events is not None:
                occurred_at = iso_utc(now())
                for transition in presence.update(heard):
                    await presence_events.append(
                        device_id=transition.device_id,
                        event_type=transition.state.value,
                        occurred_at=occurred_at,
                    )
    except BaseException:
        r.abort()
        raise
    r.commit()
    return CycleStats(
        heard=len(advertisements),
        devices=len(device_ids),
        observations=persisted,
    )


async def run_loop(
    scanner: Scanner,
    devices: DeviceRepository,
    observations: ObservationRepository,
    *,
    duration: float,
    pause: float,
    max_cycles: int | None = None,
    resolver: DeviceResolver | None = None,
    presence: PresenceTracker | None = None,
    presence_events: PresenceEventRepository | None = None,
    now: Callable[[], float] = time.time,
) -> int:
    """Scan continuously: window, persist, pause, repeat.

    Args:
        scanner: Any Scanner implementation.
        devices: Device repository (site-scoped).
        observations: Observation repository (same site).
        duration: Scan window length in seconds.
        pause: Sleep between windows in seconds.
        max_cycles: Stop after this many cycles (None = forever).
            Bounded runs are for tests and timeboxed captures.
        resolver: Pre-built resolver — the seam through which config
            (P1-9) supplies tuned thresholds. Must be built over the
            same ``devices`` repo. ``None`` builds a default resolver.
        presence: Pre-built presence tracker, carried across cycles
            (P2-1). ``None`` disables presence tracking.
        presence_events: Repository for presence transitions; required
            when ``presence`` is given, on the same connection.
        now: Clock for stamping presence transitions (injectable).

    Returns:
        Number of cycles completed.
    """
    cycles = 0
    if resolver is None:
        resolver = DeviceResolver(devices)
    await resolver.seed()
    while max_cycles is None or cycles < max_cycles:
        stats = await run_cycle(
            scanner,
            devices,
            observations,
            duration,
            resolver,
            presence=presence,
            presence_events=presence_events,
            now=now,
        )
        cycles += 1
        logger.info(
            "cycle %d: heard=%d devices=%d observations=%d",
            cycles,
            stats.heard,
            stats.devices,
            stats.observations,
        )
        if max_cycles is not None and cycles >= max_cycles:
            break
        await asyncio.sleep(pause)
    return cycles
