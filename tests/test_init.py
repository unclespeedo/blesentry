# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Init-mode bulk labeling (P2-7, #28).

DoD: a MockNotifier session labels 5 currently-PRESENT unlabeled
devices; a partial session resumes at the next snapshot member.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest

from blesentry.commands import (
    HELP_TEXT,
    IGNORED_LABEL,
    CommandContext,
    dispatch,
    run_command_loop,
)
from blesentry.init import DEFAULT_TIMEOUT_SECONDS
from blesentry.loop import iso_utc
from blesentry.notifier.mock import MockNotifier
from blesentry.notifier.models import InboundCommand, OutboundMessage
from blesentry.storage import (
    DeviceRepository,
    InitSessionRepository,
    OutboxRepository,
    PresenceEventRepository,
    apply_migrations,
    connect,
)

SITE = "init-site"
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


@pytest.fixture
def presence(db: aiosqlite.Connection) -> PresenceEventRepository:
    return PresenceEventRepository(db, SITE)


def _cmd(text: str, *, user_id: int = USER) -> InboundCommand:
    return InboundCommand(
        chat_id=CHAT, user_id=user_id, message_id=1, text=text
    )


async def _reply(
    text: str,
    devices: DeviceRepository,
    outbox: OutboxRepository,
    *,
    now: float = 1_700_000_000.0,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    user_id: int = USER,
) -> str:
    ctx = CommandContext(
        _cmd(text, user_id=user_id),
        devices,
        outbox,
        "x.db",
        lambda: 0.0,
        0.0,
        InitSessionRepository(devices.connection, devices.site_id),
        lambda: now,
        timeout,
    )
    return await dispatch(ctx)


async def _seed_present(
    devices: DeviceRepository,
    presence: PresenceEventRepository,
    count: int,
) -> list[int]:
    ids: list[int] = []
    for i in range(count):
        device_id = await devices.upsert(
            fingerprint=f"fp-{i:02d}",
            address=f"AA:00:00:00:00:{i:02X}",
        )
        await presence.append(
            device_id=device_id,
            event_type="PRESENT",
            occurred_at="2026-01-15T01:00:00.000Z",
        )
        ids.append(device_id)
    return ids


async def test_init_session_labels_five_devices_over_mock_bot(
    devices: DeviceRepository,
    outbox: OutboxRepository,
    presence: PresenceEventRepository,
) -> None:
    ids = await _seed_present(devices, presence, 5)
    names = ["One", "Two", "Three", "Four", "Five"]
    notifier = MockNotifier(
        inbound=[_cmd("/init"), *(_cmd(name) for name in names)]
    )
    processed = await run_command_loop(
        notifier,
        devices,
        outbox,
        db_path="x.db",
        started_at=0.0,
        clock=lambda: 0.0,
        now=lambda: 1_700_000_000.0,
    )
    assert processed == 6
    for device_id, name in zip(ids, names, strict=True):
        row = await devices.get(device_id)
        assert row is not None and row["label"] == name
    replies = [
        OutboundMessage.model_validate_json(row["payload"]).text
        for row in await outbox.list_pending()
    ]
    assert any("init 1/5" in r for r in replies)
    assert any("labeled device" in r for r in replies)
    assert any("done" in r.lower() and "5" in r for r in replies)


async def test_partial_init_session_resumes(
    devices: DeviceRepository,
    outbox: OutboxRepository,
    presence: PresenceEventRepository,
) -> None:
    ids = await _seed_present(devices, presence, 5)
    first = MockNotifier(inbound=[_cmd("/init"), _cmd("Alpha"), _cmd("Beta")])
    await run_command_loop(
        first,
        devices,
        outbox,
        db_path="x.db",
        started_at=0.0,
        clock=lambda: 0.0,
        now=lambda: 1_700_000_000.0,
    )
    first_row = await devices.get(ids[0])
    second_row = await devices.get(ids[1])
    third_row = await devices.get(ids[2])
    assert first_row is not None and first_row["label"] == "Alpha"
    assert second_row is not None and second_row["label"] == "Beta"
    assert third_row is not None and third_row["label"] is None

    second = MockNotifier(
        inbound=[
            _cmd("/init"),
            _cmd("Gamma"),
            _cmd("Delta"),
            _cmd("Epsilon"),
        ]
    )
    await run_command_loop(
        second,
        devices,
        outbox,
        db_path="x.db",
        started_at=0.0,
        clock=lambda: 0.0,
        now=lambda: 1_700_000_000.0,
    )
    labels = []
    for device_id in ids:
        row = await devices.get(device_id)
        assert row is not None
        labels.append(row["label"])
    assert labels == ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]


async def test_init_resume_keeps_start_snapshot(
    devices: DeviceRepository,
    outbox: OutboxRepository,
    presence: PresenceEventRepository,
) -> None:
    ids = await _seed_present(devices, presence, 2)
    await _reply("/init", devices, outbox)
    await _reply("First", devices, outbox)
    # A third device appears after the snapshot; resume must not add it.
    late = await devices.upsert(
        fingerprint="fp-late", address="FF:00:00:00:00:01"
    )
    await presence.append(
        device_id=late,
        event_type="PRESENT",
        occurred_at="2026-01-15T01:05:00.000Z",
    )
    resume = await _reply("/init", devices, outbox)
    assert f"device {ids[1]}" in resume
    assert f"device {late}" not in resume
    await _reply("Second", devices, outbox)
    late_row = await devices.get(late)
    assert late_row is not None and late_row["label"] is None


async def test_init_with_no_present_unlabeled(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    reply = await _reply("/init", devices, outbox)
    assert "no unlabeled" in reply.lower()


async def test_init_skip_and_done(
    devices: DeviceRepository,
    outbox: OutboxRepository,
    presence: PresenceEventRepository,
) -> None:
    ids = await _seed_present(devices, presence, 3)
    await _reply("/init", devices, outbox)
    await _reply("/skip", devices, outbox)
    await _reply("Kept", devices, outbox)
    await _reply("/done", devices, outbox)
    skipped = await devices.get(ids[0])
    kept = await devices.get(ids[1])
    leftover = await devices.get(ids[2])
    assert skipped is not None and skipped["label"] is None
    assert kept is not None and kept["label"] == "Kept"
    assert leftover is not None and leftover["label"] is None
    again = await _reply("/skip", devices, outbox)
    assert "no init session" in again.lower()


async def test_init_ignore_current_advances(
    devices: DeviceRepository,
    outbox: OutboxRepository,
    presence: PresenceEventRepository,
) -> None:
    ids = await _seed_present(devices, presence, 2)
    await _reply("/init", devices, outbox)
    reply = await _reply("/ignore", devices, outbox)
    assert f"device {ids[1]}" in reply
    row = await devices.get(ids[0])
    assert row is not None and row["label"] == IGNORED_LABEL
    await _reply("Named", devices, outbox)
    named = await devices.get(ids[1])
    assert named is not None and named["label"] == "Named"


async def test_init_cancel_abandons_remaining(
    devices: DeviceRepository,
    outbox: OutboxRepository,
    presence: PresenceEventRepository,
) -> None:
    ids = await _seed_present(devices, presence, 2)
    await _reply("/init", devices, outbox)
    await _reply("OnlyFirst", devices, outbox)
    cancelled = await _reply("/init cancel", devices, outbox)
    assert "cancel" in cancelled.lower()
    remaining = await devices.get(ids[1])
    assert remaining is not None and remaining["label"] is None
    # A new /init starts a fresh snapshot of remaining unlabeled.
    fresh = await _reply("/init", devices, outbox)
    assert "1/1" in fresh


async def test_expired_session_starts_fresh(
    devices: DeviceRepository,
    outbox: OutboxRepository,
    presence: PresenceEventRepository,
) -> None:
    await _seed_present(devices, presence, 2)
    start = 1_700_000_000.0
    await _reply("/init", devices, outbox, now=start, timeout=60.0)
    expired = await _reply(
        "/init", devices, outbox, now=start + 120.0, timeout=60.0
    )
    assert "expired" in expired.lower() or "init 1/2" in expired
    # The first device was never labeled; a fresh snapshot still has both.
    assert "1/2" in expired or "init 1/2" in await _reply(
        "/init", devices, outbox, now=start + 121.0, timeout=60.0
    )


async def test_expired_free_text_does_not_label(
    devices: DeviceRepository,
    outbox: OutboxRepository,
    presence: PresenceEventRepository,
) -> None:
    ids = await _seed_present(devices, presence, 1)
    start = 1_700_000_000.0
    await _reply("/init", devices, outbox, now=start, timeout=30.0)
    reply = await _reply(
        "ShouldNotStick", devices, outbox, now=start + 90.0, timeout=30.0
    )
    assert "expired" in reply.lower() or "unknown command" in reply.lower()
    stuck = await devices.get(ids[0])
    assert stuck is not None and stuck["label"] is None


async def test_help_lists_init(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    assert "/init" in HELP_TEXT
    assert "/init" in await _reply("/help", devices, outbox)


async def test_init_prompt_sanitizes_address(
    devices: DeviceRepository,
    outbox: OutboxRepository,
    presence: PresenceEventRepository,
) -> None:
    device_id = await devices.upsert(
        fingerprint="fp-esc", address="AA\n  999: Fake\x1b[2J"
    )
    await presence.append(
        device_id=device_id,
        event_type="PRESENT",
        occurred_at="2026-01-15T01:00:00.000Z",
    )
    reply = await _reply("/init", devices, outbox)
    assert "\n  999: Fake" not in reply
    assert "\x1b" not in reply
    assert f"device {device_id}" in reply


async def test_timeout_constant_is_thirty_minutes() -> None:
    assert DEFAULT_TIMEOUT_SECONDS == 1800.0
    # expires_at is a comparable ISO stamp one timeout after start.
    start = 1_700_000_000.0
    assert iso_utc(start + DEFAULT_TIMEOUT_SECONDS) > iso_utc(start)


async def test_cli_session_labels_then_completes(
    devices: DeviceRepository,
    presence: PresenceEventRepository,
) -> None:
    from blesentry.init import run_cli_session

    ids = await _seed_present(devices, presence, 2)
    lines = iter(["Kitchen", "TV"])
    outputs: list[str] = []
    code = await run_cli_session(
        devices,
        InitSessionRepository(devices.connection, devices.site_id),
        readline=lambda: next(lines, None),
        write=outputs.append,
        now=lambda: 1_700_000_000.0,
        actor="cli",
    )
    assert code == 0
    assert any("init 1/2" in line for line in outputs)
    assert any("init done: labeled 2" in line for line in outputs)
    kitchen = await devices.get(ids[0])
    tv = await devices.get(ids[1])
    assert kitchen is not None and kitchen["label"] == "Kitchen"
    assert tv is not None and tv["label"] == "TV"


async def test_stale_cursor_does_not_overwrite_label(
    devices: DeviceRepository,
    presence: PresenceEventRepository,
) -> None:
    """A second consumer holding an old snapshot must not relabel."""
    from blesentry.init import InitDeps, _apply_current, start_or_resume

    ids = await _seed_present(devices, presence, 2)
    sessions = InitSessionRepository(devices.connection, devices.site_id)
    deps = InitDeps(
        devices,
        sessions,
        actor="tg:200",
        now=lambda: 1_700_000_000.0,
        timeout=1800.0,
    )
    await start_or_resume(deps)
    stale = await sessions.get_active()
    assert stale is not None
    await _apply_current(deps, stale, label="One")
    reply = await _apply_current(deps, stale, label="Stale")
    first = await devices.get(ids[0])
    second = await devices.get(ids[1])
    assert first is not None and first["label"] == "One"
    assert second is not None and second["label"] is None
    assert f"device {ids[1]}" in reply


async def test_init_survives_connection_reopen(tmp_path: Path) -> None:
    """DoD resume: a new connection sees the persisted cursor."""
    db_path = tmp_path / "init.db"
    conn = await connect(db_path)
    try:
        await apply_migrations(conn)
        devices = DeviceRepository(conn, SITE)
        outbox = OutboxRepository(conn, SITE)
        presence = PresenceEventRepository(conn, SITE)
        ids = await _seed_present(devices, presence, 2)
        await _reply("/init", devices, outbox)
        await _reply("Alpha", devices, outbox)
    finally:
        await conn.close()
    conn2 = await connect(db_path)
    try:
        devices = DeviceRepository(conn2, SITE)
        outbox = OutboxRepository(conn2, SITE)
        resume = await _reply("/init", devices, outbox)
        assert f"device {ids[1]}" in resume
        await _reply("Beta", devices, outbox)
        first = await devices.get(ids[0])
        second = await devices.get(ids[1])
        assert first is not None and first["label"] == "Alpha"
        assert second is not None and second["label"] == "Beta"
    finally:
        await conn2.close()


async def test_status_still_works_during_init(
    devices: DeviceRepository,
    outbox: OutboxRepository,
    presence: PresenceEventRepository,
) -> None:
    await _seed_present(devices, presence, 1)
    await _reply("/init", devices, outbox)
    reply = await _reply("/status", devices, outbox)
    assert reply.startswith("status:")
    active = await InitSessionRepository(
        devices.connection, devices.site_id
    ).get_active()
    assert active is not None


async def test_cli_eof_pauses_session(
    devices: DeviceRepository,
    presence: PresenceEventRepository,
) -> None:
    from blesentry.init import run_cli_session

    await _seed_present(devices, presence, 2)
    outputs: list[str] = []
    code = await run_cli_session(
        devices,
        InitSessionRepository(devices.connection, devices.site_id),
        readline=lambda: None,
        write=outputs.append,
        now=lambda: 1_700_000_000.0,
        actor="cli",
    )
    assert code == 0
    assert any("paused" in line for line in outputs)
    active = await InitSessionRepository(
        devices.connection, devices.site_id
    ).get_active()
    assert active is not None
