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

from collections.abc import Sequence
from pathlib import Path

import pytest

import blesentry.resolver as resolver_mod
import blesentry.storage.repository as repo_mod
from blesentry.resolver import (
    DeviceResolver,
    fingerprint_key,
    fusion_score,
    hap_device_id,
)
from blesentry.scanner import Advertisement, Fingerprint
from blesentry.storage.database import apply_migrations, connect, transaction
from blesentry.storage.repository import DeviceAliasRow, DeviceRepository


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
    """Same stable address with changed payload fuses.

    Observed live: one device whose advertisement state ticked,
    not two devices.
    """
    resolver = DeviceResolver(devices)
    (id_a,) = await _resolve_cycle(
        resolver,
        _ad(
            address="F9:33:33:33:33:33",
            address_type="random_static",
            local_name="Sensor",
            manufacturer_data={"76": "aabbcc"},
        ),
    )
    (id_b,) = await _resolve_cycle(
        resolver,
        _ad(
            address="F9:33:33:33:33:33",
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
    twin_one = _ad(
        address="F9:11:11:11:11:11",
        address_type="random_static",
        local_name="Sensor",
        service_uuids=["180d"],
        manufacturer_data={"76": "aabbcc"},
    )
    twin_two = _ad(
        address="E9:22:22:22:22:22",
        address_type="random_static",
        local_name="Sensor",
        service_uuids=["180d"],
        manufacturer_data={"76": "aabbcc"},
    )
    (id_one,) = await _resolve_cycle(resolver, twin_one)
    (id_two,) = await _resolve_cycle(resolver, twin_two)
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


@pytest.mark.asyncio
async def test_seed_eviction_discards_oldest_not_newest(devices) -> None:
    """Window pressure after seeding must evict the OLDEST devices."""
    writer = DeviceResolver(devices)
    ads = [
        _ad(
            address=f"F9:33:33:33:33:{i:02X}",
            address_type="random_static",
            local_name=f"S{i}",
        )
        for i in range(3)
    ]
    for ad in ads:
        await _resolve_cycle(writer, ad)
    small = DeviceResolver(devices, recent_window=3)
    await small.seed()
    # one new resolution forces one eviction
    await _resolve_cycle(small, _ad(address="11:11:11:11:11:11"))
    remaining = {fp.local_name for fp, _, _ in small._recent.values()}
    assert "S2" in remaining, "newest seed must survive eviction"


@pytest.mark.asyncio
async def test_touch_skips_write_when_address_unchanged(devices) -> None:
    """Chatty same-address fusion must not churn updated_at (#84)."""
    resolver = DeviceResolver(devices)
    (device_id,) = await _resolve_cycle(
        resolver,
        _ad(address="5E:11:11:11:11:11", manufacturer_data={"76": "01"}),
    )
    row = await devices.get(device_id)
    assert row is not None
    before = row["updated_at"]
    await _resolve_cycle(
        resolver,
        _ad(address="5E:11:11:11:11:11", manufacturer_data={"76": "02"}),
    )
    row = await devices.get(device_id)
    assert row is not None and row["updated_at"] == before


@pytest.mark.asyncio
async def test_seed_scores_against_current_address(devices) -> None:
    """Restart inside an RPA lifetime keeps the same-address join."""
    first = DeviceResolver(devices)
    (id_a,) = await _resolve_cycle(
        first,
        _ad(
            address="5E:11:11:11:11:11",
            local_name="Tag",
            manufacturer_data={"76": "01"},
        ),
    )
    # rotation fuses (payload+name = 0.75), touching the row's
    # address to the new RPA
    await _resolve_cycle(
        first,
        _ad(
            address="43:22:22:22:22:22",
            local_name="Tag",
            manufacturer_data={"76": "01"},
        ),
    )
    assert len(await devices.list_devices()) == 1
    restarted = DeviceResolver(devices)
    await restarted.seed()
    # same current RPA, new payload key: must join via same-address
    (id_b,) = await _resolve_cycle(
        restarted,
        _ad(address="43:22:22:22:22:22", manufacturer_data={"76": "03"}),
    )
    assert id_b == id_a


# ---------------------------------------------------------------------------
# Durable aliases (#148): persist from resolve(), consume on lookup/seed
# ---------------------------------------------------------------------------


def _rotation_pair() -> tuple[Advertisement, Advertisement]:
    """Name + payload continuity: scores 0.75, fuses at the default floor."""
    founding = _ad(
        address="5E:11:11:11:11:11",
        address_type="rpa",
        local_name="Tag",
        manufacturer_data={"76": "aabbcc"},
    )
    rotated = _ad(
        address="43:22:22:22:22:22",
        address_type="rpa",
        local_name="Tag",
        manufacturer_data={"76": "aabbcc"},
    )
    return founding, rotated


@pytest.mark.asyncio
async def test_fused_key_is_recorded_as_alias(devices) -> None:
    resolver = DeviceResolver(devices)
    founding, rotated = _rotation_pair()
    (device_id,) = await _resolve_cycle(resolver, founding)
    await _resolve_cycle(resolver, rotated)
    aliases = await devices.list_aliases(device_id)
    assert [row["fingerprint"] for row in aliases] == [
        fingerprint_key(Fingerprint.from_advertisement(rotated))
    ]


@pytest.mark.asyncio
async def test_founding_key_is_not_recorded_as_alias(devices) -> None:
    resolver = DeviceResolver(devices)
    ad = _ad(local_name="Sensor")
    (device_id,) = await _resolve_cycle(resolver, ad)
    await _resolve_cycle(resolver, ad)
    assert await devices.list_aliases(device_id) == []


@pytest.mark.asyncio
async def test_fused_key_survives_restart_via_alias_not_window(
    devices,
) -> None:
    """Alias lookup recovers a rotated key with no window to re-score."""
    first = DeviceResolver(devices)
    founding, rotated = _rotation_pair()
    (id_a,) = await _resolve_cycle(first, founding)
    await _resolve_cycle(first, rotated)
    restarted = DeviceResolver(devices, recent_window=0)
    (id_b,) = await _resolve_cycle(restarted, rotated)
    assert id_b == id_a
    assert len(await devices.list_devices()) == 1


@pytest.mark.asyncio
async def test_seed_warms_alias_for_window_scoring(devices) -> None:
    """A later key can score against a seeded alias, not just the founding.

    Founding has payload only. The fused alias adds a name. A third
    advertisement sharing name+payload with the alias (0.75) but only
    payload with the founding key (0.5) must join after seed.
    """
    first = DeviceResolver(devices)
    founding = _ad(
        address="5E:11:11:11:11:11",
        manufacturer_data={"76": "aabbcc"},
    )
    aliased = _ad(
        address="5E:11:11:11:11:11",
        local_name="Tag",
        manufacturer_data={"76": "aabbcc"},
    )
    (id_a,) = await _resolve_cycle(first, founding)
    await _resolve_cycle(first, aliased)
    restarted = DeviceResolver(devices)
    await restarted.seed()
    later = _ad(
        address="43:22:22:22:22:22",
        local_name="Tag",
        manufacturer_data={"76": "aabbcc"},
    )
    (id_b,) = await _resolve_cycle(restarted, later)
    assert id_b == id_a


@pytest.mark.asyncio
async def test_seed_does_not_let_aliases_evict_other_devices(
    devices,
) -> None:
    """Founding keys keep the window budget under alias volume.

    One device minting many fused aliases (same-address payload ticks)
    must not flush every other device out of a tight seed window.
    """
    writer = DeviceResolver(devices)
    (id_b,) = await _resolve_cycle(
        writer,
        _ad(
            address="5E:22:22:22:22:22",
            address_type="rpa",
            local_name="DevB",
            manufacturer_data={"76": "ff"},
        ),
    )
    await _resolve_cycle(
        writer,
        _ad(
            address="5E:11:11:11:11:11",
            address_type="rpa",
            local_name="DevA",
            manufacturer_data={"76": "00"},
        ),
    )
    for i in range(5):
        await _resolve_cycle(
            writer,
            _ad(
                address="5E:11:11:11:11:11",
                address_type="rpa",
                local_name="DevA",
                manufacturer_data={"76": f"{i + 1:02x}"},
            ),
        )
    restarted = DeviceResolver(devices, recent_window=2)
    await restarted.seed()
    (id_b2,) = await _resolve_cycle(
        restarted,
        _ad(
            address="43:33:33:33:33:33",
            address_type="rpa",
            local_name="DevB",
            manufacturer_data={"76": "ff"},
        ),
    )
    assert id_b2 == id_b
    assert len(await devices.list_devices()) == 2


@pytest.mark.asyncio
async def test_abort_rolls_back_alias_write(devices) -> None:
    resolver = DeviceResolver(devices)
    founding, rotated = _rotation_pair()
    (device_id,) = await _resolve_cycle(resolver, founding)
    try:
        async with transaction(resolver.connection):
            await resolver.resolve(rotated)
            raise RuntimeError("cycle fails")
    except RuntimeError:
        resolver.abort()
    assert await devices.list_aliases(device_id) == []
    await _resolve_cycle(resolver, rotated)
    assert len(await devices.list_aliases(device_id)) == 1


# ---------------------------------------------------------------------------
# Alias retention + seed cache cap (#151)
# ---------------------------------------------------------------------------


def _named_payload(address: str, tick: str) -> Advertisement:
    """Name + payload continuity; address ticks mint a new fused key."""
    return _ad(
        address=address,
        address_type="rpa",
        local_name="Tag",
        manufacturer_data={"76": tick},
    )


@pytest.mark.asyncio
async def test_over_cap_device_resolves_via_founding_window_newest(
    devices, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Oldest aliases drop; founding, newest, and window still join."""
    monkeypatch.setattr(repo_mod, "_MAX_ALIASES_PER_DEVICE", 3)
    resolver = DeviceResolver(devices)
    founding = _named_payload("5E:11:11:11:11:11", "aa")
    (device_id,) = await _resolve_cycle(resolver, founding)
    rotated = [
        _named_payload(f"43:22:22:22:22:{i:02X}", "aa") for i in range(5)
    ]
    for ad in rotated:
        await _resolve_cycle(resolver, ad)
    aliases = await devices.list_aliases(device_id)
    assert len(aliases) == 3
    oldest_key = fingerprint_key(Fingerprint.from_advertisement(rotated[0]))
    newest_key = fingerprint_key(Fingerprint.from_advertisement(rotated[-1]))
    assert await devices.get_by_alias(oldest_key) is None
    assert await devices.get_by_alias(newest_key) is not None

    (id_founding,) = await _resolve_cycle(DeviceResolver(devices), founding)
    assert id_founding == device_id

    cold = DeviceResolver(devices, recent_window=0)
    (id_newest,) = await _resolve_cycle(cold, rotated[-1])
    assert id_newest == device_id

    restarted = DeviceResolver(devices)
    await restarted.seed()
    later = _named_payload("AA:33:33:33:33:33", "aa")
    (id_later,) = await _resolve_cycle(restarted, later)
    assert id_later == device_id
    assert len(await devices.list_devices()) == 1


@pytest.mark.asyncio
async def test_pruned_alias_outside_window_opens_new_device(
    devices, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documented residual: pruned key + cold window splits identity."""
    monkeypatch.setattr(repo_mod, "_MAX_ALIASES_PER_DEVICE", 3)
    resolver = DeviceResolver(devices)
    founding = _named_payload("5E:11:11:11:11:11", "aa")
    (device_id,) = await _resolve_cycle(resolver, founding)
    rotated = [
        _named_payload(f"43:22:22:22:22:{i:02X}", "aa") for i in range(5)
    ]
    for ad in rotated:
        await _resolve_cycle(resolver, ad)
    cold = DeviceResolver(devices, recent_window=0)
    (id_pruned,) = await _resolve_cycle(cold, rotated[0])
    assert id_pruned != device_id
    assert len(await devices.list_devices()) == 2


@pytest.mark.asyncio
async def test_seed_key_cache_is_bounded(
    devices, monkeypatch: pytest.MonkeyPatch
) -> None:
    """seed() must not load an unbounded alias history into RAM."""
    monkeypatch.setattr(resolver_mod, "_MAX_KEY_CACHE", 4)
    writer = DeviceResolver(devices)
    founding = _named_payload("5E:11:11:11:11:11", "aa")
    (device_id,) = await _resolve_cycle(writer, founding)
    newest = founding
    for i in range(8):
        newest = _named_payload(f"43:22:22:22:22:{i:02X}", "aa")
        await _resolve_cycle(writer, newest)
    restarted = DeviceResolver(devices)
    await restarted.seed()
    assert len(restarted._key_cache) <= 4
    founding_key = fingerprint_key(Fingerprint.from_advertisement(founding))
    newest_key = fingerprint_key(Fingerprint.from_advertisement(newest))
    assert founding_key in restarted._key_cache
    assert newest_key in restarted._key_cache
    (id_newest,) = await _resolve_cycle(restarted, newest)
    assert id_newest == device_id


# ---------------------------------------------------------------------------
# Seed batch-load + alias cache-hit touch (#153)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_loads_aliases_in_one_round_trip(
    devices, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N devices with aliases must not N+1 list_aliases."""
    writer = DeviceResolver(devices)
    for i in range(3):
        founding = _ad(
            address=f"5E:11:11:11:11:{i:02X}",
            address_type="rpa",
            local_name=f"D{i}",
            manufacturer_data={"76": "aabbcc"},
        )
        rotated = _ad(
            address=f"43:22:22:22:22:{i:02X}",
            address_type="rpa",
            local_name=f"D{i}",
            manufacturer_data={"76": "aabbcc"},
        )
        await _resolve_cycle(writer, founding)
        await _resolve_cycle(writer, rotated)

    calls = {"list": 0, "batch": 0}
    orig_list = DeviceRepository.list_aliases
    orig_batch = DeviceRepository.list_aliases_for_devices

    async def counting_list(
        self: DeviceRepository, device_id: int
    ) -> list[DeviceAliasRow]:
        calls["list"] += 1
        return await orig_list(self, device_id)

    async def counting_batch(
        self: DeviceRepository, device_ids: Sequence[int]
    ) -> list[DeviceAliasRow]:
        calls["batch"] += 1
        return await orig_batch(self, device_ids)

    monkeypatch.setattr(DeviceRepository, "list_aliases", counting_list)
    monkeypatch.setattr(
        DeviceRepository, "list_aliases_for_devices", counting_batch
    )

    restarted = DeviceResolver(devices)
    await restarted.seed()
    assert calls["list"] == 0
    assert calls["batch"] == 1


@pytest.mark.asyncio
async def test_seeded_alias_cache_hit_touches_new_address(devices) -> None:
    """A seeded alias key must still refresh devices.address."""
    first = DeviceResolver(devices)
    founding, rotated = _rotation_pair()
    (device_id,) = await _resolve_cycle(first, founding)
    await _resolve_cycle(first, rotated)
    later = _ad(
        address="AA:33:33:33:33:33",
        address_type="rpa",
        local_name="Tag",
        manufacturer_data={"76": "aabbcc"},
    )
    await _resolve_cycle(first, later)
    row = await devices.get(device_id)
    assert row is not None and row["address"] == later.address

    restarted = DeviceResolver(devices)
    await restarted.seed()
    await _resolve_cycle(restarted, rotated)
    row = await devices.get(device_id)
    assert row is not None and row["address"] == rotated.address


@pytest.mark.asyncio
async def test_live_alias_cache_hit_touches_new_address(devices) -> None:
    """Alias keys published by commit() also refresh devices.address."""
    resolver = DeviceResolver(devices)
    founding, rotated = _rotation_pair()
    (device_id,) = await _resolve_cycle(resolver, founding)
    await _resolve_cycle(resolver, rotated)
    later = _ad(
        address="AA:33:33:33:33:33",
        address_type="rpa",
        local_name="Tag",
        manufacturer_data={"76": "aabbcc"},
    )
    await _resolve_cycle(resolver, later)
    row = await devices.get(device_id)
    assert row is not None and row["address"] == later.address
    await _resolve_cycle(resolver, rotated)
    row = await devices.get(device_id)
    assert row is not None and row["address"] == rotated.address


@pytest.mark.asyncio
async def test_seeded_founding_cache_hit_does_not_touch_address(
    devices,
) -> None:
    """Founding-key cache hits stay a no-touch (#84 / #153)."""
    first = DeviceResolver(devices)
    founding, rotated = _rotation_pair()
    (device_id,) = await _resolve_cycle(first, founding)
    await _resolve_cycle(first, rotated)
    row = await devices.get(device_id)
    assert row is not None and row["address"] == rotated.address

    restarted = DeviceResolver(devices)
    await restarted.seed()
    await _resolve_cycle(restarted, founding)
    row = await devices.get(device_id)
    assert row is not None and row["address"] == rotated.address
