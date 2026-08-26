# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Scan loop tests (P1-8): scan window -> resolve -> persist.

MockScanner scripts the radio; a real temp-file SQLite database (with
migrations applied) verifies persistence end to end. The provisional
resolver is exact fingerprint identity — #19 replaces it with fusion.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from blesentry.loop import (
    CycleStats,
    fingerprint_key,
    iso_utc,
    run_cycle,
    run_loop,
)
from blesentry.resolver import DeviceResolver
from blesentry.scanner import Advertisement, Fingerprint
from blesentry.scanner.mock import MockScanner
from blesentry.storage.database import apply_migrations, connect
from blesentry.storage.repository import (
    DeviceRepository,
    ObservationRepository,
)


def _ad(
    address: str = "AA:BB:CC:DD:EE:FF",
    rssi: int = -65,
    local_name: str | None = "Device",
    timestamp: float = 1755400000.0,
) -> Advertisement:
    return Advertisement(
        address=address,
        rssi=rssi,
        local_name=local_name,
        service_uuids=["180d"],
        manufacturer_data={"76": "0102"},
        timestamp=timestamp,
        adapter_id="mock",
    )


@pytest.fixture
async def repos(tmp_path: Path):
    conn = await connect(tmp_path / "loop.db")
    await apply_migrations(conn)
    yield (
        DeviceRepository(conn, "test-site"),
        ObservationRepository(conn, "test-site"),
    )
    await conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_iso_utc_matches_schema_format() -> None:
    # docs/schema.md: %Y-%m-%dT%H:%M:%fZ, lexically sortable
    assert iso_utc(1755400000.5) == "2025-08-17T03:06:40.500Z"


def test_iso_utc_is_fixed_width() -> None:
    assert len(iso_utc(0.0)) == len(iso_utc(1755400000.123456))


def test_fingerprint_key_is_deterministic() -> None:
    ad = _ad()
    key_a = fingerprint_key(Fingerprint.from_advertisement(ad))
    key_b = fingerprint_key(Fingerprint.from_advertisement(ad))
    assert key_a == key_b


def test_fingerprint_key_differs_for_different_devices() -> None:
    key_a = fingerprint_key(Fingerprint.from_advertisement(_ad()))
    key_b = fingerprint_key(
        Fingerprint.from_advertisement(_ad(address="11:22:33:44:55:66"))
    )
    assert key_a != key_b


# ---------------------------------------------------------------------------
# One cycle: scan -> resolve -> persist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cycle_persists_devices_and_observations(repos) -> None:
    devices, observations = repos
    scanner = MockScanner(
        scenarios=[[_ad(), _ad(address="11:22:33:44:55:66", rssi=-80)]]
    )
    stats = await run_cycle(scanner, devices, observations, duration=1.0)
    # same name/uuids/payload on two addresses: fusion (#19) joins them
    assert stats == CycleStats(heard=2, devices=1, observations=2)
    assert len(await devices.list_devices()) == 1


@pytest.mark.asyncio
async def test_same_device_across_cycles_is_one_row(repos) -> None:
    devices, observations = repos
    scanner = MockScanner(
        scenarios=[
            [_ad(rssi=-60, timestamp=1755400000.0)],
            [_ad(rssi=-70, timestamp=1755400010.0)],
        ]
    )
    await run_cycle(scanner, devices, observations, duration=1.0)
    await run_cycle(scanner, devices, observations, duration=1.0)

    rows = await devices.list_devices()
    assert len(rows) == 1
    rssi = await observations.query_recent_rssi(
        device_id=rows[0]["id"], since="2025-01-01T00:00:00.000Z"
    )
    assert [r[1] for r in rssi] == [-60, -70]


@pytest.mark.asyncio
async def test_quiet_cycle_is_a_no_op(repos) -> None:
    devices, observations = repos
    stats = await run_cycle(
        MockScanner(scenarios=[]), devices, observations, duration=1.0
    )
    assert stats == CycleStats(heard=0, devices=0, observations=0)
    assert await devices.list_devices() == []


@pytest.mark.asyncio
async def test_observation_carries_advertisement_metadata(repos) -> None:
    devices, observations = repos
    scanner = MockScanner(scenarios=[[_ad(timestamp=1755400000.5)]])
    await run_cycle(scanner, devices, observations, duration=1.0)
    rows = await devices.list_devices()
    rssi = await observations.query_recent_rssi(
        device_id=rows[0]["id"], since="2025-01-01T00:00:00.000Z"
    )
    assert rssi == [("2025-08-17T03:06:40.500Z", -65)]


# ---------------------------------------------------------------------------
# The loop: multiple cycles, bounded for tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_runs_max_cycles_and_stops(repos) -> None:
    devices, observations = repos
    scanner = MockScanner(scenarios=[[_ad()], [_ad()], [_ad()]])
    cycles = await run_loop(
        scanner,
        devices,
        observations,
        duration=0.0,
        pause=0.0,
        max_cycles=2,
    )
    assert cycles == 2
    rows = await devices.list_devices()
    rssi = await observations.query_recent_rssi(
        device_id=rows[0]["id"], since="2025-01-01T00:00:00.000Z"
    )
    assert len(rssi) == 2


@pytest.mark.asyncio
async def test_loop_cancellation_propagates_mid_pause(repos) -> None:
    """Cancellation (the SIGTERM path) unwinds promptly from the pause."""
    devices, observations = repos
    scanner = MockScanner(scenarios=[[_ad()]] * 5)
    task = asyncio.create_task(
        run_loop(
            scanner,
            devices,
            observations,
            duration=0.0,
            pause=30.0,
            max_cycles=None,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(await devices.list_devices()) == 1


# ---------------------------------------------------------------------------
# Provenance persistence (#56): address_type flows to observations,
# fingerprint keys are versioned
# ---------------------------------------------------------------------------


def test_fingerprint_key_is_versioned() -> None:
    key = fingerprint_key(Fingerprint.from_advertisement(_ad()))
    assert '"v": 2' in key or '"v":2' in key


@pytest.mark.asyncio
async def test_cycle_persists_address_type(repos) -> None:
    devices, observations = repos
    ad = Advertisement(
        address="F0:11:22:33:44:55",
        address_type="random_static",
        adv_type="ADV_IND",
        rssi=-50,
        timestamp=1755400000.0,
        adapter_id="mock",
    )
    scanner = MockScanner(scenarios=[[ad]])
    await run_cycle(scanner, devices, observations, duration=1.0)
    rows = await devices.list_devices()
    obs = await observations.get(1)
    assert obs is not None
    assert obs["address_type"] == "random_static"
    assert obs["adv_type"] == "ADV_IND"
    assert rows[0]["address"] == "F0:11:22:33:44:55"


# ---------------------------------------------------------------------------
# Cycle batching (#84): one transaction per cycle, no updated_at churn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cycle_rolls_back_atomically_on_mid_batch_failure(
    repos, tmp_path
) -> None:
    """A failure inside the cycle leaves NO partial rows behind."""
    devices, observations = repos
    good = _ad(address="AA:00:00:00:00:01")
    bad = _ad(address="AA:00:00:00:00:02", rssi=-60)
    object.__setattr__(bad, "timestamp", float("nan"))
    scanner = MockScanner(scenarios=[[good, bad]])
    with pytest.raises(ValueError):
        await run_cycle(scanner, devices, observations, duration=1.0)
    assert await devices.list_devices() == []


@pytest.mark.asyncio
async def test_known_device_not_rewritten_every_cycle(repos) -> None:
    """A stationary device's row is written once, not per sighting."""
    devices, observations = repos
    scanner = MockScanner(
        scenarios=[
            [_ad(rssi=-60, timestamp=1755400000.0)],
            [_ad(rssi=-61, timestamp=1755400015.0)],
        ]
    )
    resolver = DeviceResolver(devices)
    await run_cycle(
        scanner, devices, observations, duration=1.0, resolver=resolver
    )
    first = (await devices.list_devices())[0]["updated_at"]
    await run_cycle(
        scanner, devices, observations, duration=1.0, resolver=resolver
    )
    rows = await devices.list_devices()
    assert len(rows) == 1
    assert rows[0]["updated_at"] == first
    rssi = await observations.query_recent_rssi(
        device_id=rows[0]["id"], since="2025-01-01T00:00:00.000Z"
    )
    assert len(rssi) == 2


@pytest.mark.asyncio
async def test_cache_survives_rollback_unpoisoned(repos) -> None:
    """A rolled-back cycle must not leave phantom ids in the cache."""
    devices, observations = repos
    resolver = DeviceResolver(devices)
    good = _ad(address="AA:00:00:00:00:01")
    bad = _ad(address="AA:00:00:00:00:02")
    object.__setattr__(bad, "timestamp", float("nan"))
    failing = MockScanner(scenarios=[[good, bad]])
    with pytest.raises(ValueError):
        await run_cycle(
            failing, devices, observations, duration=1.0, resolver=resolver
        )
    clean = MockScanner(scenarios=[[good]])
    stats = await run_cycle(
        clean, devices, observations, duration=1.0, resolver=resolver
    )
    assert stats.observations == 1
    assert len(await devices.list_devices()) == 1


@pytest.mark.asyncio
async def test_migration_0003_recency_index_exists(repos) -> None:
    devices, _ = repos
    cur = await devices.connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='devices'"
    )
    names = {r[0] for r in await cur.fetchall()}
    await cur.close()
    assert "idx_devices_site_updated" in names


# ---------------------------------------------------------------------------
# Resolver affinity (#149): caller-supplied DeviceResolver must share
# the cycle connection AND site_id. Observation device_id is not
# site-qualified; a foreign resolver can stamp another site's identity
# or write outside the cycle transaction.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cycle_rejects_resolver_on_different_connection(
    tmp_path: Path,
) -> None:
    """Resolve writes on another connection escape the cycle transaction."""
    db = tmp_path / "cycle.db"
    scan_conn = await connect(db)
    await apply_migrations(scan_conn)
    other_conn = await connect(db)
    try:
        devices = DeviceRepository(scan_conn, "test-site")
        observations = ObservationRepository(scan_conn, "test-site")
        resolver = DeviceResolver(DeviceRepository(other_conn, "test-site"))
        with pytest.raises(ValueError, match="cycle connection"):
            await run_cycle(
                MockScanner(scenarios=[[]]),
                devices,
                observations,
                duration=0.0,
                resolver=resolver,
            )
    finally:
        await other_conn.close()
        await scan_conn.close()


@pytest.mark.asyncio
async def test_cycle_rejects_resolver_on_different_site(repos) -> None:
    """A same-connection resolver on another site can stamp foreign ids."""
    devices, observations = repos
    resolver = DeviceResolver(
        DeviceRepository(devices.connection, "other-site")
    )
    with pytest.raises(ValueError, match="cycle site"):
        await run_cycle(
            MockScanner(scenarios=[[]]),
            devices,
            observations,
            duration=0.0,
            resolver=resolver,
        )
