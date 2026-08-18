# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Continuous scan loop (P1-8): scan window -> resolve -> persist.

The resolver here is PROVISIONAL: identity is the exact canonical
serialization of ``Fingerprint.from_advertisement()``, so a device
that rotates its MAC becomes a new ``devices`` row. #19 (P1-7)
replaces this with fingerprint fusion; the inflation in the meantime
is expected and is itself useful ground truth for that design.

Error contract (ADR-0002): scanner failures propagate out of the loop
— the process exits non-zero and the supervisor (systemd, P3-1)
restarts it. A sentinel that cannot scan must never look like a quiet
site.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import NamedTuple

from blesentry.scanner.models import Fingerprint
from blesentry.scanner.protocol import Scanner
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


def fingerprint_key(fingerprint: Fingerprint) -> str:
    """Canonical, deterministic string form of a Fingerprint.

    Sorted containers and sorted JSON keys so equal fingerprints from
    different capture passes serialize identically. This string is the
    provisional ``devices.fingerprint`` identity key (see module note).
    """
    return json.dumps(
        {
            "v": 2,
            "address": fingerprint.address,
            "service_uuids": sorted(fingerprint.service_uuids),
            "manufacturer_data": sorted(fingerprint.manufacturer_data),
            "local_name": fingerprint.local_name,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


async def run_cycle(
    scanner: Scanner,
    devices: DeviceRepository,
    observations: ObservationRepository,
    duration: float,
) -> CycleStats:
    """Run one scan window and persist everything heard."""
    advertisements = await scanner.scan(duration=duration)
    device_ids: set[int] = set()
    persisted = 0
    for ad in advertisements:
        key = fingerprint_key(Fingerprint.from_advertisement(ad))
        device_id = await devices.upsert(fingerprint=key, address=ad.address)
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

    Returns:
        Number of cycles completed.
    """
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        stats = await run_cycle(scanner, devices, observations, duration)
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
