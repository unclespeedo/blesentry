# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Fingerprint fusion resolver tests (P1-7, #19).

Scenario coverage per the DoD: rotated MAC + stable manufacturer data
(the Apple pattern), identical non-address fingerprints on different
MACs, nulls everywhere, and the live-observed same-static-MAC/
changed-payload case. Real temp-file SQLite underneath.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blesentry.resolver import DeviceResolver, fusion_score
from blesentry.scanner import Advertisement, Fingerprint
from blesentry.storage.database import apply_migrations, connect, transaction
from blesentry.storage.repository import DeviceRepository


def _ad(
    address: str = "AA:BB:CC:DD:EE:FF",
    address_type: str | None = "rpa",
    local_name: str | None = None,
    service_uuids: list[str] | None = None,
    manufacturer_data: dict[str, str] | None = None,
) -> Advertisement:
    return Advertisement(
        address=address,
        address_type=address_type,
        rssi=-60,
        local_name=local_name,
        service_uuids=service_uuids or [],
        manufacturer_data=manufacturer_data or {},
        timestamp=1755400000.0,
        adapter_id="mock",
    )


@pytest.fixture
async def devices(tmp_path: Path):
    conn = await connect(tmp_path / "resolver.db")
    await apply_migrations(conn)
    yield DeviceRepository(conn, "test-site")
    await conn.close()


async def _resolve_cycle(
    resolver: DeviceResolver, *ads: Advertisement
) -> list[int]:
    """Resolve a batch inside one committed transaction (loop shape)."""
    ids = []
    async with transaction(resolver.connection):
        for ad in ads:
            ids.append(await resolver.resolve(ad))
    resolver.commit()
    return ids


# ---------------------------------------------------------------------------
# Scoring function (pure, explicit)
# ---------------------------------------------------------------------------


def test_identical_non_address_signals_score_high() -> None:
    a = Fingerprint.from_advertisement(
        _ad(
            address="11:11:11:11:11:11",
            local_name="Eve",
            service_uuids=["180d"],
            manufacturer_data={"76": "aabbcc"},
        )
    )
    b = Fingerprint.from_advertisement(
        _ad(
            address="22:22:22:22:22:22",
            local_name="Eve",
            service_uuids=["180d"],
            manufacturer_data={"76": "aabbcc"},
        )
    )
    assert fusion_score(a, b) >= 0.55


def test_company_id_alone_scores_below_threshold() -> None:
    """Same vendor, different payload — the rotation cloud must NOT
    collapse into one device on company id alone."""
    a = Fingerprint.from_advertisement(
        _ad(address="11:11:11:11:11:11", manufacturer_data={"76": "0102"})
    )
    b = Fingerprint.from_advertisement(
        _ad(address="22:22:22:22:22:22", manufacturer_data={"76": "0304"})
    )
    assert fusion_score(a, b) < 0.55


def test_nulls_everywhere_scores_zero() -> None:
    a = Fingerprint.from_advertisement(_ad(address="11:11:11:11:11:11"))
    b = Fingerprint.from_advertisement(_ad(address="22:22:22:22:22:22"))
    assert fusion_score(a, b) == 0.0


# ---------------------------------------------------------------------------
# Resolution scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_repeat_resolves_to_same_device(devices) -> None:
    resolver = DeviceResolver(devices)
    ad = _ad(local_name="Sensor")
    (first,) = await _resolve_cycle(resolver, ad)
    (second,) = await _resolve_cycle(resolver, ad)
    assert first == second
    assert len(await devices.list_devices()) == 1


@pytest.mark.asyncio
async def test_rotated_mac_stable_payload_fuses(devices) -> None:
    """The Apple pattern: RPA rotates, identity signals persist."""
    resolver = DeviceResolver(devices)
    before = _ad(
        address="5E:11:11:11:11:11",
        address_type="rpa",
        local_name="Eve",
        service_uuids=["180d"],
        manufacturer_data={"76": "aabbcc"},
    )
    after = _ad(
        address="43:22:22:22:22:22",
        address_type="rpa",
        local_name="Eve",
        service_uuids=["180d"],
        manufacturer_data={"76": "aabbcc"},
    )
    (id_a,) = await _resolve_cycle(resolver, before)
    (id_b,) = await _resolve_cycle(resolver, after)
    assert id_a == id_b
    assert len(await devices.list_devices()) == 1


@pytest.mark.asyncio
async def test_rotating_nulls_stay_distinct(devices) -> None:
    """Nulls everywhere: rotation with no signals must NOT fuse."""
    resolver = DeviceResolver(devices)
    (id_a,) = await _resolve_cycle(
        resolver, _ad(address="5E:11:11:11:11:11")
    )
    (id_b,) = await _resolve_cycle(
        resolver, _ad(address="43:22:22:22:22:22")
    )
    assert id_a != id_b


@pytest.mark.asyncio
async def test_stable_address_changed_payload_fuses(devices) -> None:
    """The live Eve case: same static address, payload changed."""
    resolver = DeviceResolver(devices)
    (id_a,) = await _resolve_cycle(
        resolver,
        _ad(
            address="F9:50:53:07:09:79",
            address_type="random_static",
            local_name="Eve",
            manufacturer_data={"76": "aabbcc"},
        ),
    )
    (id_b,) = await _resolve_cycle(
        resolver,
        _ad(
            address="F9:50:53:07:09:79",
            address_type="random_static",
            local_name="Eve",
            manufacturer_data={"76": "ddeeff"},
        ),
    )
    assert id_a == id_b
    assert len(await devices.list_devices()) == 1


@pytest.mark.asyncio
async def test_rpa_address_match_alone_does_not_fuse(devices) -> None:
    """An address match on rotating provenance is a weak signal."""
    resolver = DeviceResolver(devices)
    (id_a,) = await _resolve_cycle(
        resolver,
        _ad(address="5E:11:11:11:11:11", manufacturer_data={"76": "01"}),
    )
    (id_b,) = await _resolve_cycle(
        resolver,
        _ad(address="5E:11:11:11:11:11", manufacturer_data={"76": "02"}),
    )
    # same RPA re-observed before rotation: exact address+key differ,
    # weak address signal + company-only must stay below threshold
    assert id_a != id_b


@pytest.mark.asyncio
async def test_threshold_is_configurable(devices) -> None:
    strict = DeviceResolver(devices, min_score=0.99)
    before = _ad(
        address="5E:11:11:11:11:11",
        local_name="Eve",
        service_uuids=["180d"],
        manufacturer_data={"76": "aabbcc"},
    )
    after = _ad(
        address="43:22:22:22:22:22",
        local_name="Eve",
        service_uuids=["180d"],
        manufacturer_data={"76": "aabbcc"},
    )
    (id_a,) = await _resolve_cycle(strict, before)
    (id_b,) = await _resolve_cycle(strict, after)
    assert id_a != id_b


@pytest.mark.asyncio
async def test_abort_discards_staged_identities(devices) -> None:
    """Rollback safety (the #84 lesson, inherited by the resolver)."""
    resolver = DeviceResolver(devices)
    ad = _ad(local_name="Sensor")
    try:
        async with transaction(resolver.connection):
            await resolver.resolve(ad)
            raise RuntimeError("cycle fails")
    except RuntimeError:
        resolver.abort()
    assert await devices.list_devices() == []
    (device_id,) = await _resolve_cycle(resolver, ad)
    assert len(await devices.list_devices()) == 1
    assert device_id > 0
