# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Daemon wiring tests (P2-4 integration, #105).

The `run` daemon runs the scan loop and the outbox drain concurrently,
each on its own connection (#91), delivering through the config-selected
notifier. These pin the supervision (fail-loud, cancel-sibling), the
notifier selection, and that a queued message is actually delivered
while scanning — without hardware or a live bot token.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from blesentry.cli import (
    _resolve_run_settings,
    _run_supervised,
    build_parser,
    main,
)
from blesentry.drain import run_drain
from blesentry.loop import run_loop
from blesentry.notifier.mock import MockNotifier
from blesentry.notifier.models import OutboundMessage
from blesentry.notifier.null import NullNotifier
from blesentry.scanner.mock import MockScanner
from blesentry.storage import (
    DeviceRepository,
    ObservationRepository,
    OutboxRepository,
    apply_migrations,
    connect,
)

SITE = "daemon-site"


# --- _run_supervised: fail-loud concurrent supervision ---------------


async def _returns(value: int) -> int:
    return value


async def _forever() -> int:
    while True:
        await asyncio.sleep(0.01)


async def _raises(exc: BaseException) -> int:
    raise exc


async def test_supervised_returns_scan_result_and_stops_drain() -> None:
    result = await _run_supervised(_returns(3), _forever())
    assert result == 3


async def test_supervised_propagates_drain_failure() -> None:
    with pytest.raises(RuntimeError, match="drain died"):
        await _run_supervised(_forever(), _raises(RuntimeError("drain died")))


async def test_supervised_propagates_scan_failure() -> None:
    with pytest.raises(ValueError, match="scan died"):
        await _run_supervised(_raises(ValueError("scan died")), _forever())


async def test_supervised_cancellation_cancels_both_children() -> None:
    cancelled: set[str] = set()

    async def _track(name: str) -> int:
        try:
            while True:
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            cancelled.add(name)
            raise

    supervisor = asyncio.ensure_future(
        _run_supervised(_track("scan"), _track("drain"))
    )
    await asyncio.sleep(0.02)  # let both children start
    supervisor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await supervisor
    assert cancelled == {"scan", "drain"}


# --- notifier selection ----------------------------------------------


def _args(*extra: str) -> argparse.Namespace:
    return build_parser().parse_args(["run", *extra])


def test_flag_mode_uses_null_notifier(tmp_path: Path) -> None:
    settings = _resolve_run_settings(
        _args("--db", str(tmp_path / "x.db"), "--site-id", "s")
    )
    assert isinstance(settings.notifier, NullNotifier)


def _write_config(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


async def test_config_none_backend_uses_null_notifier(
    tmp_path: Path,
) -> None:
    cfg = _write_config(
        tmp_path / "c.toml",
        'site_id = "s"\n[storage]\ndb = "x.db"\n'
        '[scanner]\nbackend = "mock"\n'
        '[notifier]\nbackend = "none"\n',
    )
    settings = _resolve_run_settings(_args("--config", str(cfg)))
    assert isinstance(settings.notifier, NullNotifier)
    await settings.notifier.aclose()


async def test_config_telegram_backend_builds_telegram_notifier(
    tmp_path: Path,
) -> None:
    from blesentry.notifier.telegram import TelegramNotifier

    cfg = _write_config(
        tmp_path / "c.toml",
        'site_id = "s"\n[storage]\ndb = "x.db"\n'
        '[scanner]\nbackend = "mock"\n'
        '[notifier]\nbackend = "telegram"\n'
        'bot_token = "T"\nchat_id = 1\nuser_id = 2\n',
    )
    settings = _resolve_run_settings(_args("--config", str(cfg)))
    assert isinstance(settings.notifier, TelegramNotifier)
    await settings.notifier.aclose()


# --- concurrent drain delivers while scanning ------------------------


@pytest.fixture
async def db_path(tmp_path: Path) -> AsyncIterator[Path]:
    yield tmp_path / "daemon.db"


async def test_drain_delivers_queued_message_while_scanning(
    db_path: Path,
) -> None:
    # Two connections on one WAL database — the #91 dedicated-connection
    # split the daemon uses.
    scan_conn = await connect(db_path)
    await apply_migrations(scan_conn)
    drain_conn = await connect(db_path)
    try:
        outbox = OutboxRepository(drain_conn, SITE)
        queued = await outbox.enqueue(
            payload=OutboundMessage(text="intrusion!").model_dump_json()
        )
        notifier = MockNotifier()
        cycles = await _run_supervised(
            run_loop(
                MockScanner(scenarios=[[], [], []]),
                DeviceRepository(scan_conn, SITE),
                ObservationRepository(scan_conn, SITE),
                duration=0.01,
                pause=0.0,
                max_cycles=3,
            ),
            run_drain(outbox, notifier, poll=0.0),
        )
        assert cycles == 3
        # The drain delivered the queued alert concurrently with scanning.
        assert [m.text for m in notifier.sent] == ["intrusion!"]
        row = await outbox.get(queued)
        assert row is not None and row["status"] == "DELIVERED"
    finally:
        await scan_conn.close()
        await drain_conn.close()


# --- full `run` command, self-terminating via max_cycles -------------


def test_run_command_drains_outbox_end_to_end(tmp_path: Path) -> None:
    db = tmp_path / "e2e.db"
    cfg = _write_config(
        tmp_path / "c.toml",
        f'site_id = "{SITE}"\n[storage]\ndb = "{db}"\n'
        "[scan]\nwindow = 0.01\npause = 0.0\nmax_cycles = 3\n"
        '[scanner]\nbackend = "mock"\n'
        '[notifier]\nbackend = "none"\n',
    )

    async def _seed() -> int:
        conn = await connect(db)
        await apply_migrations(conn)
        outbox = OutboxRepository(conn, SITE)
        queued = await outbox.enqueue(
            payload=OutboundMessage(text="hi").model_dump_json()
        )
        await conn.close()
        return queued

    async def _status(outbox_id: int) -> str | None:
        conn = await connect(db)
        outbox = OutboxRepository(conn, SITE)
        row = await outbox.get(outbox_id)
        await conn.close()
        return row["status"] if row is not None else None

    queued = asyncio.run(_seed())
    exit_code = main(["run", "--config", str(cfg)])
    assert exit_code == 0
    # NullNotifier delivers (discards) → the drain marked it DELIVERED.
    assert asyncio.run(_status(queued)) == "DELIVERED"
