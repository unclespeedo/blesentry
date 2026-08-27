# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""SQLite connection bootstrap, transactions, and migration runner."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import aiosqlite

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
"""

_CONNECT_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)


class MigrationError(RuntimeError):
    """Raised when a migration is mutated, or cannot be applied."""


@asynccontextmanager
async def transaction(
    conn: aiosqlite.Connection,
) -> AsyncIterator[None]:
    """Ambient unit-of-work (#84): BEGIN/COMMIT owned by the outermost.

    Nested uses join the open transaction (only the outermost commits
    or rolls back). Rollback triggers on ``BaseException`` so task
    cancellation between BEGIN and COMMIT never abandons an open
    transaction on the connection.

    Contract: all transaction() users of one connection must run in a
    SINGLE asyncio task — nesting detection is connection-global, so
    interleaved tasks would corrupt each other's units of work. The
    P2 bot handler gets its own connection (requirement recorded on
    the notifier issue).

    ``BEGIN IMMEDIATE`` (not plain ``BEGIN``): every unit of work here
    writes, and several (the scan cycle, ``set_label``) read before they
    write. A deferred ``BEGIN`` would take the write lock only at the
    first write, so a concurrent connection (scan/drain/command loop on
    the same WAL database) committing after this unit's read snapshot
    makes the write-upgrade fail with ``SQLITE_BUSY_SNAPSHOT`` — which
    ``busy_timeout`` never retries (the snapshot is stale, waiting can't
    help). Acquiring the write lock up front makes ``busy_timeout``
    govern the contention normally instead.
    """
    outermost = not conn.in_transaction
    if outermost:
        await conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        if outermost:
            await conn.execute("ROLLBACK")
        raise
    else:
        if outermost:
            await conn.execute("COMMIT")


async def connect(path: str | Path) -> aiosqlite.Connection:
    """Open a SQLite database with connection-level pragmas applied.

    ``journal_mode=WAL`` persists in the database file; the remaining
    pragmas are per-connection and must be reapplied on every open.
    """
    conn = await aiosqlite.connect(path)
    try:
        for pragma in _CONNECT_PRAGMAS:
            await conn.execute(pragma)
    except aiosqlite.Error:
        await conn.close()
        raise
    return conn


async def connect_readonly(path: str | Path) -> aiosqlite.Connection:
    """Open an existing SQLite file read-only (F1 / DC-9).

    Does not create the file, does not run migrations, and does not
    set ``journal_mode`` (that pragma can mutate a non-WAL file).
    ``query_only`` plus URI ``mode=ro`` reject writes. Pass a copied
    snapshot, not the live daemon database — a long reader against
    the live WAL pins checkpointing.

    Args:
        path: Path to an existing database file.

    Returns:
        A connection that rejects writes.

    Raises:
        FileNotFoundError: ``path`` is not an existing file.
        aiosqlite.Error: SQLite refused the open or a pragma.
    """
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"snapshot not found: {resolved}")
    uri = f"file:{quote(resolved.as_posix(), safe='/')}?mode=ro"
    conn = await aiosqlite.connect(uri, uri=True)
    try:
        await conn.execute("PRAGMA query_only=ON")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
    except aiosqlite.Error:
        await conn.close()
        raise
    return conn


async def apply_migrations(
    conn: aiosqlite.Connection,
    *,
    migrations_dir: str | Path = DEFAULT_MIGRATIONS_DIR,
) -> list[str]:
    """Apply pending migrations in filename order.

    Returns the list of newly applied migration filenames. Idempotent:
    versions recorded in ``schema_migrations`` are skipped. A recorded
    version whose file checksum no longer matches raises MigrationError.
    Each migration and its bookkeeping row are written in a single
    transaction and roll back atomically, so the schema can never be
    applied without a corresponding ``schema_migrations`` entry.
    """
    await conn.executescript(_BOOTSTRAP)

    cur = await conn.execute("SELECT version, checksum FROM schema_migrations")
    rows = await cur.fetchall()
    await cur.close()
    recorded = {version: checksum for version, checksum in rows}

    applied: list[str] = []
    directory = Path(migrations_dir)
    for script in sorted(directory.glob("*.sql")):
        version = script.name
        checksum = _checksum(script)
        if version in recorded:
            if recorded[version] != checksum:
                raise MigrationError(
                    f"migration {version} changed after it was applied"
                )
            continue
        sql = script.read_text(encoding="utf-8")
        version_sql = version.replace("'", "''")
        try:
            await conn.executescript(
                "BEGIN;\n"
                f"{sql}\n"
                "INSERT INTO schema_migrations (version, checksum) "
                f"VALUES ('{version_sql}', '{checksum}');\n"
                "COMMIT;"
            )
        except aiosqlite.Error as exc:
            await conn.rollback()
            raise MigrationError(f"migration {version} failed: {exc}") from exc
        applied.append(version)
    return applied


def _checksum(path: Path) -> str:
    """Return a sha256 hex digest of the migration file contents."""
    return hashlib.sha256(path.read_bytes()).hexdigest()
