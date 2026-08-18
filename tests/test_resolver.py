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

from blesentry.resolver import DeviceResolver, fusion_score, hap_device_id
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
            local_name="Sensor",
            service_uuids=["180d"],
            manufacturer_data={"76": "aabbcc"},
        )
    )
    b = Fingerprint.from_advertisement(
        _ad(
            address="22:22:22:22:22:22",
            local_name="Sensor",
            service_uuids=["180d"],
            manufacturer_data={"76": "aabbcc"},
        )
    )
    assert fusion_score(a, b) >= 0.55


def test_company_id_alone_scores_below_threshold() -> None:
    """Company id alone must not fuse.

    Same vendor, different payload — the rotation cloud must never
    collapse into one device per vendor.
    """
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
        local_name="Sensor",
        service_uuids=["180d"],
        manufacturer_data={"76": "aabbcc"},
    )
    after = _ad(
        address="43:22:22:22:22:22",
        address_type="rpa",
        local_name="Sensor",
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
    (id_a,) = await _resolve_cycle(resolver, _ad(address="5E:11:11:11:11:11"))
    (id_b,) = await _resolve_cycle(resolver, _ad(address="43:22:22:22:22:22"))
    assert id_a != id_b


@pytest.mark.asyncio
async def test_stable_address_changed_payload_fuses(devices) -> None:
    """Observed live: same static address, payload changed — one
    device whose advertisement state ticked, not two devices.
    """
    resolver = DeviceResolver(devices)
    (id_a,) = await _resolve_cycle(
        resolver,
        _ad(
            address="F9:50:53:07:09:79",
            address_type="random_static",
            local_name="Sensor",
            manufacturer_data={"76": "aabbcc"},
        ),
    )
    (id_b,) = await _resolve_cycle(
        resolver,
        _ad(
            address="F9:50:53:07:09:79",
            address_type="random_static",
            local_name="Sensor",
            manufacturer_data={"76": "ddeeff"},
        ),
    )
    assert id_a == id_b
    assert len(await devices.list_devices()) == 1


# (former test_rpa_address_match_alone_does_not_fuse inverted by the
# 2026-08-18 fusion-policy decision: an exact address match within the
# window is near-certain same-device — see
# test_same_address_in_window_fuses_despite_payload_change)


@pytest.mark.asyncio
async def test_threshold_is_configurable(devices) -> None:
    strict = DeviceResolver(devices, min_score=0.99)
    before = _ad(
        address="5E:11:11:11:11:11",
        local_name="Sensor",
        manufacturer_data={"76": "aabbcc"},
    )
    after = _ad(
        address="43:22:22:22:22:22",
        local_name="Sensor",
        manufacturer_data={"76": "aabbcc"},
    )
    # payload+name = 0.75: fuses at the default threshold, not at 0.99
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


# ---------------------------------------------------------------------------
# Ground truth (maintainer-verified): two factory-identical
# accessories exist at the test site. Identical twin products must
# never fuse across distinct stable addresses — the veto.
# ---------------------------------------------------------------------------


def test_stable_address_mismatch_vetoes_fusion_score() -> None:
    a = Fingerprint.from_advertisement(
        _ad(
            address="F9:11:11:11:11:11",
            address_type="random_static",
            local_name="Sensor",
            service_uuids=["180d"],
            manufacturer_data={"76": "aabbcc"},
        )
    )
    b = Fingerprint.from_advertisement(
        _ad(
            address="E9:22:22:22:22:22",
            address_type="random_static",
            local_name="Sensor",
            service_uuids=["180d"],
            manufacturer_data={"76": "aabbcc"},
        )
    )
    assert (
        fusion_score(
            a,
            b,
            address_type="random_static",
            other_address_type="random_static",
        )
        == 0.0
    )


@pytest.mark.asyncio
async def test_identical_twin_devices_stay_distinct(devices) -> None:
    """Two factory-identical sensors on different static addresses."""
    resolver = DeviceResolver(devices)
    eve_one = _ad(
        address="F9:11:11:11:11:11",
        address_type="random_static",
        local_name="Sensor",
        service_uuids=["180d"],
        manufacturer_data={"76": "aabbcc"},
    )
    eve_two = _ad(
        address="E9:22:22:22:22:22",
        address_type="random_static",
        local_name="Sensor",
        service_uuids=["180d"],
        manufacturer_data={"76": "aabbcc"},
    )
    (id_one,) = await _resolve_cycle(resolver, eve_one)
    (id_two,) = await _resolve_cycle(resolver, eve_two)
    assert id_one != id_two
    assert len(await devices.list_devices()) == 2


@pytest.mark.asyncio
async def test_rpa_rotation_still_fuses_after_veto(devices) -> None:
    """The veto must not break the rotation join it exists beside."""
    resolver = DeviceResolver(devices)
    (id_a,) = await _resolve_cycle(
        resolver,
        _ad(
            address="5E:11:11:11:11:11",
            address_type="rpa",
            local_name="Tag",
            manufacturer_data={"76": "aabbcc"},
        ),
    )
    (id_b,) = await _resolve_cycle(
        resolver,
        _ad(
            address="43:22:22:22:22:22",
            address_type="rpa",
            local_name="Tag",
            manufacturer_data={"76": "aabbcc"},
        ),
    )
    assert id_a == id_b


# ---------------------------------------------------------------------------
# Panel fixes: durable exact-key recovery, fused-address currency,
# scoring edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_key_recovers_across_restart(devices) -> None:
    """A fresh resolver (restart) must find the durable row, not fork."""
    first = DeviceResolver(devices)
    ad = _ad(local_name="Sensor")
    (id_a,) = await _resolve_cycle(first, ad)
    restarted = DeviceResolver(devices)
    (id_b,) = await _resolve_cycle(restarted, ad)
    assert id_a == id_b
    assert len(await devices.list_devices()) == 1


@pytest.mark.asyncio
async def test_fused_rotation_updates_stored_address(devices) -> None:
    resolver = DeviceResolver(devices)
    (device_id,) = await _resolve_cycle(
        resolver,
        _ad(
            address="5E:11:11:11:11:11",
            address_type="rpa",
            local_name="Tag",
            manufacturer_data={"76": "aabbcc"},
        ),
    )
    await _resolve_cycle(
        resolver,
        _ad(
            address="43:22:22:22:22:22",
            address_type="rpa",
            local_name="Tag",
            manufacturer_data={"76": "aabbcc"},
        ),
    )
    row = await devices.get(device_id)
    assert row is not None and row["address"] == "43:22:22:22:22:22"


def test_empty_local_name_awards_nothing() -> None:
    a = Fingerprint.from_advertisement(
        _ad(
            address="11:11:11:11:11:11",
            local_name="",
            manufacturer_data={"76": "aabbcc"},
        )
    )
    b = Fingerprint.from_advertisement(
        _ad(
            address="22:22:22:22:22:22",
            local_name="",
            manufacturer_data={"76": "aabbcc"},
        )
    )
    assert fusion_score(a, b) < 0.55


def test_equal_address_uses_either_sides_provenance() -> None:
    a = Fingerprint.from_advertisement(
        _ad(address="F0:11:11:11:11:11", address_type=None)
    )
    b = Fingerprint.from_advertisement(
        _ad(address="F0:11:11:11:11:11", address_type="random_static")
    )
    assert (
        fusion_score(
            a, b, address_type=None, other_address_type="random_static"
        )
        == 0.6
    )


# ---------------------------------------------------------------------------
# Strengthened fusion (maintainer decision 2026-08-18): HAP stable IDs,
# same-address-within-window, startup seeding
# ---------------------------------------------------------------------------


def test_hap_device_id_extracted() -> None:
    # HAP adv: type 06, STL 31 (subtype 1, len 17), AIL, then 6-byte id
    fp = Fingerprint.from_advertisement(
        _ad(manufacturer_data={"76": "0631001e2a3b4c5d6e0a01020304"})
    )
    assert hap_device_id(fp) == "1e2a3b4c5d6e"


def test_hap_id_absent_for_non_hap_payloads() -> None:
    fp = Fingerprint.from_advertisement(
        _ad(manufacturer_data={"76": "1005011234aa"})
    )
    assert hap_device_id(fp) is None


def test_matching_hap_ids_fuse_across_rotation() -> None:
    a = Fingerprint.from_advertisement(
        _ad(
            address="5E:11:11:11:11:11",
            manufacturer_data={"76": "0631001e2a3b4c5d6e0a01020304"},
        )
    )
    b = Fingerprint.from_advertisement(
        _ad(
            address="43:22:22:22:22:22",
            manufacturer_data={"76": "0631071e2a3b4c5d6e0b02030405"},
        )
    )
    # state bytes differ, device id identical -> authoritative match
    assert fusion_score(a, b) >= 1.0


def test_differing_hap_ids_veto_fusion() -> None:
    """Differing HAP ids veto.

    Twin accessories: same name, different HAP ids — never fuse, even
    without address provenance (closes the macOS-capture gap).
    """
    a = Fingerprint.from_advertisement(
        _ad(
            address="11:11:11:11:11:11",
            local_name="Sensor",
            manufacturer_data={"76": "0631001e2a3b4c5d6e0a01020304"},
        )
    )
    b = Fingerprint.from_advertisement(
        _ad(
            address="22:22:22:22:22:22",
            local_name="Sensor",
            manufacturer_data={"76": "063100ffeeddccbbaa0a01020304"},
        )
    )
    assert fusion_score(a, b) == 0.0


@pytest.mark.asyncio
async def test_same_address_in_window_fuses_despite_payload_change(
    devices,
) -> None:
    """Same address within the window fuses.

    One iPhone flipping its Nearby status byte within an RPA lifetime
    must not fork identities (panel major, policy-approved).
    """
    resolver = DeviceResolver(devices)
    (id_a,) = await _resolve_cycle(
        resolver,
        _ad(address="5E:11:11:11:11:11", manufacturer_data={"76": "01"}),
    )
    (id_b,) = await _resolve_cycle(
        resolver,
        _ad(address="5E:11:11:11:11:11", manufacturer_data={"76": "02"}),
    )
    assert id_a == id_b


@pytest.mark.asyncio
async def test_seed_restores_fusion_memory_across_restart(devices) -> None:
    first = DeviceResolver(devices)
    (id_a,) = await _resolve_cycle(
        first,
        _ad(
            address="5E:11:11:11:11:11",
            address_type="rpa",
            local_name="Tag",
            manufacturer_data={"76": "aabbcc"},
        ),
    )
    restarted = DeviceResolver(devices)
    await restarted.seed()
    (id_b,) = await _resolve_cycle(
        restarted,
        _ad(
            address="43:22:22:22:22:22",
            address_type="rpa",
            local_name="Tag",
            manufacturer_data={"76": "aabbcc"},
        ),
    )
    assert id_a == id_b
