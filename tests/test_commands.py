# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin command tests (P2-8, #29).

Each command handler is unit-tested; the command loop is tested over
MockNotifier; and the single-operator auth (a wrong user_id is ignored)
is pinned end-to-end through a TelegramNotifier with a mock transport.
No hardware, no live token.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import httpx
import pytest

from blesentry.commands import (
    HELP_TEXT,
    IGNORED_LABEL,
    CommandContext,
    _format_duration,
    _format_size,
    _parse,
    dispatch,
    run_command_loop,
)
from blesentry.notifier.mock import MockNotifier
from blesentry.notifier.models import InboundCommand, OutboundMessage
from blesentry.notifier.telegram import TelegramNotifier
from blesentry.storage import (
    DeviceRepository,
    OutboxRepository,
    apply_migrations,
    connect,
)

SITE = "test-site"
USER = 200
CHAT = 100


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
def outbox(db: aiosqlite.Connection) -> OutboxRepository:
    return OutboxRepository(db, SITE)


def _cmd(text: str, *, user_id: int = USER) -> InboundCommand:
    return InboundCommand(
        chat_id=CHAT, user_id=user_id, message_id=1, text=text
    )


async def _reply(
    text: str,
    devices: DeviceRepository,
    outbox: OutboxRepository,
    *,
    db_path: str = "x.db",
    clock: float = 0.0,
    started_at: float = 0.0,
    user_id: int = USER,
) -> str:
    ctx = CommandContext(
        _cmd(text, user_id=user_id),
        devices,
        outbox,
        db_path,
        lambda: clock,
        started_at,
    )
    return await dispatch(ctx)


# --- parsing ---------------------------------------------------------


def test_parse_strips_slash_bot_and_lowercases() -> None:
    assert _parse("/Status@mybot") == ("status", "")
    assert _parse("/label 5 Front Door") == ("label", "5 Front Door")
    assert _parse("  plain text  ") == ("plain", "text")


# --- help / unknown --------------------------------------------------


async def test_help(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    assert "commands:" in await _reply("/help", devices, outbox)


async def test_unknown_command_shows_help(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    reply = await _reply("/frobnicate", devices, outbox)
    assert "unknown command" in reply
    assert HELP_TEXT in reply


# --- status ----------------------------------------------------------


async def test_status_reports_fields(
    devices: DeviceRepository, outbox: OutboxRepository, tmp_path: Path
) -> None:
    await devices.upsert(fingerprint="fp")
    await outbox.enqueue(payload="pending")
    db_file = tmp_path / "s.db"
    db_file.write_bytes(b"x" * 2048)
    reply = await _reply(
        "/status",
        devices,
        outbox,
        db_path=str(db_file),
        clock=3661.0,
        started_at=0.0,
    )
    assert "uptime: 1h 1m" in reply
    assert "devices: 1" in reply
    assert "outbox depth: 1" in reply
    assert "db size: 2.0 KB" in reply


# --- list ------------------------------------------------------------


async def test_list_empty(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    assert await _reply("/list", devices, outbox) == "no devices yet"


async def test_list_shows_devices(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    labeled = await devices.upsert(
        fingerprint="fp1", address="AA:BB:CC:DD:EE:FF"
    )
    await devices.set_label(labeled, label="Phone", actor="op")
    await devices.upsert(fingerprint="fp2", address="11:22:33:44:55:66")
    reply = await _reply("/list", devices, outbox)
    assert "Phone" in reply
    assert "(unlabeled)" in reply
    assert "AA:BB:CC:DD:EE:FF" in reply


async def test_list_caps_long_output(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    for i in range(55):
        await devices.upsert(fingerprint=f"fp-{i:03d}")
    reply = await _reply("/list", devices, outbox)
    assert "…and 5 more (55 total)" in reply
    assert len(reply.splitlines()) <= 52  # header + 50 rows + footer


async def test_list_sanitizes_newlines_in_label(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    device_id = await devices.upsert(fingerprint="fp")
    # A label with a newline could otherwise forge an extra list row.
    await devices.set_label(device_id, label="Door\n  999: Fake", actor="op")
    reply = await _reply("/list", devices, outbox)
    assert "Door 999: Fake" in reply  # collapsed onto one line
    assert "\n  999: Fake" not in reply


# --- label / unlabel -------------------------------------------------


async def test_label_sets_and_audits(
    devices: DeviceRepository,
    outbox: OutboxRepository,
    db: aiosqlite.Connection,
) -> None:
    device_id = await devices.upsert(fingerprint="fp")
    reply = await _reply(
        f"/label {device_id} Living Room", devices, outbox, user_id=200
    )
    assert reply == f"labeled device {device_id}: Living Room"
    row = await devices.get(device_id)
    assert row is not None and row["label"] == "Living Room"
    cur = await db.execute(
        "SELECT actor, new_label FROM label_audit WHERE device_id = ?",
        (device_id,),
    )
    audit = await cur.fetchone()
    await cur.close()
    assert audit == ("tg:200", "Living Room")


async def test_label_usage_errors(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    assert "usage" in await _reply("/label", devices, outbox)
    assert "usage" in await _reply("/label 5", devices, outbox)
    assert "usage" in await _reply("/label abc name", devices, outbox)


async def test_label_unknown_device(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    assert await _reply("/label 999 x", devices, outbox) == "no device 999"


async def test_label_oversized_id_does_not_crash(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    # An id beyond int64 must not raise OverflowError at the SQL layer.
    reply = await _reply("/label 99999999999999999999999 x", devices, outbox)
    assert "usage" in reply


async def test_unlabel_clears(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    device_id = await devices.upsert(fingerprint="fp")
    await devices.set_label(device_id, label="X", actor="op")
    reply = await _reply(f"/unlabel {device_id}", devices, outbox)
    assert reply == f"cleared label on device {device_id}"
    row = await devices.get(device_id)
    assert row is not None and row["label"] is None


async def test_unlabel_usage(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    assert "usage" in await _reply("/unlabel", devices, outbox)
    assert "usage" in await _reply("/unlabel abc", devices, outbox)


# --- describe --------------------------------------------------------


async def test_describe_sets_note(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    device_id = await devices.upsert(fingerprint="fp")
    reply = await _reply(
        f"/describe {device_id} kitchen sensor", devices, outbox
    )
    assert reply == f"described device {device_id}"
    row = await devices.get(device_id)
    assert row is not None and row["description"] == "kitchen sensor"


async def test_describe_usage(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    assert "usage" in await _reply("/describe 5", devices, outbox)


async def test_ignore_sets_sentinel_label(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    device_id = await devices.upsert(fingerprint="fp")
    reply = await _reply(f"/ignore {device_id}", devices, outbox)
    assert "no more alerts" in reply
    row = await devices.get(device_id)
    assert row is not None and row["label"] == IGNORED_LABEL


async def test_ignore_usage(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    assert "usage" in await _reply("/ignore", devices, outbox)


# --- run_command_loop over MockNotifier ------------------------------


async def test_command_loop_enqueues_replies_to_outbox(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    device_id = await devices.upsert(fingerprint="fp")
    notifier = MockNotifier(
        inbound=[_cmd("/help"), _cmd(f"/label {device_id} Door")]
    )
    processed = await run_command_loop(
        notifier,
        devices,
        outbox,
        db_path="x.db",
        started_at=0.0,
        clock=lambda: 0.0,
    )
    assert processed == 2
    # Replies flow through the outbox (ADR-0003), not fire-and-forget.
    replies = [
        OutboundMessage.model_validate_json(row["payload"]).text
        for row in await outbox.list_pending()
    ]
    assert any("commands:" in r for r in replies)
    assert f"labeled device {device_id}: Door" in replies
    # The label command actually took effect.
    row = await devices.get(device_id)
    assert row is not None and row["label"] == "Door"


# --- auth: a wrong user_id never reaches a handler -------------------


async def test_wrong_user_id_is_never_dispatched(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    device_id = await devices.upsert(fingerprint="fp")

    def handler(request: httpx.Request) -> httpx.Response:
        # Two updates: an unauthorized /label (wrong user), then an
        # authorized /status. Only the authorized one is yielded.
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 1,
                            "from": {"id": 999},  # wrong user
                            "chat": {"id": CHAT},
                            "text": f"/label {device_id} HACKED",
                        },
                    },
                    {
                        "update_id": 2,
                        "message": {
                            "message_id": 2,
                            "from": {"id": USER},
                            "chat": {"id": CHAT},
                            "text": "/status",
                        },
                    },
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notifier = TelegramNotifier(
        bot_token="T",
        chat_id=CHAT,
        user_id=USER,
        client=client,
        error_backoff=0.0,
    )
    processed = await run_command_loop(
        notifier,
        devices,
        outbox,
        db_path="x.db",
        started_at=0.0,
        clock=lambda: 0.0,
        max_commands=1,  # stop after the one authorized command
    )
    await notifier.aclose()

    assert processed == 1
    # The unauthorized /label never ran — the device is still unlabeled.
    row = await devices.get(device_id)
    assert row is not None and row["label"] is None
    # The one reply is the authorized /status.
    replies = [
        OutboundMessage.model_validate_json(row["payload"]).text
        for row in await outbox.list_pending()
    ]
    assert len(replies) == 1
    assert replies[0].startswith("status:")


# --- formatting helpers ----------------------------------------------


def test_format_duration() -> None:
    assert _format_duration(45) == "45s"
    assert _format_duration(90) == "1m 30s"
    assert _format_duration(3661) == "1h 1m"
    assert _format_duration(90061) == "1d 1h 1m"


def test_format_size_unknown_on_missing_file() -> None:
    assert _format_size("/no/such/file.db") == "unknown"


def test_format_size_units(tmp_path: Path) -> None:
    small = tmp_path / "a"
    small.write_bytes(b"x" * 512)
    assert _format_size(str(small)) == "512 B"
    big = tmp_path / "b"
    big.write_bytes(b"x" * 3072)
    assert _format_size(str(big)) == "3.0 KB"
