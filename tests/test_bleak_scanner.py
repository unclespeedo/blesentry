# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""BleakScanner adapter tests (P1-3, reworked for #67).

Two layers, split so mock drift cannot hide API breakage again:

- Behaviour tests mock ``BleakScanner._run_scan`` — a seam we own —
  and verify conversion, fail-fast propagation, and configuration
  validation.
- Contract tests exercise the *real* bleak library: introspection
  everywhere, real construction on Linux (CI), so a bleak API change
  fails in CI instead of on the Pi. Construction is skipped on macOS
  because creating the CoreBluetooth backend can trigger the TCC
  Bluetooth permission prompt.
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Sequence
from typing import cast
from unittest.mock import AsyncMock

import pytest
from bleak import BleakScanner as _RealBleakScanner
from bleak.args.bluez import BlueZScannerArgs, OrPattern, OrPatternLike
from bleak.assigned_numbers import AdvertisementDataType
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from blesentry.scanner import Advertisement
from blesentry.scanner.bleak import BleakScanner

OR_PATTERNS = [OrPattern(0, AdvertisementDataType.FLAGS, b"\x06")]


def _scanner(
    adapter_id: str = "test-adapter",
    *,
    adapter: str | None = None,
    or_patterns: Sequence[OrPatternLike] | None = None,
) -> BleakScanner:
    """Build an adapter that is valid on every platform."""
    return BleakScanner(
        adapter_id,
        adapter=adapter,
        or_patterns=OR_PATTERNS if or_patterns is None else or_patterns,
    )


def _make_device(
    address: str = "AA:BB:CC:DD:EE:FF",
    name: str | None = "Test Device",
) -> BLEDevice:
    """Create a real BLEDevice for testing."""
    return BLEDevice(address=address, name=name, details={})


def _make_adv_data(
    rssi: int = -65,
    local_name: str | None = "Test Device",
    manufacturer_data: dict[int, bytes] | None = None,
    service_data: dict[str, bytes] | None = None,
    service_uuids: list[str] | None = None,
    tx_power: int | None = None,
) -> AdvertisementData:
    """Create a real AdvertisementData for testing."""
    return AdvertisementData(
        local_name=local_name,
        manufacturer_data=manufacturer_data or {},
        service_data=service_data or {},
        service_uuids=service_uuids or [],
        tx_power=tx_power,
        rssi=rssi,
        platform_data=(),
    )


def _patch_run_scan(
    scanner: BleakScanner,
    results: dict[str, tuple[BLEDevice, AdvertisementData]],
) -> AsyncMock:
    """Mock the bleak seam with scripted scan results."""
    mock = AsyncMock(return_value=results)
    scanner._run_scan = mock  # type: ignore[method-assign]
    return mock


# ---------------------------------------------------------------------------
# Construction / configuration validation (fail-fast contract, #63)
# ---------------------------------------------------------------------------


def test_bleak_scanner_implements_scanner_protocol() -> None:
    assert hasattr(BleakScanner, "scan")


def test_bleak_scanner_instantiates() -> None:
    scanner = _scanner()
    assert scanner.adapter_id == "test-adapter"


def test_missing_or_patterns_raises_on_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Linux, constructing without or_patterns fails fast."""
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ValueError, match="or_patterns"):
        BleakScanner(adapter_id="bluez-linux")


def test_empty_or_patterns_raises_on_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(ValueError, match="or_patterns"):
        BleakScanner(adapter_id="bluez-linux", or_patterns=[])


def test_or_patterns_optional_on_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CoreBluetooth ignores or_patterns; construction succeeds."""
    monkeypatch.setattr(sys, "platform", "darwin")
    BleakScanner(adapter_id="macos-corebluetooth")


# ---------------------------------------------------------------------------
# Behaviour: conversion and result shaping (mocked at the _run_scan seam)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_returns_empty_list_on_quiet_window() -> None:
    """A quiet scan is a successful scan: empty list, no error."""
    scanner = _scanner()
    _patch_run_scan(scanner, {})
    assert await scanner.scan(duration=1.0) == []


@pytest.mark.asyncio
async def test_scan_passes_duration_to_run_scan() -> None:
    scanner = _scanner()
    mock = _patch_run_scan(scanner, {})
    await scanner.scan(duration=2.5)
    mock.assert_awaited_once_with(2.5)


@pytest.mark.asyncio
async def test_scan_converts_advertisement() -> None:
    scanner = _scanner()
    device = _make_device("AA:BB:CC:DD:EE:FF", "Test Device")
    adv_data = _make_adv_data(rssi=-65, local_name="Test Device")
    _patch_run_scan(scanner, {device.address: (device, adv_data)})

    result = await scanner.scan(duration=1.0)
    assert len(result) == 1
    assert isinstance(result[0], Advertisement)
    assert result[0].address == "AA:BB:CC:DD:EE:FF"
    assert result[0].rssi == -65
    assert result[0].local_name == "Test Device"
    assert result[0].adapter_id == "test-adapter"


@pytest.mark.asyncio
async def test_scan_converts_multiple_devices() -> None:
    scanner = _scanner()
    results = {}
    for i in range(3):
        device = _make_device(f"AA:BB:CC:DD:EE:{i:02X}", f"Device {i}")
        adv_data = _make_adv_data(rssi=-60 - i, local_name=f"Device {i}")
        results[device.address] = (device, adv_data)
    _patch_run_scan(scanner, results)

    result = await scanner.scan(duration=1.0)
    assert len(result) == 3
    assert all(isinstance(ad, Advertisement) for ad in result)


@pytest.mark.asyncio
async def test_scan_handles_device_without_name() -> None:
    scanner = _scanner()
    device = _make_device("AA:BB:CC:DD:EE:FF", None)
    adv_data = _make_adv_data(rssi=-70, local_name=None)
    _patch_run_scan(scanner, {device.address: (device, adv_data)})

    result = await scanner.scan(duration=1.0)
    assert result[0].local_name is None


@pytest.mark.asyncio
async def test_scan_converts_manufacturer_data_to_hex() -> None:
    scanner = _scanner()
    device = _make_device("AA:BB:CC:DD:EE:FF", "Test")
    adv_data = _make_adv_data(
        rssi=-65,
        local_name="Test",
        manufacturer_data={76: b"\x01\x02\x03"},
        tx_power=-4,
    )
    _patch_run_scan(scanner, {device.address: (device, adv_data)})

    result = await scanner.scan(duration=1.0)
    assert result[0].manufacturer_data == {"76": "010203"}
    assert result[0].tx_power == -4


@pytest.mark.asyncio
async def test_scan_converts_service_data_to_hex() -> None:
    scanner = _scanner()
    device = _make_device("AA:BB:CC:DD:EE:FF", "Test")
    adv_data = _make_adv_data(rssi=-65, service_data={"180D": b"\x01\x02"})
    _patch_run_scan(scanner, {device.address: (device, adv_data)})

    result = await scanner.scan(duration=1.0)
    assert result[0].service_data == {"180D": "0102"}


@pytest.mark.asyncio
async def test_scan_passes_service_uuids_through() -> None:
    scanner = _scanner()
    device = _make_device("AA:BB:CC:DD:EE:FF", "Test")
    adv_data = _make_adv_data(rssi=-65, service_uuids=["180D", "180F"])
    _patch_run_scan(scanner, {device.address: (device, adv_data)})

    result = await scanner.scan(duration=1.0)
    assert result[0].service_uuids == ("180D", "180F")


@pytest.mark.asyncio
async def test_scan_converts_empty_containers() -> None:
    scanner = _scanner()
    device = _make_device("AA:BB:CC:DD:EE:FF", "Test")
    adv_data = _make_adv_data(
        rssi=-65,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
    )
    _patch_run_scan(scanner, {device.address: (device, adv_data)})

    result = await scanner.scan(duration=1.0)
    assert result[0].manufacturer_data == {}
    assert result[0].service_data == {}
    assert result[0].service_uuids == ()


# ---------------------------------------------------------------------------
# Behaviour: error contract (fail fast; degrade per-advertisement only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_failure_propagates() -> None:
    """Scanner-level failure raises — never a silent empty list."""
    scanner = _scanner()
    scanner._run_scan = AsyncMock(  # type: ignore[method-assign]
        side_effect=OSError("D-Bus error")
    )
    with pytest.raises(OSError, match="D-Bus error"):
        await scanner.scan(duration=1.0)


@pytest.mark.asyncio
async def test_malformed_advertisement_is_skipped_not_fatal() -> None:
    """One bad advertisement is dropped; the rest still convert."""
    scanner = _scanner()
    good = _make_device("AA:BB:CC:DD:EE:01", "Good")
    good_adv = _make_adv_data(rssi=-60, local_name="Good")
    bad = _make_device("AA:BB:CC:DD:EE:02", "Bad")
    _patch_run_scan(
        scanner,
        {
            good.address: (good, good_adv),
            # not an AdvertisementData: attribute access raises
            bad.address: (bad, cast(AdvertisementData, object())),
        },
    )

    result = await scanner.scan(duration=1.0)
    assert [ad.address for ad in result] == ["AA:BB:CC:DD:EE:01"]


# ---------------------------------------------------------------------------
# Contract tests: the real bleak API. Introspection runs everywhere;
# construction runs on Linux (CI), where backend __init__ has no side
# effects. These are the tests that catch #67-class drift.
# ---------------------------------------------------------------------------


def test_contract_discover_is_a_classmethod() -> None:
    """Documents why _run_scan never calls discover().

    A classmethod constructs a fresh default scanner via cls(**kwargs)
    and would discard everything _create_scanner configured — the #67
    root cause.
    """
    attr = inspect.getattr_static(_RealBleakScanner, "discover")
    assert isinstance(attr, classmethod)


def test_contract_results_property_exists() -> None:
    attr = inspect.getattr_static(
        _RealBleakScanner, "discovered_devices_and_advertisement_data"
    )
    assert isinstance(attr, property)


def test_contract_constructor_accepts_our_arguments() -> None:
    sig = inspect.signature(_RealBleakScanner.__init__)
    assert "scanning_mode" in sig.parameters
    assert "bluez" in sig.parameters


def test_contract_bluez_args_accepts_our_keys() -> None:
    keys = BlueZScannerArgs.__annotations__
    assert "adapter" in keys
    assert "or_patterns" in keys


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="CoreBluetooth backend construction can prompt for "
    "Bluetooth permission; Linux construction is side-effect free",
)
def test_contract_create_scanner_constructs_on_linux() -> None:
    """The exact production configuration constructs without error."""
    scanner = _scanner(adapter="hci0")
    real = scanner._create_scanner()
    assert isinstance(real, _RealBleakScanner)


@pytest.mark.skipif(
    sys.platform == "darwin",
    reason="BlueZ backend is Linux-only",
)
def test_contract_bleak_rejects_passive_without_or_patterns() -> None:
    """Bleak's own guard — the raise docs/risks.md documents (#67)."""
    from bleak.exc import BleakError

    with pytest.raises(BleakError):
        _RealBleakScanner(scanning_mode="passive")


# ---------------------------------------------------------------------------
# Address-type extraction (#56): BlueZ AddressType refined by MAC bits
# ---------------------------------------------------------------------------


def test_address_type_public_passthrough() -> None:
    scanner = _scanner()
    device = BLEDevice(
        address="00:11:22:33:44:55",
        name=None,
        details={"props": {"AddressType": "public"}},
    )
    assert scanner._address_type(device) == "public"


@pytest.mark.parametrize(
    ("first_octet", "expected"),
    [
        ("F0", "random_static"),
        ("5E", "rpa"),
        ("2C", "non_resolvable"),
    ],
)
def test_address_type_random_refined_by_top_bits(
    first_octet: str, expected: str
) -> None:
    scanner = _scanner()
    device = BLEDevice(
        address=f"{first_octet}:11:22:33:44:55",
        name=None,
        details={"props": {"AddressType": "random"}},
    )
    assert scanner._address_type(device) == expected


def test_address_type_absent_details_is_none() -> None:
    scanner = _scanner()
    device = _make_device()
    assert scanner._address_type(device) is None


@pytest.mark.asyncio
async def test_scan_stamps_address_type() -> None:
    scanner = _scanner()
    device = BLEDevice(
        address="F0:11:22:33:44:55",
        name=None,
        details={"props": {"AddressType": "random"}},
    )
    adv_data = _make_adv_data(rssi=-60, local_name=None)
    _patch_run_scan(scanner, {device.address: (device, adv_data)})
    result = await scanner.scan(duration=1.0)
    assert result[0].address_type == "random_static"
