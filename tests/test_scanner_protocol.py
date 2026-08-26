# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Scanner protocol + MockScanner fixture replay (P1-2).

TDD: the ``Scanner`` protocol defines the scan seam consumed by the
rest of the system; ``MockScanner`` replays scripted advertisement
sequences so every downstream test runs without hardware.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from blesentry.scanner import Advertisement, MockScanner

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = FIXTURES / "corpus-macos-corebluetooth.json"


def _corpus() -> list[Advertisement]:
    raw = json.loads(CORPUS.read_text(encoding="utf-8"))
    return [Advertisement.model_validate(r) for r in raw]


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_scanner_protocol_has_scan_method() -> None:
    """Scanner exposes ``scan(duration) -> list[Advertisement]``."""
    assert hasattr(MockScanner, "scan")


# ---------------------------------------------------------------------------
# Basic scan behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_returns_list_of_advertisements() -> None:
    """scan() returns a list of Advertisement objects."""
    from blesentry.scanner import MockScanner

    ads = _corpus()[:3]
    scanner = MockScanner(scenarios=[ads])
    result = await scanner.scan(duration=1.0)
    assert isinstance(result, list)
    assert all(isinstance(a, Advertisement) for a in result)


@pytest.mark.asyncio
async def test_scan_returns_empty_when_no_scenario() -> None:
    """Exhausted scenarios yield an empty list."""
    from blesentry.scanner import MockScanner

    scanner = MockScanner(scenarios=[])
    result = await scanner.scan(duration=1.0)
    assert result == []


@pytest.mark.asyncio
async def test_scan_returns_empty_on_exhaustion() -> None:
    """Second scan() after one scenario consumed is empty."""
    from blesentry.scanner import MockScanner

    ads = _corpus()[:2]
    scanner = MockScanner(scenarios=[ads])
    await scanner.scan(duration=1.0)
    result = await scanner.scan(duration=1.0)
    assert result == []


@pytest.mark.asyncio
async def test_scan_consumes_in_order() -> None:
    """Scenarios are consumed FIFO — first scan gets the first batch."""
    from blesentry.scanner import MockScanner

    batch_a = [_corpus()[0]]
    batch_b = [_corpus()[1]]
    scanner = MockScanner(scenarios=[batch_a, batch_b])
    result_a = await scanner.scan(duration=1.0)
    result_b = await scanner.scan(duration=1.0)
    assert result_a[0].address == batch_a[0].address
    assert result_b[0].address == batch_b[0].address


# ---------------------------------------------------------------------------
# Scripted scenarios: appear / disappear / MAC rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_device_appears() -> None:
    """Device absent then present across two scan windows."""
    from blesentry.scanner import MockScanner

    device = _corpus()[0]
    scanner = MockScanner(scenarios=[[], [device]])
    scan1 = await scanner.scan(duration=5.0)
    scan2 = await scanner.scan(duration=5.0)
    assert scan1 == []
    assert len(scan2) == 1
    assert scan2[0].address == device.address


@pytest.mark.asyncio
async def test_scenario_device_disappears() -> None:
    """Device present then absent across two scan windows."""
    from blesentry.scanner import MockScanner

    device = _corpus()[0]
    scanner = MockScanner(scenarios=[[device], []])
    scan1 = await scanner.scan(duration=5.0)
    scan2 = await scanner.scan(duration=5.0)
    assert len(scan1) == 1
    assert scan2 == []


@pytest.mark.asyncio
async def test_scenario_mac_rotation() -> None:
    """Same device with two different MACs (Apple randomization)."""
    from blesentry.scanner import MockScanner

    ads = _corpus()
    old_mac = ads[0]
    new_mac = ads[1]
    scanner = MockScanner(scenarios=[[old_mac], [new_mac]])
    scan1 = await scanner.scan(duration=5.0)
    scan2 = await scanner.scan(duration=5.0)
    assert scan1[0].address == old_mac.address
    assert scan2[0].address == new_mac.address
    assert scan1[0].address != scan2[0].address


@pytest.mark.asyncio
async def test_scenario_multi_device_appear_disappear() -> None:
    """Multiple devices appear, then one drops out."""
    from blesentry.scanner import MockScanner

    ads = _corpus()
    a, b, c = ads[0], ads[1], ads[2]
    scanner = MockScanner(scenarios=[[a, b, c], [a, b], [a]])
    scan1 = await scanner.scan(duration=5.0)
    scan2 = await scanner.scan(duration=5.0)
    scan3 = await scanner.scan(duration=5.0)
    assert len(scan1) == 3
    assert len(scan2) == 2
    assert len(scan3) == 1


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_from_corpus_loads_fixture_file() -> None:
    """from_corpus() loads a JSON fixture and produces a MockScanner."""
    from blesentry.scanner import MockScanner

    scanner = MockScanner.from_corpus(CORPUS)
    result = await scanner.scan(duration=1.0)
    assert len(result) == 13
    assert all(isinstance(a, Advertisement) for a in result)


@pytest.mark.asyncio
async def test_from_corpus_single_window() -> None:
    """from_corpus() replays entire corpus in one scan window."""
    from blesentry.scanner import MockScanner

    scanner = MockScanner.from_corpus(CORPUS)
    first = await scanner.scan(duration=1.0)
    second = await scanner.scan(duration=1.0)
    assert len(first) == 13
    assert second == []


# ---------------------------------------------------------------------------
# Per-device RSSI sequences (#58)
# ---------------------------------------------------------------------------


def _proto(address: str, rssi: int = -50, ts: float = 1.0) -> Advertisement:
    return Advertisement(
        address=address, rssi=rssi, timestamp=ts, adapter_id="test"
    )


@pytest.mark.asyncio
async def test_rssi_sequence_applies_per_window() -> None:
    """Each scan() window carries the next RSSI in the sequence."""
    addr = "AA:00:00:00:00:01"
    scanner = MockScanner.from_rssi_sequences(
        templates={addr: _proto(addr, rssi=-99)},
        sequences={addr: [-90, -70, -50]},
    )
    assert [a.rssi for a in await scanner.scan(1.0)] == [-90]
    assert [a.rssi for a in await scanner.scan(1.0)] == [-70]
    assert [a.rssi for a in await scanner.scan(1.0)] == [-50]
    assert await scanner.scan(1.0) == []


@pytest.mark.asyncio
async def test_rssi_sequence_none_omits_device() -> None:
    """None in the sequence means the device is absent that window."""
    addr = "AA:00:00:00:00:02"
    scanner = MockScanner.from_rssi_sequences(
        templates={addr: _proto(addr)},
        sequences={addr: [-60, None, -60]},
    )
    assert len(await scanner.scan(1.0)) == 1
    assert await scanner.scan(1.0) == []
    assert len(await scanner.scan(1.0)) == 1


@pytest.mark.asyncio
async def test_rssi_sequence_shorter_device_drops_out() -> None:
    """A shorter sequence ends; the other device keeps emitting."""
    a, b = "AA:00:00:00:00:0A", "AA:00:00:00:00:0B"
    scanner = MockScanner.from_rssi_sequences(
        templates={a: _proto(a), b: _proto(b)},
        sequences={a: [-40, -40], b: [-50, -50, -50]},
    )
    assert {x.address for x in await scanner.scan(1.0)} == {a, b}
    assert {x.address for x in await scanner.scan(1.0)} == {a, b}
    assert {x.address for x in await scanner.scan(1.0)} == {b}


@pytest.mark.asyncio
async def test_rssi_sequence_does_not_mutate_template() -> None:
    """Prototype advertisements stay frozen at their original RSSI."""
    addr = "AA:00:00:00:00:03"
    template = _proto(addr, rssi=-42)
    scanner = MockScanner.from_rssi_sequences(
        templates={addr: template},
        sequences={addr: [-80, -60]},
    )
    await scanner.scan(1.0)
    await scanner.scan(1.0)
    assert template.rssi == -42


@pytest.mark.asyncio
async def test_rssi_sequence_window_dt_advances_timestamps() -> None:
    """window_dt offsets each window's timestamp from the prototype."""
    addr = "AA:00:00:00:00:04"
    scanner = MockScanner.from_rssi_sequences(
        templates={addr: _proto(addr, ts=100.0)},
        sequences={addr: [-50, -50, -50]},
        window_dt=15.0,
    )
    stamps = [(await scanner.scan(1.0))[0].timestamp for _ in range(3)]
    assert stamps == [100.0, 115.0, 130.0]


@pytest.mark.asyncio
async def test_rssi_sequence_near_threshold_flicker() -> None:
    """Flicker around the gate: above, below, above — RSSI as scripted."""
    addr = "AA:00:00:00:00:05"
    gate = -80
    scanner = MockScanner.from_rssi_sequences(
        templates={addr: _proto(addr)},
        sequences={addr: [gate + 5, gate - 5, gate + 1]},
    )
    rssi = [(await scanner.scan(1.0))[0].rssi for _ in range(3)]
    assert rssi == [-75, -85, -79]


@pytest.mark.asyncio
async def test_rssi_sequence_gradual_approach() -> None:
    """Rising trajectory: far → near across successive windows."""
    addr = "AA:00:00:00:00:06"
    profile = [-95, -85, -75, -65, -55]
    scanner = MockScanner.from_rssi_sequences(
        templates={addr: _proto(addr)},
        sequences={addr: profile},
    )
    rssi = [(await scanner.scan(1.0))[0].rssi for _ in profile]
    assert rssi == profile


@pytest.mark.asyncio
async def test_rssi_sequence_brief_spike_then_gone() -> None:
    """Car pass-by: two strong windows, then absent."""
    addr = "AA:00:00:00:00:07"
    scanner = MockScanner.from_rssi_sequences(
        templates={addr: _proto(addr)},
        sequences={addr: [-40, -42, None, None]},
    )
    assert [(await scanner.scan(1.0))[0].rssi] == [-40]
    assert [(await scanner.scan(1.0))[0].rssi] == [-42]
    assert await scanner.scan(1.0) == []
    assert await scanner.scan(1.0) == []


def test_rssi_sequence_unknown_address_raises() -> None:
    """A sequence address with no prototype is a caller error."""
    with pytest.raises(ValueError, match="no prototype"):
        MockScanner.from_rssi_sequences(
            templates={"AA:00:00:00:00:08": _proto("AA:00:00:00:00:08")},
            sequences={"AA:00:00:00:00:99": [-50]},
        )


def test_rssi_sequence_key_must_match_advertisement_address() -> None:
    """The templates dict key must equal Advertisement.address."""
    with pytest.raises(ValueError, match="address"):
        MockScanner.from_rssi_sequences(
            templates={"AA:00:00:00:00:0C": _proto("AA:00:00:00:00:0D")},
            sequences={"AA:00:00:00:00:0C": [-50]},
        )
