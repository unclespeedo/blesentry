# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Inside own-gear exclusion tests (I2 / #137).

Pins stable-fixture and rotating-address operator gear subtraction
before adjacent counting; strangers still count.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiosqlite
import pytest

from blesentry.detection.familiar import FamiliarSet, build_familiar_device_ids
from blesentry.detection.inside import (
    build_inside_excluded,
    build_own_rotating_gear_device_ids,
    inside_count,
)
from blesentry.storage import apply_migrations, connect
from blesentry.storage.repository import (
    DeviceRepository,
    ObservationRepository,
)

SITE = "test-site"
OWN_STABLE_FP = "fp-own-stable-fixture"
OWN_STABLE_ADDR = "AA:BB:00:00:00:0A"
OWN_ROTATING_FP = "fp-own-rotating-shard"
OWN_ROTATING_ADDR = "43:22:33:44:55:66"
LABELED_ANCHOR_FP = "fp-labeled-anchor"
LABELED_ANCHOR_ADDR = "5E:11:22:33:44:55"
STRANGER_FP = "fp-stranger"
STRANGER_ADDR = "AA:BB:00:00:00:0B"


def _day(day: int, hour: int = 12) -> str:
    return f"2026-01-{day:02d}T{hour:02d}:00:00.000000Z"


@pytest.fixture
async def db() -> AsyncIterator[aiosqlite.Connection]:
    conn = await connect(":memory:")
    try:
        await apply_migrations(conn)
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def devices(db: aiosqlite.Connection) -> DeviceRepository:
    return DeviceRepository(db, SITE)


@pytest.fixture
def observations(db: aiosqlite.Connection) -> ObservationRepository:
    return ObservationRepository(db, SITE)


async def _observe(
    observations: ObservationRepository,
    device_id: int,
    when: str,
    *,
    rssi: int = -55,
    address_type: str | None = None,
) -> None:
    await observations.append(
        device_id=device_id,
        rssi=rssi,
        observed_at=when,
        address_type=address_type,
    )


async def _observe_days(
    observations: ObservationRepository,
    device_id: int,
    days: tuple[int, ...],
    *,
    rssi: int = -55,
    address_type: str | None = None,
) -> None:
    for day in days:
        await _observe(
            observations,
            device_id,
            _day(day),
            rssi=rssi,
            address_type=address_type,
        )


def test_build_inside_excluded_unions_familiar_and_own_rotating() -> None:
    familiar = FamiliarSet(frozenset({1}))
    own_rotating = frozenset({2})
    heard = {1: -55, 2: -54, 3: -53}
    excluded = build_inside_excluded(
        heard,
        familiar=familiar,
        own_rotating_gear=own_rotating,
    )
    assert excluded == frozenset({1, 2})
    assert inside_count(heard, excluded=excluded) == 1


@pytest.mark.asyncio
async def test_own_stable_fixture_does_not_trigger_inside_count(
    devices: DeviceRepository,
    observations: ObservationRepository,
) -> None:
    stable_id = await devices.upsert(
        fingerprint=OWN_STABLE_FP,
        address=OWN_STABLE_ADDR,
    )
    stranger_id = await devices.upsert(
        fingerprint=STRANGER_FP,
        address=STRANGER_ADDR,
    )
    await _observe_days(observations, stable_id, (1, 2, 3))
    familiar_ids = await build_familiar_device_ids(devices, observations)
    familiar = FamiliarSet(familiar_ids)
    own_rotating = await build_own_rotating_gear_device_ids(
        devices,
        observations,
    )
    heard = {stable_id: -55, stranger_id: -54}
    excluded = build_inside_excluded(
        heard,
        familiar=familiar,
        own_rotating_gear=own_rotating,
    )
    assert inside_count(heard, excluded=excluded) == 1


@pytest.mark.asyncio
async def test_own_rotating_address_device_does_not_trigger_inside_count(
    devices: DeviceRepository,
    observations: ObservationRepository,
) -> None:
    anchor_id = await devices.upsert(
        fingerprint=LABELED_ANCHOR_FP,
        address=LABELED_ANCHOR_ADDR,
    )
    await devices.set_label(anchor_id, label="Operator phone", actor="op")
    rotating_id = await devices.upsert(
        fingerprint=OWN_ROTATING_FP,
        address=OWN_ROTATING_ADDR,
    )
    await _observe(
        observations,
        anchor_id,
        _day(1),
        rssi=-60,
        address_type="rpa",
    )
    await _observe(
        observations,
        rotating_id,
        _day(1, 13),
        rssi=-55,
        address_type="rpa",
    )
    stranger_id = await devices.upsert(
        fingerprint=STRANGER_FP,
        address=STRANGER_ADDR,
    )
    familiar = FamiliarSet(
        await build_familiar_device_ids(devices, observations)
    )
    own_rotating = await build_own_rotating_gear_device_ids(
        devices,
        observations,
    )
    assert rotating_id in own_rotating
    assert not familiar.is_familiar(rotating_id)
    heard = {rotating_id: -55, stranger_id: -54}
    excluded = build_inside_excluded(
        heard,
        familiar=familiar,
        own_rotating_gear=own_rotating,
    )
    assert inside_count(heard, excluded=excluded) == 1


@pytest.mark.asyncio
async def test_unrelated_rpa_stranger_not_own_rotating_gear(
    devices: DeviceRepository,
    observations: ObservationRepository,
) -> None:
    anchor_id = await devices.upsert(
        fingerprint=LABELED_ANCHOR_FP,
        address=LABELED_ANCHOR_ADDR,
    )
    await devices.set_label(anchor_id, label="Operator phone", actor="op")
    stranger_id = await devices.upsert(
        fingerprint=STRANGER_FP,
        address=STRANGER_ADDR,
    )
    await _observe(
        observations,
        anchor_id,
        _day(1),
        address_type="rpa",
    )
    await _observe(
        observations,
        stranger_id,
        _day(2),
        address_type="rpa",
    )
    own_rotating = await build_own_rotating_gear_device_ids(
        devices,
        observations,
    )
    assert stranger_id not in own_rotating


@pytest.mark.asyncio
async def test_null_provenance_shard_counts_as_rotating(
    devices: DeviceRepository,
    observations: ObservationRepository,
) -> None:
    """CoreBluetooth / legacy store address_type NULL — still exclude."""
    anchor_id = await devices.upsert(
        fingerprint=LABELED_ANCHOR_FP,
        address=LABELED_ANCHOR_ADDR,
    )
    await devices.set_label(anchor_id, label="Operator phone", actor="op")
    rotating_id = await devices.upsert(
        fingerprint=OWN_ROTATING_FP,
        address=OWN_ROTATING_ADDR,
    )
    await _observe(observations, anchor_id, _day(1), rssi=-60)
    await _observe(
        observations,
        rotating_id,
        _day(1, 13),
        rssi=-55,
        address_type=None,
    )
    own_rotating = await build_own_rotating_gear_device_ids(
        devices,
        observations,
    )
    assert rotating_id in own_rotating


@pytest.mark.asyncio
async def test_known_stable_coobserved_not_own_rotating(
    devices: DeviceRepository,
    observations: ObservationRepository,
) -> None:
    """Known-stable types stay out of the rotating-shard set."""
    anchor_id = await devices.upsert(
        fingerprint=LABELED_ANCHOR_FP,
        address=LABELED_ANCHOR_ADDR,
    )
    await devices.set_label(anchor_id, label="Operator phone", actor="op")
    stable_id = await devices.upsert(
        fingerprint=OWN_STABLE_FP,
        address=OWN_STABLE_ADDR,
    )
    await _observe(
        observations,
        anchor_id,
        _day(1),
        address_type="rpa",
    )
    await _observe(
        observations,
        stable_id,
        _day(1, 13),
        address_type="random_static",
    )
    own_rotating = await build_own_rotating_gear_device_ids(
        devices,
        observations,
    )
    assert stable_id not in own_rotating
