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
    PresenceEventRepository,
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
    d1 = await device_repo.upsert(
        fingerprint="fp-d1", address="AA:00:00:00:00:01"
    )
    d2 = await device_repo.upsert(
        fingerprint="fp-d2", address="AA:00:00:00:00:02"
    )
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


async def test_append_cross_site_is_caller_contract(
    obs_repo: ObservationRepository,
    device_repo: DeviceRepository,
    db: aiosqlite.Connection,
) -> None:
    """Cross-site append is a documented caller contract (#84).

    The per-append ownership probe was dropped: device_id must come
    from this site's DeviceRepository (documented caller contract).
    A cross-site id passes the FK; the row lands under the repo site.
    """
    other_devs = DeviceRepository(db, "other-site")
    other_id = await other_devs.upsert(
        fingerprint="fp-xsite", address="AA:BB:CC:DD:EE:FF"
    )
    obs_id = await obs_repo.append(
        device_id=other_id,
        rssi=-60,
        observed_at=_ts(10, 0),
        adapter_id="hci0",
    )
    row = await obs_repo.get(obs_id)
    assert row is not None and row["site_id"] == "test-site"


async def test_append_rejects_nonexistent_device(
    obs_repo: ObservationRepository,
) -> None:
    """FK enforcement (#84): missing device fails loudly at insert."""
    with pytest.raises(aiosqlite.IntegrityError):
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


# -- DeviceRepository: labels + audit (P2-8) --


async def _audit(
    db: aiosqlite.Connection, device_id: int
) -> list[tuple[str, str | None, str | None]]:
    cur = await db.execute(
        "SELECT actor, previous_label, new_label FROM label_audit "
        "WHERE device_id = ? ORDER BY id",
        (device_id,),
    )
    rows = await cur.fetchall()
    await cur.close()
    return [(r[0], r[1], r[2]) for r in rows]


async def test_set_label_updates_and_audits(
    device_repo: DeviceRepository, db: aiosqlite.Connection
) -> None:
    device_id = await device_repo.upsert(fingerprint="fp-lbl")
    assert await device_repo.set_label(
        device_id, label="Front Door", actor="op"
    )
    row = await device_repo.get(device_id)
    assert row is not None and row["label"] == "Front Door"
    assert await _audit(db, device_id) == [("op", None, "Front Door")]


async def test_set_label_records_previous_label(
    device_repo: DeviceRepository, db: aiosqlite.Connection
) -> None:
    device_id = await device_repo.upsert(fingerprint="fp-re", label="Old")
    await device_repo.set_label(device_id, label="New", actor="op")
    assert (await _audit(db, device_id))[-1] == ("op", "Old", "New")


async def test_set_label_none_clears_and_audits(
    device_repo: DeviceRepository, db: aiosqlite.Connection
) -> None:
    device_id = await device_repo.upsert(fingerprint="fp-un", label="X")
    assert await device_repo.set_label(device_id, label=None, actor="op")
    row = await device_repo.get(device_id)
    assert row is not None and row["label"] is None
    assert (await _audit(db, device_id))[-1] == ("op", "X", None)


async def test_set_label_unknown_device_is_noop(
    device_repo: DeviceRepository, db: aiosqlite.Connection
) -> None:
    assert await device_repo.set_label(999, label="X", actor="op") is False
    assert await _audit(db, 999) == []


async def test_set_label_is_site_scoped(db: aiosqlite.Connection) -> None:
    site_a = DeviceRepository(db, "site-a")
    site_b = DeviceRepository(db, "site-b")
    device_id = await site_a.upsert(fingerprint="fp")
    assert await site_b.set_label(device_id, label="X", actor="op") is False
    row = await site_a.get(device_id)
    assert row is not None and row["label"] is None


async def test_set_description(device_repo: DeviceRepository) -> None:
    device_id = await device_repo.upsert(fingerprint="fp-d")
    assert await device_repo.set_description(device_id, description="note")
    row = await device_repo.get(device_id)
    assert row is not None and row["description"] == "note"


async def test_set_description_unknown_device_is_noop(
    device_repo: DeviceRepository,
) -> None:
    assert await device_repo.set_description(999, description="x") is False


# -- DeviceRepository: device_aliases (ADR-0005 / #96) --


async def test_record_alias_binds_fingerprint_to_device(
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(fingerprint="fp-founding")
    alias_id = await device_repo.record_alias(
        fingerprint="fp-rotated", device_id=device_id
    )
    assert alias_id >= 1
    bound = await device_repo.get_by_alias("fp-rotated")
    assert bound is not None
    assert bound["id"] == device_id
    assert bound["fingerprint"] == "fp-founding"
    assert bound["site_id"] == SITE
    aliases = await device_repo.list_aliases(device_id)
    assert aliases[0]["site_id"] == SITE


async def test_record_alias_unknown_device_raises(
    device_repo: DeviceRepository,
) -> None:
    with pytest.raises(ValueError, match="not found"):
        await device_repo.record_alias(fingerprint="fp-x", device_id=999)


async def test_record_alias_is_site_scoped(db: aiosqlite.Connection) -> None:
    site_a = DeviceRepository(db, "site-a")
    site_b = DeviceRepository(db, "site-b")
    device_id = await site_a.upsert(fingerprint="fp-a")
    with pytest.raises(ValueError, match="not found"):
        await site_b.record_alias(fingerprint="fp-rot", device_id=device_id)
    await site_a.record_alias(fingerprint="fp-rot", device_id=device_id)
    assert await site_b.get_by_alias("fp-rot") is None
    bound = await site_a.get_by_alias("fp-rot")
    assert bound is not None and bound["id"] == device_id


async def test_record_alias_idempotent_same_device(
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(fingerprint="fp-f")
    first = await device_repo.record_alias(
        fingerprint="fp-rot", device_id=device_id
    )
    before = (await device_repo.list_aliases(device_id))[0]["updated_at"]
    second = await device_repo.record_alias(
        fingerprint="fp-rot", device_id=device_id
    )
    assert first == second
    aliases = await device_repo.list_aliases(device_id)
    assert len(aliases) == 1
    assert aliases[0]["fingerprint"] == "fp-rot"
    assert aliases[0]["created_at"]
    assert aliases[0]["updated_at"] == before


async def test_record_alias_conflict_different_device(
    device_repo: DeviceRepository,
) -> None:
    a = await device_repo.upsert(fingerprint="fp-a")
    b = await device_repo.upsert(fingerprint="fp-b")
    await device_repo.record_alias(fingerprint="fp-rot", device_id=a)
    with pytest.raises(ValueError, match="alias conflict"):
        await device_repo.record_alias(fingerprint="fp-rot", device_id=b)


async def test_list_aliases_oldest_first(
    device_repo: DeviceRepository,
) -> None:
    device_id = await device_repo.upsert(fingerprint="fp-f")
    await device_repo.record_alias(fingerprint="fp-1", device_id=device_id)
    await device_repo.record_alias(fingerprint="fp-2", device_id=device_id)
    aliases = await device_repo.list_aliases(device_id)
    assert [row["fingerprint"] for row in aliases] == ["fp-1", "fp-2"]


async def test_list_aliases_unknown_device_is_empty(
    device_repo: DeviceRepository,
) -> None:
    assert await device_repo.list_aliases(999) == []


async def test_get_by_alias_unknown_is_none(
    device_repo: DeviceRepository,
) -> None:
    assert await device_repo.get_by_alias("no-such") is None


async def test_alias_fingerprint_unique_per_site(
    db: aiosqlite.Connection,
) -> None:
    site_a = DeviceRepository(db, "site-a")
    site_b = DeviceRepository(db, "site-b")
    a = await site_a.upsert(fingerprint="fp-a")
    b = await site_b.upsert(fingerprint="fp-b")
    await site_a.record_alias(fingerprint="fp-shared", device_id=a)
    await site_b.record_alias(fingerprint="fp-shared", device_id=b)
    bound_a = await site_a.get_by_alias("fp-shared")
    bound_b = await site_b.get_by_alias("fp-shared")
    assert bound_a is not None and bound_a["id"] == a
    assert bound_b is not None and bound_b["id"] == b


# -- PresenceEventRepository (P2-1) --


@pytest.fixture
def presence_repo(db: aiosqlite.Connection) -> PresenceEventRepository:
    return PresenceEventRepository(db, SITE)


async def test_presence_append_and_list(
    device_repo: DeviceRepository, presence_repo: PresenceEventRepository
) -> None:
    device_id = await device_repo.upsert(fingerprint="fp-pe")
    await presence_repo.append(
        device_id=device_id, event_type="PRESENT", occurred_at=_ts(10, 0)
    )
    await presence_repo.append(
        device_id=device_id, event_type="ABSENT", occurred_at=_ts(10, 5)
    )
    events = await presence_repo.list_for_device(device_id)
    assert [(e["event_type"], e["occurred_at"]) for e in events] == [
        ("PRESENT", _ts(10, 0)),
        ("ABSENT", _ts(10, 5)),
    ]


async def test_presence_is_site_scoped(db: aiosqlite.Connection) -> None:
    site_a = DeviceRepository(db, "site-a")
    device_id = await site_a.upsert(fingerprint="fp")
    await PresenceEventRepository(db, "site-a").append(
        device_id=device_id, event_type="PRESENT", occurred_at=_ts(1, 0)
    )
    other = PresenceEventRepository(db, "site-b")
    assert await other.list_for_device(device_id) == []


async def test_presence_rejects_bad_event_type(
    device_repo: DeviceRepository, presence_repo: PresenceEventRepository
) -> None:
    device_id = await device_repo.upsert(fingerprint="fp")
    with pytest.raises(Exception):  # noqa: B017 - schema CHECK constraint
        await presence_repo.append(
            device_id=device_id, event_type="MAYBE", occurred_at=_ts(1, 0)
        )
