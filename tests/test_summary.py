# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Daily summary (P2-9): snapshot-tested digest + restart-safe schedule.

A digest covering devices seen, new devices, presence transitions, and
outbox health is enqueued through the outbox. A persisted last-sent
marker makes the schedule survive process death without double-send.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import aiosqlite
import pytest

from blesentry.loop import iso_utc
from blesentry.notifier.models import OutboundMessage
from blesentry.storage import apply_migrations, connect
from blesentry.storage.database import transaction as db_transaction
from blesentry.storage.repository import (
    DeviceRepository,
    ObservationRepository,
    OutboxRepository,
    PresenceEventRepository,
    SiteStateRepository,
)
from blesentry.summary import (
    LAST_SENT_KEY,
    SummaryDeps,
    is_due,
    render_summary,
    run_summary_loop,
    tick,
)

SITE = "summary-site"
OTHER = "other-site"

# Frozen noon on 2026-08-26 UTC — the default hour_utc=12 due instant.
NOON = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)
NOON_TS = NOON.timestamp()
HOUR = 3600.0
DAY = 86400.0

# Distinctive radio-sourced values that must never appear in the digest.
SECRET_ADDRESS = "AA:BB:CC:DD:EE:FF"
SECRET_FINGERPRINT = "fp-SECRET-should-not-leak"


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


@pytest.fixture
def presence(db: aiosqlite.Connection) -> PresenceEventRepository:
    return PresenceEventRepository(db, SITE)


@pytest.fixture
def outbox(db: aiosqlite.Connection) -> OutboxRepository:
    return OutboxRepository(db, SITE)


@pytest.fixture
def state(db: aiosqlite.Connection) -> SiteStateRepository:
    return SiteStateRepository(db, SITE)


def _deps(
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
    state: SiteStateRepository,
    *,
    now: float = NOON_TS,
    hour_utc: int = 12,
    enabled: bool = True,
) -> SummaryDeps:
    return SummaryDeps(
        devices=devices,
        observations=observations,
        presence_events=presence,
        outbox=outbox,
        state=state,
        now=lambda: now,
        hour_utc=hour_utc,
        enabled=enabled,
    )


async def _backdate_created(
    db: aiosqlite.Connection, device_id: int, ts: str
) -> None:
    await db.execute(
        "UPDATE devices SET created_at = ?, updated_at = ? WHERE id = ?",
        (ts, ts, device_id),
    )
    await db.commit()


async def _seed_snapshot_fixture(
    db: aiosqlite.Connection,
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
) -> None:
    """Two devices, mixed window membership, outbox health rows."""
    kitchen = await devices.upsert(
        fingerprint=SECRET_FINGERPRINT, address=SECRET_ADDRESS
    )
    await devices.set_label(kitchen, label="kitchen", actor="test")
    await _backdate_created(db, kitchen, iso_utc(NOON_TS - 2 * DAY))
    unlabeled = await devices.upsert(
        fingerprint="fp-new", address="11:22:33:44:55:66"
    )
    await _backdate_created(db, unlabeled, iso_utc(NOON_TS - HOUR))

    # In-window observations for both; one out-of-window ghost.
    await observations.append(
        device_id=kitchen,
        rssi=-60,
        observed_at=iso_utc(NOON_TS - HOUR),
    )
    await observations.append(
        device_id=unlabeled,
        rssi=-70,
        observed_at=iso_utc(NOON_TS - 2 * HOUR),
    )
    await observations.append(
        device_id=kitchen,
        rssi=-90,
        observed_at=iso_utc(NOON_TS - 2 * DAY),
    )
    await presence.append(
        device_id=kitchen,
        event_type="PRESENT",
        occurred_at=iso_utc(NOON_TS - HOUR),
    )
    await presence.append(
        device_id=kitchen,
        event_type="ABSENT",
        occurred_at=iso_utc(NOON_TS - 30 * 60),
    )
    pending = await outbox.enqueue(payload="still-pending")
    failed = await outbox.enqueue(payload="gave-up")
    await outbox.mark_failed(failed, "forbidden")
    assert pending >= 1


SNAPSHOT = """\
daily summary 2026-08-26 12:00Z
window: 2026-08-25T12:00:00.000Z .. 2026-08-26T12:00:00.000Z
devices seen: 2
  #1 kitchen
  #2 (unlabeled)
new devices: 1
  #2 (unlabeled)
presence: 1 PRESENT, 1 ABSENT
  #1 kitchen PRESENT
  #1 kitchen ABSENT
outbox: 1 pending, 1 failed"""


# ---------------------------------------------------------------------------
# is_due (pure schedule)
# ---------------------------------------------------------------------------


def test_is_due_at_configured_hour_with_no_marker() -> None:
    assert is_due(NOON_TS, hour_utc=12, last_sent=None) is True


def test_is_due_before_configured_hour() -> None:
    morning = NOON_TS - 4 * HOUR  # 08:00
    assert is_due(morning, hour_utc=12, last_sent=None) is False


def test_is_due_after_hour_same_utc_day_as_marker() -> None:
    last = iso_utc(NOON_TS)
    later = NOON_TS + 3 * HOUR  # 15:00 same day
    assert is_due(later, hour_utc=12, last_sent=last) is False


def test_is_due_future_marker_is_not_due() -> None:
    """Clock rollback / fast RTC: a future last_sent must not re-fire."""
    future = iso_utc(NOON_TS + DAY)
    assert is_due(NOON_TS, hour_utc=12, last_sent=future) is False


def test_is_due_next_utc_day_at_hour() -> None:
    last = iso_utc(NOON_TS)
    next_noon = NOON_TS + DAY
    assert is_due(next_noon, hour_utc=12, last_sent=last) is True


def test_is_due_next_utc_day_before_hour() -> None:
    last = iso_utc(NOON_TS)
    next_morning = NOON_TS + DAY - 4 * HOUR
    assert is_due(next_morning, hour_utc=12, last_sent=last) is False


# ---------------------------------------------------------------------------
# Snapshot-tested content (DoD)
# ---------------------------------------------------------------------------


async def test_summary_content_snapshot(
    db: aiosqlite.Connection,
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
    state: SiteStateRepository,
) -> None:
    await _seed_snapshot_fixture(db, devices, observations, presence, outbox)
    sent = await tick(_deps(devices, observations, presence, outbox, state))
    assert sent is True
    pending = await outbox.list_pending()
    texts = [
        OutboundMessage.model_validate_json(row["payload"]).text
        for row in pending
        if row["payload"] != "still-pending"
    ]
    assert texts == [SNAPSHOT]
    assert SECRET_ADDRESS not in texts[0]
    assert SECRET_FINGERPRINT not in texts[0]
    assert "11:22:33:44:55:66" not in texts[0]


async def test_render_summary_matches_snapshot_helper(
    db: aiosqlite.Connection,
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
) -> None:
    await _seed_snapshot_fixture(db, devices, observations, presence, outbox)
    start = iso_utc(NOON_TS - DAY)
    end = iso_utc(NOON_TS)
    text = await render_summary(
        devices,
        presence,
        outbox,
        window_start=start,
        window_end=end,
        sent_at=NOON_TS,
    )
    assert text == SNAPSHOT


async def test_label_newlines_collapsed_and_not_forged_rows(
    db: aiosqlite.Connection,
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
    state: SiteStateRepository,
) -> None:
    device_id = await devices.upsert(fingerprint="fp-nl", address="x")
    await devices.set_label(device_id, label="living\nroom", actor="test")
    await _backdate_created(db, device_id, iso_utc(NOON_TS - 2 * DAY))
    await observations.append(
        device_id=device_id,
        rssi=-50,
        observed_at=iso_utc(NOON_TS - HOUR),
    )
    await tick(_deps(devices, observations, presence, outbox, state))
    text = OutboundMessage.model_validate_json(
        (await outbox.list_pending())[0]["payload"]
    ).text
    assert "living room" in text
    assert "living\nroom" not in text


async def test_long_label_is_truncated(
    db: aiosqlite.Connection,
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
    state: SiteStateRepository,
) -> None:
    device_id = await devices.upsert(fingerprint="fp-long", address="x")
    await devices.set_label(device_id, label="L" * 80, actor="test")
    await _backdate_created(db, device_id, iso_utc(NOON_TS - 2 * DAY))
    await observations.append(
        device_id=device_id,
        rssi=-50,
        observed_at=iso_utc(NOON_TS - HOUR),
    )
    await tick(_deps(devices, observations, presence, outbox, state))
    text = OutboundMessage.model_validate_json(
        (await outbox.list_pending())[0]["payload"]
    ).text
    assert "L" * 40 in text
    assert "L" * 41 not in text


async def test_seen_list_is_capped(
    db: aiosqlite.Connection,
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
    state: SiteStateRepository,
) -> None:
    for i in range(21):
        device_id = await devices.upsert(
            fingerprint=f"fp-{i}", address=f"addr-{i}"
        )
        await _backdate_created(db, device_id, iso_utc(NOON_TS - 2 * DAY))
        await observations.append(
            device_id=device_id,
            rssi=-60,
            observed_at=iso_utc(NOON_TS - HOUR),
        )
    await tick(_deps(devices, observations, presence, outbox, state))
    text = OutboundMessage.model_validate_json(
        (await outbox.list_pending())[0]["payload"]
    ).text
    assert "devices seen: 21" in text
    assert "…and 1 more" in text
    assert "#21" not in text.split("new devices:")[0]


async def test_presence_roster_is_capped(
    db: aiosqlite.Connection,
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
    state: SiteStateRepository,
) -> None:
    device_id = await devices.upsert(fingerprint="fp-p", address="x")
    await _backdate_created(db, device_id, iso_utc(NOON_TS - 2 * DAY))
    for i in range(21):
        await presence.append(
            device_id=device_id,
            event_type="PRESENT",
            occurred_at=iso_utc(NOON_TS - HOUR + i),
        )
    await tick(_deps(devices, observations, presence, outbox, state))
    text = OutboundMessage.model_validate_json(
        (await outbox.list_pending())[0]["payload"]
    ).text
    roster, _, _ = text.partition("outbox:")
    presence_block = roster.split("presence:", 1)[1]
    assert "21 PRESENT" in presence_block
    assert "…and 1 more" in presence_block
    assert presence_block.count("PRESENT") == 21  # header + 20 roster rows


# ---------------------------------------------------------------------------
# Schedule + restart (DoD)
# ---------------------------------------------------------------------------


async def test_tick_noops_before_hour(
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
    state: SiteStateRepository,
) -> None:
    sent = await tick(
        _deps(
            devices,
            observations,
            presence,
            outbox,
            state,
            now=NOON_TS - 4 * HOUR,
        )
    )
    assert sent is False
    assert await outbox.count_pending() == 0
    assert await state.get(LAST_SENT_KEY) is None


async def test_tick_noops_when_disabled(
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
    state: SiteStateRepository,
) -> None:
    sent = await tick(
        _deps(
            devices,
            observations,
            presence,
            outbox,
            state,
            enabled=False,
        )
    )
    assert sent is False
    assert await outbox.count_pending() == 0


async def test_tick_records_marker_and_skips_same_day(
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
    state: SiteStateRepository,
) -> None:
    first = await tick(_deps(devices, observations, presence, outbox, state))
    assert first is True
    marker = await state.get(LAST_SENT_KEY)
    assert marker == iso_utc(NOON_TS)
    second = await tick(
        _deps(
            devices,
            observations,
            presence,
            outbox,
            state,
            now=NOON_TS + 3 * HOUR,
        )
    )
    assert second is False
    assert await outbox.count_pending() == 1


async def test_schedule_survives_restart_no_double_send(
    db: aiosqlite.Connection,
) -> None:
    """DoD: persisted marker on one connection is visible to a later tick."""
    conn1 = db
    devices = DeviceRepository(conn1, SITE)
    outbox = OutboxRepository(conn1, SITE)
    state = SiteStateRepository(conn1, SITE)
    sent = await tick(
        _deps(
            devices,
            ObservationRepository(conn1, SITE),
            PresenceEventRepository(conn1, SITE),
            outbox,
            state,
        )
    )
    assert sent is True

    # Same connection, fresh repository objects: the marker is what
    # survives, not process memory. tmpfile reconnect is tested below.
    devices2 = DeviceRepository(conn1, SITE)
    outbox2 = OutboxRepository(conn1, SITE)
    state2 = SiteStateRepository(conn1, SITE)
    again = await tick(
        _deps(
            devices2,
            ObservationRepository(conn1, SITE),
            PresenceEventRepository(conn1, SITE),
            outbox2,
            state2,
            now=NOON_TS + HOUR,
        )
    )
    assert again is False
    assert await outbox2.count_pending() == 1


async def test_schedule_survives_tmpfile_reconnect(tmp_path) -> None:
    path = tmp_path / "summary.db"
    conn = await connect(path)
    await apply_migrations(conn)
    sent = await tick(
        _deps(
            DeviceRepository(conn, SITE),
            ObservationRepository(conn, SITE),
            PresenceEventRepository(conn, SITE),
            OutboxRepository(conn, SITE),
            SiteStateRepository(conn, SITE),
        )
    )
    assert sent is True
    await conn.close()

    conn = await connect(path)
    outbox = OutboxRepository(conn, SITE)
    again = await tick(
        _deps(
            DeviceRepository(conn, SITE),
            ObservationRepository(conn, SITE),
            PresenceEventRepository(conn, SITE),
            outbox,
            SiteStateRepository(conn, SITE),
            now=NOON_TS + HOUR,
        )
    )
    assert again is False
    assert await outbox.count_pending() == 1
    await conn.close()


async def test_next_utc_day_sends_again(
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
    state: SiteStateRepository,
) -> None:
    await tick(_deps(devices, observations, presence, outbox, state))
    sent = await tick(
        _deps(
            devices,
            observations,
            presence,
            outbox,
            state,
            now=NOON_TS + DAY,
        )
    )
    assert sent is True
    assert await outbox.count_pending() == 2


async def test_catchup_window_starts_at_last_sent(
    db: aiosqlite.Connection,
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
    state: SiteStateRepository,
) -> None:
    await tick(_deps(devices, observations, presence, outbox, state))
    device_id = await devices.upsert(fingerprint="fp-gap", address="z")
    await _backdate_created(db, device_id, iso_utc(NOON_TS + HOUR))
    await observations.append(
        device_id=device_id,
        rssi=-55,
        observed_at=iso_utc(NOON_TS + 2 * HOUR),
    )
    # Three days later: one catch-up covering (last_sent, now].
    later = NOON_TS + 3 * DAY
    await tick(
        _deps(
            devices,
            observations,
            presence,
            outbox,
            state,
            now=later,
        )
    )
    texts = [
        OutboundMessage.model_validate_json(row["payload"]).text
        for row in await outbox.list_pending()
    ]
    catchup = texts[-1]
    assert iso_utc(NOON_TS) in catchup
    assert iso_utc(later) in catchup
    assert (
        "#1 (unlabeled)" in catchup or f"#{device_id} (unlabeled)" in catchup
    )


async def test_enqueue_and_marker_roll_back_together(
    db: aiosqlite.Connection,
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
    state: SiteStateRepository,
) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with db_transaction(db):
            await tick(_deps(devices, observations, presence, outbox, state))
            raise RuntimeError("boom")
    assert await outbox.count_pending() == 0
    assert await state.get(LAST_SENT_KEY) is None


async def test_other_site_observations_are_excluded(
    db: aiosqlite.Connection,
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
    state: SiteStateRepository,
) -> None:
    other_devices = DeviceRepository(db, OTHER)
    other_obs = ObservationRepository(db, OTHER)
    foreign = await other_devices.upsert(fingerprint="fp-x", address="yy")
    await other_obs.append(
        device_id=foreign,
        rssi=-40,
        observed_at=iso_utc(NOON_TS - HOUR),
    )
    await tick(_deps(devices, observations, presence, outbox, state))
    text = OutboundMessage.model_validate_json(
        (await outbox.list_pending())[0]["payload"]
    ).text
    assert "devices seen: 0" in text
    assert f"#{foreign}" not in text


async def test_run_summary_loop_sends_once_per_day(
    devices: DeviceRepository,
    observations: ObservationRepository,
    presence: PresenceEventRepository,
    outbox: OutboxRepository,
    state: SiteStateRepository,
) -> None:
    ticks = await run_summary_loop(
        _deps(devices, observations, presence, outbox, state),
        poll=0.0,
        max_ticks=3,
    )
    assert ticks == 3
    assert await outbox.count_pending() == 1
