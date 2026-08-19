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
from datetime import UTC, datetime
from typing import NamedTuple

from blesentry.resolver import DeviceResolver, fingerprint_key
from blesentry.scanner.protocol import Scanner
from blesentry.storage.database import transaction
from blesentry.storage.repository import (
    DeviceRepository,
    ObservationRepository,
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
    """
    advertisements = await scanner.scan(duration=duration)
    if observations.connection is not devices.connection:
        raise ValueError(
            "repositories must share one connection for cycle atomicity"
        )
    r = resolver if resolver is not None else DeviceResolver(devices)
    device_ids: set[int] = set()
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
                persisted += 1
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

    Returns:
        Number of cycles completed.
    """
    cycles = 0
    if resolver is None:
        resolver = DeviceResolver(devices)
    await resolver.seed()
    while max_cycles is None or cycles < max_cycles:
        stats = await run_cycle(
            scanner, devices, observations, duration, resolver
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
