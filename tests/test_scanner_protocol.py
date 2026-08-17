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
    assert result_a[0].mac == batch_a[0].mac
    assert result_b[0].mac == batch_b[0].mac


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
    assert scan2[0].mac == device.mac


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
    assert scan1[0].mac == old_mac.mac
    assert scan2[0].mac == new_mac.mac
    assert scan1[0].mac != scan2[0].mac


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
