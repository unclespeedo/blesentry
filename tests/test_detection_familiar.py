# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Familiar-set tests (F6 / #125).

Pins K-day auto-learn, labeled bypass, cap, and refresh cadence.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiosqlite
import pytest

from blesentry.detection.familiar import (
    FAMILIAR_MAX_DEVICES,
    FAMILIAR_MIN_DAYS,
    FamiliarSet,
    FamiliarSetRefresher,
    build_familiar_device_ids,
)
from blesentry.storage import apply_migrations, connect
from blesentry.storage.repository import (
    DeviceRepository,
    ObservationRepository,
    SiteStateRepository,
)

SITE = "test-site"
STABLE_FP = "fp-stable-fixture"
STABLE_ADDR = "AA:BB:00:00:00:0A"
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


async def _observe_days(
    devices: DeviceRepository,
    observations: ObservationRepository,
    device_id: int,
    days: tuple[int, ...],
) -> None:
    for day in days:
        await observations.append(
            device_id=device_id,
            rssi=-70,
            observed_at=_day(day),
        )


async def test_frozen_knobs_match_docs() -> None:
    assert FAMILIAR_MIN_DAYS == 3
    assert FAMILIAR_MAX_DEVICES == 48


async def test_stable_fixture_familiar_after_k_distinct_days(
    devices: DeviceRepository,
    observations: ObservationRepository,
) -> None:
    stable_id = await devices.upsert(
        fingerprint=STABLE_FP,
        address=STABLE_ADDR,
    )
    await _observe_days(devices, observations, stable_id, (1, 2))
    assert stable_id not in await build_familiar_device_ids(
        devices,
        observations,
    )
    await observations.append(
        device_id=stable_id,
        rssi=-70,
        observed_at=_day(3),
    )
    familiar = await build_familiar_device_ids(devices, observations)
    assert stable_id in familiar


async def test_stranger_not_familiar_on_one_day(
    devices: DeviceRepository,
    observations: ObservationRepository,
) -> None:
    stranger_id = await devices.upsert(
        fingerprint=STRANGER_FP,
        address=STRANGER_ADDR,
    )
    await _observe_days(devices, observations, stranger_id, (1,))
    familiar = await build_familiar_device_ids(devices, observations)
    assert stranger_id not in familiar


async def test_labeled_device_familiar_without_observations(
    devices: DeviceRepository,
    observations: ObservationRepository,
) -> None:
    labeled_id = await devices.upsert(
        fingerprint="fp-labeled",
        address="AA:BB:00:00:00:0C",
        label="kitchen speaker",
    )
    familiar = await build_familiar_device_ids(devices, observations)
    assert labeled_id in familiar


async def test_ignored_label_counts_as_familiar(
    devices: DeviceRepository,
    observations: ObservationRepository,
) -> None:
    ignored_id = await devices.upsert(
        fingerprint="fp-ignored",
        address="AA:BB:00:00:00:0D",
        label="(ignored)",
    )
    familiar = await build_familiar_device_ids(devices, observations)
    assert ignored_id in familiar


async def test_auto_learn_cap_limits_history_pool(
    devices: DeviceRepository,
    observations: ObservationRepository,
) -> None:
    ids: list[int] = []
    for index in range(FAMILIAR_MAX_DEVICES + 5):
        device_id = await devices.upsert(
            fingerprint=f"fp-cap-{index}",
            address=f"AA:BB:00:00:{index:02X}",
        )
        ids.append(device_id)
        await _observe_days(
            devices,
            observations,
            device_id,
            (1, 2, 3),
        )
    familiar = await build_familiar_device_ids(devices, observations)
    auto_only = {device_id for device_id in familiar if device_id in ids}
    assert len(auto_only) == FAMILIAR_MAX_DEVICES
    top_id = ids[-1]
    bottom_id = ids[0]
    assert bottom_id in familiar
    assert top_id not in familiar


async def test_labeled_devices_do_not_consume_auto_learn_cap_slots(
    devices: DeviceRepository,
    observations: ObservationRepository,
) -> None:
    labeled_ids: list[int] = []
    for index in range(10):
        labeled_ids.append(
            await devices.upsert(
                fingerprint=f"fp-labeled-cap-{index}",
                address=f"AA:BB:01:00:{index:02X}",
                label=f"fixture-{index}",
            )
        )
        await _observe_days(
            devices,
            observations,
            labeled_ids[-1],
            (1, 2, 3, 4, 5),
        )
    stranger_ids: list[int] = []
    for index in range(FAMILIAR_MAX_DEVICES):
        stranger_id = await devices.upsert(
            fingerprint=f"fp-stranger-cap-{index}",
            address=f"AA:BB:02:00:{index:02X}",
        )
        stranger_ids.append(stranger_id)
        await _observe_days(devices, observations, stranger_id, (1, 2, 3))
    familiar = await build_familiar_device_ids(devices, observations)
    assert all(device_id in familiar for device_id in labeled_ids)
    assert all(device_id in familiar for device_id in stranger_ids)


async def test_rotating_alias_same_device_id_accumulates_days(
    devices: DeviceRepository,
    observations: ObservationRepository,
) -> None:
    device_id = await devices.upsert(
        fingerprint="fp-rotate-base",
        address="AA:00:00:00:00:01",
    )
    await devices.record_alias(
        fingerprint="fp-rotate-alias",
        device_id=device_id,
    )
    await _observe_days(devices, observations, device_id, (1, 2, 3))
    familiar = await build_familiar_device_ids(devices, observations)
    assert device_id in familiar


async def test_familiar_set_is_familiar() -> None:
    familiar = FamiliarSet(frozenset({1, 3}))
    assert familiar.is_familiar(1)
    assert not familiar.is_familiar(2)


async def test_refresher_build_and_daily_refresh(
    db: aiosqlite.Connection,
    devices: DeviceRepository,
    observations: ObservationRepository,
) -> None:
    site_state = SiteStateRepository(db, SITE)
    refresher = FamiliarSetRefresher(
        devices,
        observations,
        site_state=site_state,
    )
    await refresher.build()
    assert not refresher.familiar.is_familiar(1)
    stable_id = await devices.upsert(
        fingerprint=STABLE_FP,
        address=STABLE_ADDR,
    )
    await _observe_days(devices, observations, stable_id, (1, 2, 3))
    first = await refresher.refresh_if_due(_day(3, 23))
    assert first
    assert refresher.familiar.is_familiar(stable_id)
    second = await refresher.refresh_if_due(_day(3, 23))
    assert not second
    third = await refresher.refresh_if_due(_day(4))
    assert third
    assert refresher.familiar.is_familiar(stable_id)
