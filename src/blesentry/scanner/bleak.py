# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""BleakScanner adapter — real BLE scanning via bleak.

Implements the Scanner protocol using the bleak library. On macOS,
uses CoreBluetooth (active scanning only — CoreBluetooth limitation).
On Linux, uses BlueZ via D-Bus (passive scanning with or_patterns).

Both backends are normalized into Advertisement objects.

Log-and-degrade on D-Bus hiccups: errors are logged and result in
empty scan results, never exceptions propagate to callers.
"""

from __future__ import annotations

import logging
import sys
import time

from bleak import BleakScanner as _BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from blesentry.scanner.models import Advertisement

logger = logging.getLogger(__name__)


class BleakScanner:
    """Real BLE scanner using the bleak library.

    Implements the Scanner protocol. On macOS, uses CoreBluetooth
    (which only supports active scanning). On Linux, uses BlueZ
    via D-Bus with passive scanning enabled.

    Args:
        adapter_id: Identifier for the BLE adapter (e.g., "bluez-linux",
            "corebluetooth-macos"). Used to tag Advertisement objects.
            On Linux, also selects the physical adapter (e.g., "hci0").
    """

    def __init__(self, adapter_id: str) -> None:
        """Initialize the BleakScanner.

        Args:
            adapter_id: Unique identifier for this scanner's BLE adapter.
                On Linux, this is passed to BlueZ to select the adapter.
        """
        self.adapter_id = adapter_id

    async def scan(
        self,
        duration: float,
    ) -> list[Advertisement]:
        """Run a BLE scan for *duration* seconds.

        Uses bleak's discover method with return_adv=True to get
        a dict mapping device address to (BLEDevice, AdvertisementData)
        tuples. Converts to Advertisement objects. On any error, logs
        the issue and returns an empty list.

        Platform differences:
        - macOS: active scanning (CoreBluetooth limitation)
        - Linux: passive scanning with BlueZ

        Args:
            duration: How long to scan, in seconds.

        Returns:
            Advertised BLE devices heard during the window.
        """
        try:
            scanner = self._create_scanner()
            devices = await scanner.discover(
                timeout=duration,
                return_adv=True,
            )

            advertisements = []
            for device, adv_data in devices.values():
                ad = self._convert_device(device, adv_data)
                if ad is not None:
                    advertisements.append(ad)

            return advertisements
        except Exception:
            logger.exception("BLE scan failed, degrading to empty result")
            return []

    def _create_scanner(self) -> _BleakScanner:
        """Create a platform-appropriate BleakScanner.

        On macOS, CoreBluetooth only supports active scanning.
        On Linux, BlueZ supports passive scanning with or_patterns.

        Returns:
            Configured BleakScanner instance.
        """
        if sys.platform == "darwin":
            return _BleakScanner(scanning_mode="active")
        else:
            return _BleakScanner(
                scanning_mode="passive",
                bluez={"adapter": self.adapter_id},
            )

    def _convert_device(
        self,
        device: BLEDevice,
        adv_data: AdvertisementData,
    ) -> Advertisement | None:
        """Convert a bleak device and advertisement data to an Advertisement.

        Args:
            device: The BLEDevice discovered by bleak.
            adv_data: The AdvertisementData from the discovery.

        Returns:
            Advertisement object, or None if conversion fails.
        """
        try:
            manufacturer_data: dict[str, str] = {}
            for key, value in adv_data.manufacturer_data.items():
                manufacturer_data[str(key)] = value.hex()

            service_data: dict[str, str] = {}
            for key, value in adv_data.service_data.items():
                service_data[str(key)] = value.hex()

            return Advertisement(
                mac=device.address,
                rssi=adv_data.rssi,
                local_name=adv_data.local_name,
                service_uuids=adv_data.service_uuids,
                manufacturer_data=manufacturer_data,
                service_data=service_data,
                tx_power=adv_data.tx_power,
                timestamp=time.time(),
                adapter_id=self.adapter_id,
            )
        except (AttributeError, ValueError, TypeError):
            logger.warning(
                "Failed to convert device %s, skipping",
                device.address,
                exc_info=True,
            )
            return None
