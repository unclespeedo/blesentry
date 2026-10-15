# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for approach alert name enrichment (#186)."""

from __future__ import annotations

from pathlib import Path

import pytest

from blesentry.detection.approach import APPROACH_DETECTOR_ID, APPROACH_KIND
from blesentry.detection.approach_enrich import enrich_approach_event
from blesentry.detection.models import DetectionEvent
from blesentry.scanner.models import Advertisement
from blesentry.storage.database import apply_migrations, connect
from blesentry.storage.repository import DeviceRepository

ADDR = "AA:BB:00:00:00:01"


def _event() -> DetectionEvent:
    return DetectionEvent(
        detector=APPROACH_DETECTOR_ID,
        kind=APPROACH_KIND,
        window_index=7,
        rssi=-72,
        band="far",
        rising=True,
        approach_identity=ADDR,
    )


def _ad(*, local_name: str | None) -> Advertisement:
    return Advertisement(
        address=ADDR,
        rssi=-72,
        timestamp=1.0,
        adapter_id="test",
        local_name=local_name,
    )


@pytest.mark.asyncio
async def test_enrich_picks_first_usable_local_name(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "e.db")
    try:
        await apply_migrations(conn)
        devices = DeviceRepository(conn, "site")
        enriched = await enrich_approach_event(
            _event(),
            advertisements=(
                _ad(local_name="   "),
                _ad(local_name="Guest BLE"),
            ),
            address_to_device_id={ADDR: 1},
            devices=devices,
        )
        assert enriched.device_name == "Guest BLE"
        assert enriched.approach_identity is None
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_enrich_label_controls_fall_back_to_local_name(
    tmp_path: Path,
) -> None:
    conn = await connect(tmp_path / "e.db")
    try:
        await apply_migrations(conn)
        devices = DeviceRepository(conn, "site")
        device_id = await devices.upsert(
            fingerprint="fp-test",
            address=ADDR,
            label="\x00\x00",
        )
        enriched = await enrich_approach_event(
            _event(),
            advertisements=(_ad(local_name="Guest BLE"),),
            address_to_device_id={ADDR: device_id},
            devices=devices,
        )
        assert enriched.device_name == "Guest BLE"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_enrich_non_approach_event_unchanged(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "e.db")
    try:
        await apply_migrations(conn)
        devices = DeviceRepository(conn, "site")
        event = DetectionEvent(
            detector="mock",
            kind="test",
            window_index=0,
        )
        same = await enrich_approach_event(
            event,
            advertisements=(),
            address_to_device_id={},
            devices=devices,
        )
        assert same is event
    finally:
        await conn.close()
