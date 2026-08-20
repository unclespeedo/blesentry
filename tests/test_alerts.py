# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""UnknownDeviceAlerter tests (P2-6, #27).

An unlabeled device going PRESENT enqueues one interactive alert; a
labeled device stays silent. (The full end-to-end alert → reply → no
re-alert flow is in test_label_flow.py.)
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiosqlite
import pytest

from blesentry.alerts import UnknownDeviceAlerter
from blesentry.notifier.models import OutboundMessage
from blesentry.presence import PresenceState, PresenceTransition
from blesentry.storage import (
    DeviceRepository,
    OutboxRepository,
    apply_migrations,
    connect,
)

SITE = "test-site"
PRESENT = PresenceState.PRESENT
ABSENT = PresenceState.ABSENT


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


async def _alert_texts(outbox: OutboxRepository) -> list[str]:
    return [
        OutboundMessage.model_validate_json(row["payload"]).text
        for row in await outbox.list_pending()
    ]


async def test_alerts_unlabeled_present_device(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    device_id = await devices.upsert(
        fingerprint="fp", address="AA:BB:CC:DD:EE:FF"
    )
    alerter = UnknownDeviceAlerter(devices, outbox)
    count = await alerter.handle([PresenceTransition(device_id, PRESENT)])
    assert count == 1
    texts = await _alert_texts(outbox)
    assert len(texts) == 1
    assert f"/label {device_id}" in texts[0]
    assert f"/ignore {device_id}" in texts[0]
    assert "AA:BB:CC:DD:EE:FF" in texts[0]


async def test_skips_labeled_device(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    device_id = await devices.upsert(fingerprint="fp")
    await devices.set_label(device_id, label="Phone", actor="op")
    alerter = UnknownDeviceAlerter(devices, outbox)
    assert await alerter.handle([PresenceTransition(device_id, PRESENT)]) == 0
    assert await outbox.count_pending() == 0


async def test_skips_absent_transitions(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    device_id = await devices.upsert(fingerprint="fp")
    alerter = UnknownDeviceAlerter(devices, outbox)
    assert await alerter.handle([PresenceTransition(device_id, ABSENT)]) == 0
    assert await outbox.count_pending() == 0


async def test_skips_vanished_device(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    alerter = UnknownDeviceAlerter(devices, outbox)
    assert await alerter.handle([PresenceTransition(999, PRESENT)]) == 0
    assert await outbox.count_pending() == 0


async def test_alerts_only_the_unlabeled_of_a_batch(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    known = await devices.upsert(fingerprint="fp-known")
    await devices.set_label(known, label="Router", actor="op")
    unknown = await devices.upsert(fingerprint="fp-unknown")
    alerter = UnknownDeviceAlerter(devices, outbox)
    count = await alerter.handle(
        [
            PresenceTransition(known, PRESENT),
            PresenceTransition(unknown, PRESENT),
        ]
    )
    assert count == 1
    texts = await _alert_texts(outbox)
    assert len(texts) == 1
    assert f"/label {unknown}" in texts[0]


async def test_alert_sanitizes_crafted_address(
    devices: DeviceRepository, outbox: OutboxRepository
) -> None:
    # A radio-sourced address with a newline must not forge a second,
    # tappable command line in the operator's alert.
    device_id = await devices.upsert(
        fingerprint="fp", address="x]\n/ignore 9 ["
    )
    alerter = UnknownDeviceAlerter(devices, outbox)
    await alerter.handle([PresenceTransition(device_id, PRESENT)])
    text = (await _alert_texts(outbox))[0]
    assert "\n/ignore 9" not in text  # no forged line
    assert f"/label {device_id}" in text  # legit lines intact


async def test_run_cycle_alerter_requires_presence(
    db: aiosqlite.Connection,
    devices: DeviceRepository,
    outbox: OutboxRepository,
) -> None:
    from blesentry.loop import run_cycle
    from blesentry.scanner.mock import MockScanner
    from blesentry.storage import ObservationRepository

    with pytest.raises(ValueError, match="alerter requires presence"):
        await run_cycle(
            MockScanner(scenarios=[[]]),
            devices,
            ObservationRepository(db, SITE),
            0.0,
            alerter=UnknownDeviceAlerter(devices, outbox),
        )
