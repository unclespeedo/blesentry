# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""End-to-end interactive label flow (P2-6, #27) — the DoD.

Drives the real pieces (scan cycle → presence → alerter → outbox →
drain → notifier, and the command loop for the reply) with MockScanner +
MockNotifier: an unknown device is alerted exactly once, labeled via a
reply, and never re-alerts once labeled.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiosqlite
import pytest

from blesentry.alerts import UnknownDeviceAlerter
from blesentry.commands import run_command_loop
from blesentry.drain import DrainResult, drain_once
from blesentry.loop import run_loop
from blesentry.notifier.mock import MockNotifier
from blesentry.notifier.models import InboundCommand
from blesentry.presence import PresenceTracker
from blesentry.scanner.mock import MockScanner
from blesentry.scanner.models import Advertisement
from blesentry.storage import (
    DeviceRepository,
    ObservationRepository,
    OutboxRepository,
    PresenceEventRepository,
    apply_migrations,
    connect,
)

SITE = "flow"
ADDR = "C0:FF:EE:00:00:01"


class _WindowClock:
    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        self.t += 15.0
        return self.t


def _ad(rssi: int) -> Advertisement:
    return Advertisement(
        address=ADDR, rssi=rssi, timestamp=1.0, adapter_id="test"
    )


@pytest.fixture
async def conn() -> AsyncIterator[aiosqlite.Connection]:
    c = await connect(":memory:")
    try:
        await apply_migrations(c)
        yield c
    finally:
        await c.close()


async def test_unknown_device_alerted_once_labeled_no_realert(
    conn: aiosqlite.Connection,
) -> None:
    devices = DeviceRepository(conn, SITE)
    observations = ObservationRepository(conn, SITE)
    presence_events = PresenceEventRepository(conn, SITE)
    outbox = OutboxRepository(conn, SITE)
    tracker = PresenceTracker(appear_windows=2, disappear_windows=2)
    alerter = UnknownDeviceAlerter(devices, outbox)
    notifier = MockNotifier()
    clock = _WindowClock()

    async def _scan(scenarios: list[list[Advertisement]]) -> None:
        await run_loop(
            MockScanner(scenarios=scenarios),
            devices,
            observations,
            duration=0.0,
            pause=0.0,
            max_cycles=len(scenarios),
            presence=tracker,
            presence_events=presence_events,
            alerter=alerter,
            now=clock,
        )

    async def _drain_all() -> None:
        while await drain_once(outbox, notifier) is not DrainResult.IDLE:
            pass

    def _unknown_alerts() -> list[str]:
        return [m.text for m in notifier.sent if "Unknown device" in m.text]

    # 1. Unknown device appears (2 windows → PRESENT) → one alert.
    await _scan([[_ad(-50)], [_ad(-50)]])
    device_id = (await devices.list_devices())[0]["id"]
    await _drain_all()
    assert len(_unknown_alerts()) == 1
    assert f"/label {device_id}" in _unknown_alerts()[0]

    # 2. Operator replies to name it (referencing the id from the alert).
    reply = MockNotifier(
        inbound=[
            InboundCommand(
                chat_id=1,
                user_id=2,
                message_id=1,
                text=f"/label {device_id} Front Gate",
            )
        ]
    )
    await run_command_loop(
        reply,
        devices,
        outbox,
        db_path="x",
        started_at=0.0,
        clock=lambda: 0.0,
        max_commands=1,
    )
    row = await devices.get(device_id)
    assert row is not None and row["label"] == "Front Gate"
    await _drain_all()  # deliver the confirmation reply

    # 3. It leaves and returns (a new visit) — now labeled → NO re-alert.
    await _scan([[], [], [_ad(-50)], [_ad(-50)]])
    await _drain_all()
    assert len(_unknown_alerts()) == 1  # still exactly one


async def test_ignore_also_stops_alerts(
    conn: aiosqlite.Connection,
) -> None:
    devices = DeviceRepository(conn, SITE)
    observations = ObservationRepository(conn, SITE)
    presence_events = PresenceEventRepository(conn, SITE)
    outbox = OutboxRepository(conn, SITE)
    tracker = PresenceTracker(appear_windows=1, disappear_windows=1)
    alerter = UnknownDeviceAlerter(devices, outbox)
    notifier = MockNotifier()
    clock = _WindowClock()

    async def _scan(scenarios: list[list[Advertisement]]) -> None:
        await run_loop(
            MockScanner(scenarios=scenarios),
            devices,
            observations,
            duration=0.0,
            pause=0.0,
            max_cycles=len(scenarios),
            presence=tracker,
            presence_events=presence_events,
            alerter=alerter,
            now=clock,
        )

    async def _drain_all() -> None:
        while await drain_once(outbox, notifier) is not DrainResult.IDLE:
            pass

    def _unknown_alerts() -> list[str]:
        return [m.text for m in notifier.sent if "Unknown device" in m.text]

    await _scan([[_ad(-50)]])  # appear=1 → PRESENT → alert
    device_id = (await devices.list_devices())[0]["id"]
    await _drain_all()
    assert len(_unknown_alerts()) == 1

    reply = MockNotifier(
        inbound=[
            InboundCommand(
                chat_id=1, user_id=2, message_id=1, text=f"/ignore {device_id}"
            )
        ]
    )
    await run_command_loop(
        reply,
        devices,
        outbox,
        db_path="x",
        started_at=0.0,
        clock=lambda: 0.0,
        max_commands=1,
    )
    await _drain_all()

    await _scan([[], [_ad(-50)]])  # leaves, returns → no re-alert
    await _drain_all()
    assert len(_unknown_alerts()) == 1
