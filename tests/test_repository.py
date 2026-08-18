# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for DeviceRepository and ObservationRepository (P1-6)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from blesentry.storage import apply_migrations, connect
from blesentry.storage.repository import (
    DeviceRepository,
    ObservationRepository,
)

SITE = "test-site"


def _ts(hour: int = 0, minute: int = 0) -> str:
    return f"2026-01-15T{hour:02d}:{minute:02d}:00.000000Z"


@pytest.fixture
async def db() -> AsyncIterator[aiosqlite.Connection]:
    conn = await connect(":memory:")
    try:
        await apply_migrations(conn)
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def device_repo(db: aiosqlite.Connection) -> DeviceRepository:
    return DeviceRepository(db, SITE)


@pytest.fixture
def obs_repo(db: aiosqlite.Connection) -> ObservationRepository:
    return ObservationRepository(db, SITE)


# -- DeviceRepository: upsert --


async def test_upsert_creates_new_device(
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-aaa", address="AA:BB:CC:DD:EE:FF"
    )
    assert device_id >= 1


async def test_upsert_same_fingerprint_returns_same_id(
    device_repo: DeviceRepository,
) -> None:
    first = await device_repo.upsert(
        fingerprint="fp-x", address="11:22:33:44:55:66"
    )
    second = await device_repo.upsert(
        fingerprint="fp-x", address="11:22:33:44:55:66"
    )
    assert first == second


async def test_upsert_updates_mac_on_fingerprint_match(
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-rot", address="AA:00:00:00:00:01"
    )
    await device_repo.upsert(fingerprint="fp-rot", address="AA:00:00:00:00:02")
    device = await device_repo.get(device_id)
    assert device is not None
    assert device["address"] == "AA:00:00:00:00:02"


async def test_upsert_stores_label_and_description(
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-label",
        address="AA:BB:CC:DD:EE:FF",
        label="Ryan's Phone",
        description="iPhone 15 Pro",
    )
    device = await device_repo.get(device_id)
    assert device is not None
    assert device["label"] == "Ryan's Phone"
    assert device["description"] == "iPhone 15 Pro"


async def test_upsert_preserves_metadata_on_partial_update(
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-persist",
        address="AA:BB:CC:DD:EE:FF",
        label="Keep Me",
        description="Keep This Too",
    )
    await device_repo.upsert(
        fingerprint="fp-persist",
        address="AA:BB:CC:DD:EE:02",
    )
    device = await device_repo.get(device_id)
    assert device is not None
    assert device["address"] == "AA:BB:CC:DD:EE:02"
    assert device["label"] == "Keep Me"
    assert device["description"] == "Keep This Too"


async def test_upsert_sets_site_id(
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-s", address="00:11:22:33:44:55"
    )
    device = await device_repo.get(device_id)
    assert device is not None
    assert device["site_id"] == SITE


async def test_upsert_sets_created_and_updated_at(
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-ts", address="00:11:22:33:44:55"
    )
    device = await device_repo.get(device_id)
    assert device is not None
    assert device["created_at"]
    assert device["updated_at"]


async def test_upsert_in_file_database(
    tmp_path: Path,
) -> None:
    conn = await connect(tmp_path / "repo.db")
    try:
        await apply_migrations(conn)
        repo = DeviceRepository(conn, SITE)
        device_id = await repo.upsert(
            fingerprint="fp-file", address="DE:AD:BE:EF:00:01"
        )
        device = await repo.get(device_id)
        assert device is not None
        assert device["fingerprint"] == "fp-file"
    finally:
        await conn.close()


async def test_upsert_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "persist.db"
    conn = await connect(path)
    try:
        await apply_migrations(conn)
        repo = DeviceRepository(conn, SITE)
        device_id = await repo.upsert(
            fingerprint="fp-dur", address="DE:AD:BE:EF:00:01"
        )
    finally:
        await conn.close()
    conn2 = await connect(path)
    try:
        await apply_migrations(conn2)
        repo2 = DeviceRepository(conn2, SITE)
        device = await repo2.get(device_id)
        assert device is not None
        assert device["fingerprint"] == "fp-dur"
        assert device["address"] == "DE:AD:BE:EF:00:01"
    finally:
        await conn2.close()


async def test_upsert_preserves_mac_on_omission(
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-mac-persist",
        address="AA:BB:CC:DD:EE:FF",
    )
    await device_repo.upsert(fingerprint="fp-mac-persist")
    device = await device_repo.get(device_id)
    assert device is not None
    assert device["address"] == "AA:BB:CC:DD:EE:FF"


# -- DeviceRepository: get --


async def test_get_existing_device(
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-get", address="AA:BB:CC:DD:EE:01"
    )
    device = await device_repo.get(device_id)
    assert device is not None
    assert device["id"] == device_id
    assert device["fingerprint"] == "fp-get"
    assert device["address"] == "AA:BB:CC:DD:EE:01"


async def test_get_nonexistent_device_returns_none(
    device_repo: DeviceRepository,
) -> None:
    device = await device_repo.get(99999)
    assert device is None


# -- DeviceRepository: list_devices --


async def test_list_devices_empty_by_default(
    device_repo: DeviceRepository,
) -> None:
    devices = await device_repo.list_devices()
    assert devices == []


async def test_list_devices_returns_all(
    device_repo: DeviceRepository,
) -> None:
    await device_repo.upsert(fingerprint="fp-1", address="AA:00:00:00:00:01")
    await device_repo.upsert(fingerprint="fp-2", address="AA:00:00:00:00:02")
    await device_repo.upsert(fingerprint="fp-3", address="AA:00:00:00:00:03")
    devices = await device_repo.list_devices()
    assert len(devices) == 3
    fps = {d["fingerprint"] for d in devices}
    assert fps == {"fp-1", "fp-2", "fp-3"}


async def test_list_devices_filtered_by_site_id(
    device_repo: DeviceRepository,
    db: aiosqlite.Connection,
) -> None:
    other = DeviceRepository(db, "other-site")
    await device_repo.upsert(fingerprint="fp-a", address="AA:00:00:00:00:01")
    await other.upsert(fingerprint="fp-b", address="BB:00:00:00:00:01")
    devices = await device_repo.list_devices()
    assert len(devices) == 1
    assert devices[0]["fingerprint"] == "fp-a"


# -- ObservationRepository: append --


async def test_append_creates_observation(
    obs_repo: ObservationRepository,
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-app", address="AA:BB:CC:DD:EE:FF"
    )
    obs_id = await obs_repo.append(
        device_id=device_id,
        rssi=-65,
        observed_at=_ts(10, 0),
        adapter_id="hci0",
    )
    assert obs_id >= 1


async def test_append_returns_unique_ids(
    obs_repo: ObservationRepository,
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-uid", address="AA:BB:CC:DD:EE:FF"
    )
    id1 = await obs_repo.append(
        device_id=device_id,
        rssi=-65,
        observed_at=_ts(10, 0),
        adapter_id="hci0",
    )
    id2 = await obs_repo.append(
        device_id=device_id,
        rssi=-70,
        observed_at=_ts(10, 1),
        adapter_id="hci0",
    )
    assert id1 != id2


async def test_append_stores_all_fields(
    obs_repo: ObservationRepository,
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-fields", address="AA:BB:CC:DD:EE:FF"
    )
    obs_id = await obs_repo.append(
        device_id=device_id,
        rssi=-42,
        observed_at=_ts(14, 30),
        adapter_id="macos-corebluetooth",
    )
    obs = await obs_repo.get(obs_id)
    assert obs is not None
    assert obs["site_id"] == SITE
    assert obs["device_id"] == device_id
    assert obs["rssi"] == -42
    assert obs["observed_at"] == _ts(14, 30)
    assert obs["adapter_id"] == "macos-corebluetooth"


async def test_append_without_adapter_id(
    obs_repo: ObservationRepository,
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-no-adapter", address="AA:BB:CC:DD:EE:FF"
    )
    obs_id = await obs_repo.append(
        device_id=device_id,
        rssi=-55,
        observed_at=_ts(12, 0),
    )
    obs = await obs_repo.get(obs_id)
    assert obs is not None
    assert obs["adapter_id"] is None


async def test_append_in_file_database(
    tmp_path: Path,
) -> None:
    conn = await connect(tmp_path / "obs.db")
    try:
        await apply_migrations(conn)
        devs = DeviceRepository(conn, SITE)
        obs = ObservationRepository(conn, SITE)
        device_id = await devs.upsert(
            fingerprint="fp-file-obs", address="DE:AD:BE:EF:00:01"
        )
        obs_id = await obs.append(
            device_id=device_id,
            rssi=-72,
            observed_at=_ts(8, 0),
            adapter_id="hci0",
        )
        assert obs_id >= 1
    finally:
        await conn.close()


async def test_observation_survives_reopen(
    tmp_path: Path,
) -> None:
    path = tmp_path / "obs-persist.db"
    conn = await connect(path)
    try:
        await apply_migrations(conn)
        devs = DeviceRepository(conn, SITE)
        obs = ObservationRepository(conn, SITE)
        device_id = await devs.upsert(
            fingerprint="fp-obs-dur",
            address="DE:AD:BE:EF:00:01",
        )
        obs_id = await obs.append(
            device_id=device_id,
            rssi=-55,
            observed_at=_ts(10, 0),
            adapter_id="hci0",
        )
    finally:
        await conn.close()
    conn2 = await connect(path)
    try:
        await apply_migrations(conn2)
        obs2 = ObservationRepository(conn2, SITE)
        row = await obs2.get(obs_id)
        assert row is not None
        assert row["rssi"] == -55
        assert row["adapter_id"] == "hci0"
    finally:
        await conn2.close()


# -- ObservationRepository: query_recent_rssi --


async def test_query_recent_rssi_empty_when_no_observations(
    obs_repo: ObservationRepository,
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-empty", address="AA:BB:CC:DD:EE:FF"
    )
    results = await obs_repo.query_recent_rssi(
        device_id=device_id, since=_ts(0, 0)
    )
    assert results == []


async def test_query_recent_rssi_returns_in_order(
    obs_repo: ObservationRepository,
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-order", address="AA:BB:CC:DD:EE:FF"
    )
    await obs_repo.append(
        device_id=device_id,
        rssi=-50,
        observed_at=_ts(10, 0),
        adapter_id="hci0",
    )
    await obs_repo.append(
        device_id=device_id,
        rssi=-60,
        observed_at=_ts(10, 1),
        adapter_id="hci0",
    )
    await obs_repo.append(
        device_id=device_id,
        rssi=-70,
        observed_at=_ts(10, 2),
        adapter_id="hci0",
    )
    results = await obs_repo.query_recent_rssi(
        device_id=device_id, since=_ts(0, 0)
    )
    assert len(results) == 3
    assert results[0] == (_ts(10, 0), -50)
    assert results[1] == (_ts(10, 1), -60)
    assert results[2] == (_ts(10, 2), -70)


async def test_query_recent_rssi_filters_by_since(
    obs_repo: ObservationRepository,
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-since", address="AA:BB:CC:DD:EE:FF"
    )
    await obs_repo.append(
        device_id=device_id,
        rssi=-50,
        observed_at=_ts(10, 0),
        adapter_id="hci0",
    )
    await obs_repo.append(
        device_id=device_id,
        rssi=-60,
        observed_at=_ts(10, 5),
        adapter_id="hci0",
    )
    await obs_repo.append(
        device_id=device_id,
        rssi=-70,
        observed_at=_ts(10, 10),
        adapter_id="hci0",
    )
    results = await obs_repo.query_recent_rssi(
        device_id=device_id, since=_ts(10, 5)
    )
    assert len(results) == 2
    assert results[0] == (_ts(10, 5), -60)
    assert results[1] == (_ts(10, 10), -70)


async def test_query_recent_rssi_excludes_other_devices(
    obs_repo: ObservationRepository,
    device_repo: DeviceRepository,
) -> None:
    d1 = await device_repo.upsert(fingerprint="fp-d1", address="AA:00:00:00:00:01")
    d2 = await device_repo.upsert(fingerprint="fp-d2", address="AA:00:00:00:00:02")
    await obs_repo.append(
        device_id=d1,
        rssi=-50,
        observed_at=_ts(10, 0),
        adapter_id="hci0",
    )
    await obs_repo.append(
        device_id=d2,
        rssi=-80,
        observed_at=_ts(10, 0),
        adapter_id="hci0",
    )
    results = await obs_repo.query_recent_rssi(device_id=d1, since=_ts(0, 0))
    assert len(results) == 1
    assert results[0] == (_ts(10, 0), -50)


async def test_query_recent_rssi_excludes_other_sites(
    obs_repo: ObservationRepository,
    device_repo: DeviceRepository,
    db: aiosqlite.Connection,
) -> None:
    other_obs = ObservationRepository(db, "other-site")
    other_devs = DeviceRepository(db, "other-site")
    device_id = await device_repo.upsert(
        fingerprint="fp-site", address="AA:BB:CC:DD:EE:FF"
    )
    other_id = await other_devs.upsert(
        fingerprint="fp-site", address="AA:BB:CC:DD:EE:FF"
    )
    await obs_repo.append(
        device_id=device_id,
        rssi=-55,
        observed_at=_ts(10, 0),
        adapter_id="hci0",
    )
    await other_obs.append(
        device_id=other_id,
        rssi=-90,
        observed_at=_ts(10, 0),
        adapter_id="hci0",
    )
    results = await obs_repo.query_recent_rssi(
        device_id=device_id, since=_ts(0, 0)
    )
    assert len(results) == 1
    assert results[0][1] == -55


async def test_query_recent_rssi_handles_duplicate_appends(
    obs_repo: ObservationRepository,
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-dup", address="AA:BB:CC:DD:EE:FF"
    )
    ts = _ts(10, 0)
    await obs_repo.append(
        device_id=device_id,
        rssi=-50,
        observed_at=ts,
        adapter_id="hci0",
    )
    await obs_repo.append(
        device_id=device_id,
        rssi=-60,
        observed_at=ts,
        adapter_id="hci0",
    )
    results = await obs_repo.query_recent_rssi(
        device_id=device_id, since=_ts(0, 0)
    )
    assert len(results) == 2


async def test_append_rejects_cross_site_device(
    obs_repo: ObservationRepository,
    device_repo: DeviceRepository,
    db: aiosqlite.Connection,
) -> None:
    other_devs = DeviceRepository(db, "other-site")
    other_id = await other_devs.upsert(
        fingerprint="fp-xsite", address="AA:BB:CC:DD:EE:FF"
    )
    with pytest.raises(ValueError, match="not found in site"):
        await obs_repo.append(
            device_id=other_id,
            rssi=-60,
            observed_at=_ts(10, 0),
            adapter_id="hci0",
        )


async def test_append_rejects_nonexistent_device(
    obs_repo: ObservationRepository,
) -> None:
    with pytest.raises(ValueError, match="not found in site"):
        await obs_repo.append(
            device_id=99999,
            rssi=-60,
            observed_at=_ts(10, 0),
            adapter_id="hci0",
        )


# -- ObservationRepository: get --


async def test_obs_get_existing(
    obs_repo: ObservationRepository,
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(
        fingerprint="fp-obs-get", address="AA:BB:CC:DD:EE:FF"
    )
    obs_id = await obs_repo.append(
        device_id=device_id,
        rssi=-55,
        observed_at=_ts(10, 0),
        adapter_id="hci0",
    )
    obs = await obs_repo.get(obs_id)
    assert obs is not None
    assert obs["id"] == obs_id
    assert obs["device_id"] == device_id
    assert obs["rssi"] == -55


async def test_obs_get_nonexistent_returns_none(
    obs_repo: ObservationRepository,
) -> None:
    obs = await obs_repo.get(99999)
    assert obs is None
