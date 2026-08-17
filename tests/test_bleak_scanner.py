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
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from blesentry.scanner import Advertisement
from blesentry.scanner.bleak import BleakScanner


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
        mock_instance.discover = AsyncMock(return_value={})
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert isinstance(result, list)


@pytest.mark.asyncio
async def test_bleak_scanner_handles_empty_discovery() -> None:
    """Empty discovery returns empty list."""
    scanner = BleakScanner(adapter_id="test-adapter")
    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(return_value={})
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert result == []


@pytest.mark.asyncio
async def test_bleak_scanner_converts_advertisement() -> None:
    """Discovered devices are converted to Advertisement objects."""
    scanner = BleakScanner(adapter_id="test-adapter")
    device = _make_device("AA:BB:CC:DD:EE:FF", "Test Device")
    adv_data = _make_adv_data(rssi=-65, local_name="Test Device")

    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(
            return_value={device.address: (device, adv_data)}
        )
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
    devices = {}
    for i in range(3):
        device = _make_device(f"AA:BB:CC:DD:EE:{i:02X}", f"Device {i}")
        adv_data = _make_adv_data(rssi=-60 - i, local_name=f"Device {i}")
        devices[device.address] = (device, adv_data)

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
    device = _make_device("AA:BB:CC:DD:EE:FF", None)
    adv_data = _make_adv_data(rssi=-70, local_name=None)

    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(
            return_value={device.address: (device, adv_data)}
        )
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert result[0].local_name is None


@pytest.mark.asyncio
async def test_bleak_scanner_handles_manufacturer_data() -> None:
    """Manufacturer data bytes are converted to hex strings."""
    scanner = BleakScanner(adapter_id="test-adapter")
    device = _make_device("AA:BB:CC:DD:EE:FF", "Test")
    adv_data = _make_adv_data(
        rssi=-65,
        local_name="Test",
        manufacturer_data={76: b"\x01\x02\x03"},
        tx_power=-4,
    )

    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(
            return_value={device.address: (device, adv_data)}
        )
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert result[0].manufacturer_data == {"76": "010203"}
        assert result[0].tx_power == -4


@pytest.mark.asyncio
async def test_bleak_scanner_handles_service_data() -> None:
    """Service data bytes are converted to hex strings."""
    scanner = BleakScanner(adapter_id="test-adapter")
    device = _make_device("AA:BB:CC:DD:EE:FF", "Test")
    adv_data = _make_adv_data(
        rssi=-65,
        service_data={"180D": b"\x01\x02"},
    )

    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(
            return_value={device.address: (device, adv_data)}
        )
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert result[0].service_data == {"180D": "0102"}


@pytest.mark.asyncio
async def test_bleak_scanner_handles_service_uuids() -> None:
    """Service UUIDs are passed through correctly."""
    scanner = BleakScanner(adapter_id="test-adapter")
    device = _make_device("AA:BB:CC:DD:EE:FF", "Test")
    adv_data = _make_adv_data(
        rssi=-65,
        service_uuids=["180D", "180F"],
    )

    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(
            return_value={device.address: (device, adv_data)}
        )
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert result[0].service_uuids == ("180D", "180F")


@pytest.mark.asyncio
async def test_bleak_scanner_passes_return_adv_true() -> None:
    """discover() is called with return_adv=True."""
    scanner = BleakScanner(adapter_id="test-adapter")
    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(return_value={})
        mock_cls.return_value = mock_instance
        await scanner.scan(duration=2.5)
        mock_instance.discover.assert_called_once_with(
            timeout=2.5, return_adv=True
        )


@pytest.mark.asyncio
async def test_bleak_scanner_passes_timeout() -> None:
    """Duration is passed as timeout to discover()."""
    scanner = BleakScanner(adapter_id="test-adapter")
    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(return_value={})
        mock_cls.return_value = mock_instance
        await scanner.scan(duration=5.0)
        mock_instance.discover.assert_called_once_with(
            timeout=5.0, return_adv=True
        )


@pytest.mark.asyncio
async def test_bleak_scanner_converts_empty_manufacturer_data() -> None:
    """Empty manufacturer data results in empty dict."""
    scanner = BleakScanner(adapter_id="test-adapter")
    device = _make_device("AA:BB:CC:DD:EE:FF", "Test")
    adv_data = _make_adv_data(rssi=-65, manufacturer_data={})

    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(
            return_value={device.address: (device, adv_data)}
        )
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert result[0].manufacturer_data == {}


@pytest.mark.asyncio
async def test_bleak_scanner_converts_empty_service_data() -> None:
    """Empty service data results in empty dict."""
    scanner = BleakScanner(adapter_id="test-adapter")
    device = _make_device("AA:BB:CC:DD:EE:FF", "Test")
    adv_data = _make_adv_data(rssi=-65, service_data={})

    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(
            return_value={device.address: (device, adv_data)}
        )
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert result[0].service_data == {}


@pytest.mark.asyncio
async def test_bleak_scanner_converts_empty_service_uuids() -> None:
    """Empty service UUIDs results in empty tuple."""
    scanner = BleakScanner(adapter_id="test-adapter")
    device = _make_device("AA:BB:CC:DD:EE:FF", "Test")
    adv_data = _make_adv_data(rssi=-65, service_uuids=[])

    with patch("blesentry.scanner.bleak._BleakScanner") as mock_cls:
        mock_instance = MagicMock()
        mock_instance.discover = AsyncMock(
            return_value={device.address: (device, adv_data)}
        )
        mock_cls.return_value = mock_instance
        result = await scanner.scan(duration=1.0)
        assert result[0].service_uuids == ()
