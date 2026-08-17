# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""BleakScanner adapter tests (P1-3).

TDD: failing tests first, then implementation. The BleakScanner
implements the Scanner protocol using the bleak library for real
BLE scanning on macOS (CoreBluetooth) and Linux (BlueZ).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from blesentry.scanner import Advertisement
from blesentry.scanner.bleak import BleakScanner


def test_bleak_scanner_implements_scanner_protocol() -> None:
    """BleakScanner is a structural subtype of Scanner."""
    assert hasattr(BleakScanner, "scan")


@pytest.mark.asyncio
async def test_bleak_scanner_instantiates() -> None:
    """BleakScanner can be instantiated with an adapter_id."""
    scanner = BleakScanner(adapter_id="test-adapter")
    assert scanner.adapter_id == "test-adapter"


@pytest.mark.asyncio
async def test_bleak_scanner_scan_returns_list() -> None:
    """scan() returns a list of Advertisement objects."""
    scanner = BleakScanner(adapter_id="test-adapter")
    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(return_value=[])
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert isinstance(result, list)


@pytest.mark.asyncio
async def test_bleak_scanner_handles_empty_discovery() -> None:
    """Empty discovery returns empty list."""
    scanner = BleakScanner(adapter_id="test-adapter")
    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(return_value=[])
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert result == []


@pytest.mark.asyncio
async def test_bleak_scanner_converts_advertisement() -> None:
    """Discovered devices are converted to Advertisement objects."""
    scanner = BleakScanner(adapter_id="test-adapter")
    mock_device = MagicMock()
    mock_device.address = "AA:BB:CC:DD:EE:FF"
    mock_device.rssi = -65
    mock_device.name = "Test Device"
    mock_device.uuids = ["180D"]
    mock_device.metadata = {
        "manufacturer_data": {76: b"\x01\x02"},
        "service_data": {},
        "tx_power": None,
    }
    
    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(return_value=[mock_device])
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert len(result) == 1
        assert isinstance(result[0], Advertisement)
        assert result[0].mac == "AA:BB:CC:DD:EE:FF"
        assert result[0].rssi == -65
        assert result[0].local_name == "Test Device"
        assert result[0].adapter_id == "test-adapter"


@pytest.mark.asyncio
async def test_bleak_scanner_handles_multiple_devices() -> None:
    """Multiple discovered devices are all converted."""
    scanner = BleakScanner(adapter_id="test-adapter")
    devices = []
    for i in range(3):
        mock_device = MagicMock()
        mock_device.address = f"AA:BB:CC:DD:EE:{i:02X}"
        mock_device.rssi = -60 - i
        mock_device.name = f"Device {i}"
        mock_device.uuids = []
        mock_device.metadata = {
            "manufacturer_data": {},
            "service_data": {},
            "tx_power": None,
        }
        devices.append(mock_device)
    
    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(return_value=devices)
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert len(result) == 3
        assert all(isinstance(ad, Advertisement) for ad in result)


@pytest.mark.asyncio
async def test_bleak_scanner_logs_and_degrades_on_error() -> None:
    """D-Bus errors are logged and result in empty list, not exception."""
    scanner = BleakScanner(adapter_id="test-adapter")
    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(
            side_effect=Exception("D-Bus error")
        )
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert result == []


@pytest.mark.asyncio
async def test_bleak_scanner_handles_device_without_name() -> None:
    """Devices with no name get None for local_name."""
    scanner = BleakScanner(adapter_id="test-adapter")
    mock_device = MagicMock()
    mock_device.address = "AA:BB:CC:DD:EE:FF"
    mock_device.rssi = -70
    mock_device.name = None
    mock_device.uuids = []
    mock_device.metadata = {
        "manufacturer_data": {},
        "service_data": {},
        "tx_power": None,
    }
    
    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(return_value=[mock_device])
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert result[0].local_name is None


@pytest.mark.asyncio
async def test_bleak_scanner_handles_manufacturer_data() -> None:
    """Manufacturer data bytes are converted to hex strings."""
    scanner = BleakScanner(adapter_id="test-adapter")
    mock_device = MagicMock()
    mock_device.address = "AA:BB:CC:DD:EE:FF"
    mock_device.rssi = -65
    mock_device.name = "Test"
    mock_device.uuids = []
    mock_device.metadata = {
        "manufacturer_data": {76: b"\x01\x02\x03"},
        "service_data": {},
        "tx_power": -4,
    }
    
    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(return_value=[mock_device])
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert result[0].manufacturer_data == {"76": "010203"}
        assert result[0].tx_power == -4
