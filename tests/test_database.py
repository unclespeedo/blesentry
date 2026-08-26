# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for the SQLite bootstrap, migration runner, and v1 schema."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from blesentry.storage import (
    MigrationError,
    apply_migrations,
    connect,
    transaction,
)

EXPECTED_TABLES = (
    "devices",
    "observations",
    "presence_events",
    "outbox",
    "label_audit",
    "device_aliases",
)

SCHEMA_V1 = "0001_schema_v1.sql"


async def _tables(conn: aiosqlite.Connection) -> set[str]:
    cur = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )
    rows = await cur.fetchall()
    await cur.close()
    return {row[0] for row in rows}


async def _pragma(conn: aiosqlite.Connection, pragma: str) -> str:
    cur = await conn.execute(f"PRAGMA {pragma}")
    row = await cur.fetchone()
    await cur.close()
    return str(row[0]) if row else ""


def _write_migration(directory: Path, name: str, sql: str) -> Path:
    path = directory / name
    path.write_text(sql, encoding="utf-8")
    return path


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    db = await connect(tmp_path / "test.db")
    try:
        yield db
    finally:
        await db.close()


async def test_connect_opens_wal_database(conn: aiosqlite.Connection) -> None:
    assert await _pragma(conn, "journal_mode") == "wal"


async def test_connect_sets_pragmas(conn: aiosqlite.Connection) -> None:
    assert await _pragma(conn, "synchronous") == "1"
    assert await _pragma(conn, "foreign_keys") == "1"
    assert int(await _pragma(conn, "busy_timeout")) > 0


async def test_wal_persists_across_reconnect(tmp_path: Path) -> None:
    path = tmp_path / "wal.db"
    db = await connect(path)
    await apply_migrations(db)
    await db.close()
    db = await connect(path)
    try:
        assert await _pragma(db, "journal_mode") == "wal"
    finally:
        await db.close()


async def test_apply_migrations_returns_all_versions(
    conn: aiosqlite.Connection,
) -> None:
    applied = await apply_migrations(conn)
    assert SCHEMA_V1 in applied


async def test_migrations_are_idempotent(
    conn: aiosqlite.Connection,
) -> None:
    first = await apply_migrations(conn)
    second = await apply_migrations(conn)
    assert first[0] == SCHEMA_V1
    assert second == []
    cur = await conn.execute("SELECT COUNT(*) FROM schema_migrations")
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    assert row[0] == len(first)


async def test_all_v1_tables_created(conn: aiosqlite.Connection) -> None:
    await apply_migrations(conn)
    tables = await _tables(conn)
    assert set(EXPECTED_TABLES) <= tables
    assert "schema_migrations" in tables


async def test_every_table_has_site_id(conn: aiosqlite.Connection) -> None:
    await apply_migrations(conn)
    for name in EXPECTED_TABLES:
        cur = await conn.execute(f"PRAGMA table_info({name})")
        cols = {row[1] for row in await cur.fetchall()}
        await cur.close()
        assert "site_id" in cols, f"{name} is missing site_id"


async def test_check_constraints_enforced(
    conn: aiosqlite.Connection,
) -> None:
    await apply_migrations(conn)
    await conn.execute(
        "INSERT INTO devices (site_id, fingerprint) VALUES (?, ?)",
        ("site-a", "fp-1"),
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(
            "INSERT INTO presence_events "
            "(site_id, device_id, event_type, occurred_at) "
            "VALUES (?, ?, ?, ?)",
            ("site-a", 1, "WANDERING", "2026-01-01T00:00:00Z"),
        )


async def test_foreign_keys_enforced(conn: aiosqlite.Connection) -> None:
    await apply_migrations(conn)
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(
            "INSERT INTO observations "
            "(site_id, device_id, rssi, observed_at) "
            "VALUES (?, ?, ?, ?)",
            ("site-a", 999, -70, "2026-01-01T00:00:00Z"),
        )


async def test_custom_migrations_apply_in_filename_order(
    tmp_path: Path,
) -> None:
    _write_migration(
        tmp_path,
        "0001_first.sql",
        "CREATE TABLE first (id INTEGER PRIMARY KEY);",
    )
    _write_migration(
        tmp_path,
        "0002_second.sql",
        "CREATE TABLE second (first_id INTEGER REFERENCES first(id));",
    )
    db = await connect(tmp_path / "test.db")
    try:
        applied = await apply_migrations(db, migrations_dir=tmp_path)
        assert applied == ["0001_first.sql", "0002_second.sql"]
    finally:
        await db.close()


async def test_failed_migration_rolls_back_atomically(
    tmp_path: Path,
) -> None:
    _write_migration(
        tmp_path,
        "0001_broken.sql",
        "CREATE TABLE should_not_persist (id INTEGER PRIMARY KEY);\n"
        "THIS IS NOT SQL;",
    )
    db = await connect(tmp_path / "test.db")
    try:
        with pytest.raises(MigrationError):
            await apply_migrations(db, migrations_dir=tmp_path)
        assert "should_not_persist" not in await _tables(db)
    finally:
        await db.close()


async def test_failed_migration_writes_no_bookkeeping_row(
    tmp_path: Path,
) -> None:
    broken = _write_migration(
        tmp_path,
        "0001_partial.sql",
        "CREATE TABLE partial (id INTEGER PRIMARY KEY);\nTHIS IS NOT SQL;",
    )
    db = await connect(tmp_path / "test.db")
    try:
        with pytest.raises(MigrationError):
            await apply_migrations(db, migrations_dir=tmp_path)
        assert "partial" not in await _tables(db)
        cur = await db.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
            ("0001_partial.sql",),
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
        assert row[0] == 0
        broken.write_text(
            "CREATE TABLE partial (id INTEGER PRIMARY KEY);",
            encoding="utf-8",
        )
        applied = await apply_migrations(db, migrations_dir=tmp_path)
        assert "0001_partial.sql" in applied
    finally:
        await db.close()


async def test_mutated_migration_is_detected(tmp_path: Path) -> None:
    script = _write_migration(
        tmp_path,
        "0001_stable.sql",
        "CREATE TABLE stable (id INTEGER PRIMARY KEY);",
    )
    db = await connect(tmp_path / "test.db")
    try:
        await apply_migrations(db, migrations_dir=tmp_path)
        script.write_text(
            "CREATE TABLE stable (id INTEGER PRIMARY KEY, extra TEXT);",
            encoding="utf-8",
        )
        with pytest.raises(MigrationError, match="changed"):
            await apply_migrations(db, migrations_dir=tmp_path)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_0002_applies_address_provenance(tmp_path) -> None:
    conn = await connect(tmp_path / "m2.db")
    applied = await apply_migrations(conn)
    assert "0002_address_provenance.sql" in applied
    cur = await conn.execute("PRAGMA table_info(observations)")
    cols = {row[1] for row in await cur.fetchall()}
    await cur.close()
    assert {"address_type", "adv_type"} <= cols
    cur = await conn.execute("PRAGMA table_info(devices)")
    dcols = {row[1] for row in await cur.fetchall()}
    await cur.close()
    assert "address" in dcols and "mac" not in dcols
    await conn.close()


@pytest.mark.asyncio
async def test_migration_0002_preserves_v1_data(tmp_path) -> None:
    """Upgrade path: a populated v1 database renames without data loss."""
    db = tmp_path / "upgrade.db"
    conn = await connect(db)
    only_v1 = tmp_path / "v1"
    only_v1.mkdir()
    import shutil

    from blesentry.storage.database import DEFAULT_MIGRATIONS_DIR

    shutil.copy(
        DEFAULT_MIGRATIONS_DIR / "0001_schema_v1.sql",
        only_v1 / "0001_schema_v1.sql",
    )
    await apply_migrations(conn, migrations_dir=only_v1)
    await conn.execute(
        "INSERT INTO devices (site_id, fingerprint, mac) "
        "VALUES ('s', 'fp', 'AA:BB:CC:DD:EE:FF')"
    )
    await conn.commit()
    await conn.execute(
        "INSERT INTO observations "
        "(site_id, device_id, rssi, observed_at) "
        "VALUES ('s', 1, -60, '2025-01-01T00:00:00.000Z')"
    )
    await conn.commit()
    applied = await apply_migrations(conn)
    assert "0002_address_provenance.sql" in applied
    cur = await conn.execute("SELECT address FROM devices")
    row = await cur.fetchone()
    await cur.close()
    assert row is not None and row[0] == "AA:BB:CC:DD:EE:FF"
    cur = await conn.execute("PRAGMA foreign_key_check")
    assert await cur.fetchall() == []
    await cur.close()
    cur = await conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='devices'"
    )
    names = {r[0] for r in await cur.fetchall()}
    await cur.close()
    assert "idx_devices_address" in names
    assert "idx_devices_mac" not in names
    cur = await conn.execute(
        "SELECT address_type FROM observations WHERE id = 1"
    )
    old = await cur.fetchone()
    await cur.close()
    assert old is not None and old[0] is None
    await conn.close()


@pytest.mark.asyncio
async def test_migration_0004_creates_device_aliases(tmp_path) -> None:
    conn = await connect(tmp_path / "m4.db")
    applied = await apply_migrations(conn)
    assert "0004_device_aliases.sql" in applied
    cur = await conn.execute("PRAGMA table_info(device_aliases)")
    cols = {row[1] for row in await cur.fetchall()}
    await cur.close()
    assert {
        "id",
        "site_id",
        "fingerprint",
        "device_id",
        "created_at",
        "updated_at",
    } <= cols
    cur = await conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'device_aliases'"
    )
    row = await cur.fetchone()
    await cur.close()
    assert row is not None
    sql = row[0].lower()
    assert "unique" in sql
    assert "references devices" in sql
    cur = await conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='index' AND tbl_name='device_aliases'"
    )
    names = {r[0] for r in await cur.fetchall()}
    await cur.close()
    assert "idx_device_aliases_device" in names
    await conn.close()


@pytest.mark.asyncio
async def test_transaction_commits_on_success(tmp_path) -> None:
    conn = await connect(tmp_path / "t.db")
    await apply_migrations(conn)
    async with transaction(conn):
        await conn.execute(
            "INSERT INTO devices (site_id, fingerprint) VALUES ('s','f')"
        )
    cur = await conn.execute("SELECT COUNT(*) FROM devices")
    row = await cur.fetchone()
    await cur.close()
    assert row is not None and row[0] == 1
    await conn.close()


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_error(tmp_path) -> None:
    conn = await connect(tmp_path / "t.db")
    await apply_migrations(conn)
    with pytest.raises(RuntimeError):
        async with transaction(conn):
            await conn.execute(
                "INSERT INTO devices (site_id, fingerprint) VALUES ('s','f')"
            )
            raise RuntimeError("boom")
    cur = await conn.execute("SELECT COUNT(*) FROM devices")
    row = await cur.fetchone()
    await cur.close()
    assert row is not None and row[0] == 0
    await conn.close()


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_cancellation(tmp_path) -> None:
    """BaseException (CancelledError) must also roll back."""
    conn = await connect(tmp_path / "t.db")
    await apply_migrations(conn)
    with pytest.raises(asyncio.CancelledError):
        async with transaction(conn):
            await conn.execute(
                "INSERT INTO devices (site_id, fingerprint) VALUES ('s','f')"
            )
            raise asyncio.CancelledError()
    cur = await conn.execute("SELECT COUNT(*) FROM devices")
    row = await cur.fetchone()
    await cur.close()
    assert row is not None and row[0] == 0
    await conn.close()


@pytest.mark.asyncio
async def test_transactions_nest_outermost_wins(tmp_path) -> None:
    conn = await connect(tmp_path / "t.db")
    await apply_migrations(conn)
    with pytest.raises(RuntimeError):
        async with transaction(conn):
            async with transaction(conn):
                await conn.execute(
                    "INSERT INTO devices (site_id, fingerprint) "
                    "VALUES ('s','f')"
                )
            raise RuntimeError("outer fails after inner exits")
    cur = await conn.execute("SELECT COUNT(*) FROM devices")
    row = await cur.fetchone()
    await cur.close()
    assert row is not None and row[0] == 0
    await conn.close()
