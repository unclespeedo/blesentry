# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for per-cycle window band-count persistence (C2 / #132)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from blesentry.loop import run_cycle
from blesentry.scanner import Advertisement
from blesentry.scanner.mock import MockScanner
from blesentry.storage.database import apply_migrations, connect
from blesentry.storage.repository import (
    DeviceRepository,
    ObservationRepository,
    SiteStateRepository,
    WindowBandCountRepository,
)

SITE = "test-site"
TS_OLD = "2026-01-01T00:00:00.000000Z"
TS_NEW = "2026-01-15T12:00:00.000000Z"


@pytest.fixture
async def db() -> AsyncIterator[aiosqlite.Connection]:
    conn = await connect(":memory:")
    try:
        await apply_migrations(conn)
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def band_repo(db: aiosqlite.Connection) -> WindowBandCountRepository:
    return WindowBandCountRepository(
        db,
        SITE,
        site_state=SiteStateRepository(db, SITE),
    )


def _ad(rssi: int = -65) -> Advertisement:
    return Advertisement(
        address="AA:BB:CC:DD:EE:01",
        rssi=rssi,
        local_name="dev",
        service_uuids=["180d"],
        manufacturer_data={"76": "0102"},
        timestamp=1755400000.0,
        adapter_id="mock",
    )


async def test_append_persists_nested_band_counts(
    band_repo: WindowBandCountRepository,
) -> None:
    row_id = await band_repo.append(
        window_index=0,
        observed_at=TS_NEW,
        count_all=3,
        count_far=2,
        count_near=1,
        count_adjacent=0,
    )
    assert row_id >= 1
    rows = await band_repo.list_for_site(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["site_id"] == SITE
    assert row["window_index"] == 0
    assert row["observed_at"] == TS_NEW
    assert row["count_all"] == 3
    assert row["count_far"] == 2
    assert row["count_near"] == 1
    assert row["count_adjacent"] == 0


async def test_append_rejects_unnested_counts(
    band_repo: WindowBandCountRepository,
) -> None:
    with pytest.raises(ValueError, match="adjacent"):
        await band_repo.append(
            window_index=0,
            observed_at=TS_NEW,
            count_all=1,
            count_far=0,
            count_near=2,
            count_adjacent=0,
        )


async def test_retention_deletes_rows_older_than_cutoff(
    band_repo: WindowBandCountRepository,
) -> None:
    await band_repo.append(
        window_index=0,
        observed_at=TS_OLD,
        count_all=1,
        count_far=1,
        count_near=1,
        count_adjacent=0,
    )
    await band_repo.append(
        window_index=1,
        observed_at=TS_NEW,
        count_all=2,
        count_far=2,
        count_near=1,
        count_adjacent=0,
    )
    deleted = await band_repo.delete_before("2026-01-10T00:00:00.000000Z")
    assert deleted == 1
    rows = await band_repo.list_for_site(limit=10)
    assert len(rows) == 1
    assert rows[0]["observed_at"] == TS_NEW


async def test_retention_runs_at_most_once_per_utc_day(
    band_repo: WindowBandCountRepository,
) -> None:
    await band_repo.append(
        window_index=0,
        observed_at=TS_OLD,
        count_all=1,
        count_far=1,
        count_near=1,
        count_adjacent=0,
    )
    first = await band_repo.run_retention_if_due(TS_NEW)
    assert first == 1
    second = await band_repo.run_retention_if_due(TS_NEW)
    assert second == 0
    rows = await band_repo.list_for_site(limit=10)
    assert len(rows) == 0


async def test_retention_runs_again_on_next_utc_day(
    band_repo: WindowBandCountRepository,
) -> None:
    await band_repo.append(
        window_index=0,
        observed_at=TS_OLD,
        count_all=1,
        count_far=1,
        count_near=1,
        count_adjacent=0,
    )
    await band_repo.run_retention_if_due("2026-01-15T23:59:59.000000Z")
    await band_repo.append(
        window_index=1,
        observed_at=TS_OLD,
        count_all=1,
        count_far=1,
        count_near=1,
        count_adjacent=0,
    )
    deleted = await band_repo.run_retention_if_due(
        "2026-01-16T00:00:01.000000Z"
    )
    assert deleted == 1


async def test_run_cycle_writes_band_counts_inside_transaction(
    tmp_path: Path,
) -> None:
    conn = await connect(tmp_path / "cycle.db")
    await apply_migrations(conn)
    devices = DeviceRepository(conn, SITE)
    observations = ObservationRepository(conn, SITE)
    band_counts = WindowBandCountRepository(
        conn,
        SITE,
        site_state=SiteStateRepository(conn, SITE),
    )
    scanner = MockScanner([[_ad(rssi=-70), _ad(rssi=-55)]])
    await run_cycle(
        scanner,
        devices,
        observations,
        duration=0.01,
        window_index=3,
        window_band_counts=band_counts,
        now=lambda: 1755400000.0,
    )
    rows = await band_counts.list_for_site(limit=10)
    assert len(rows) == 1
    assert rows[0]["window_index"] == 3
    assert rows[0]["count_all"] == 1
    assert rows[0]["count_near"] == 1
    assert rows[0]["count_adjacent"] == 1
    await conn.close()


async def test_run_cycle_band_count_rolled_back_on_failure(
    tmp_path: Path,
) -> None:
    conn = await connect(tmp_path / "rollback.db")
    await apply_migrations(conn)
    devices = DeviceRepository(conn, SITE)
    band_counts = WindowBandCountRepository(
        conn,
        SITE,
        site_state=SiteStateRepository(conn, SITE),
    )

    class _FailObs(ObservationRepository):
        async def append(self, **kwargs: object) -> int:
            raise RuntimeError("boom")

    scanner = MockScanner([[_ad()]])
    with pytest.raises(RuntimeError, match="boom"):
        await run_cycle(
            scanner,
            devices,
            _FailObs(conn, SITE),
            duration=0.01,
            window_index=0,
            window_band_counts=band_counts,
        )
    rows = await band_counts.list_for_site(limit=10)
    assert rows == []
    await conn.close()


async def test_run_cycle_requires_shared_connection(tmp_path: Path) -> None:
    conn1 = await connect(tmp_path / "a.db")
    conn2 = await connect(tmp_path / "b.db")
    await apply_migrations(conn1)
    await apply_migrations(conn2)
    devices = DeviceRepository(conn1, SITE)
    observations = ObservationRepository(conn1, SITE)
    band_counts = WindowBandCountRepository(conn2, SITE)
    scanner = MockScanner([[_ad()]])
    with pytest.raises(ValueError, match="window_band_counts must share"):
        await run_cycle(
            scanner,
            devices,
            observations,
            duration=0.01,
            window_index=0,
            window_band_counts=band_counts,
        )
    await conn1.close()
    await conn2.close()


async def test_migration_0007_creates_window_band_counts(
    tmp_path: Path,
) -> None:
    conn = await connect(tmp_path / "m7.db")
    applied = await apply_migrations(conn)
    assert "0007_window_band_counts.sql" in applied
    cur = await conn.execute("PRAGMA table_info(window_band_counts)")
    cols = {row[1] for row in await cur.fetchall()}
    await cur.close()
    assert {
        "id",
        "site_id",
        "window_index",
        "observed_at",
        "count_all",
        "count_far",
        "count_near",
        "count_adjacent",
    } <= cols
    await conn.close()
