# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""BleakScanner adapter — real BLE scanning via bleak.

Implements the Scanner protocol using the bleak library. On macOS,
uses CoreBluetooth (active scanning). On Linux, uses BlueZ via D-Bus
(passive scanning). Both backends are normalized into Advertisement
objects.

Log-and-degrade on D-Bus hiccups: errors are logged and result in
empty scan results, never exceptions propagate to callers.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from bleak import BleakScanner as _BleakScanner

from blesentry.scanner.models import Advertisement

logger = logging.getLogger(__name__)


class BleakScanner:
    """Real BLE scanner using the bleak library.

    Implements the Scanner protocol. On macOS, uses CoreBluetooth
    (which only supports active scanning). On Linux, uses BlueZ
    via D-Bus for passive scanning.

    Args:
        adapter_id: Identifier for the BLE adapter (e.g., "bluez-linux",
            "corebluetooth-macos"). Used to tag Advertisement objects.
    """

    def __init__(self, adapter_id: str) -> None:
        """Initialize the BleakScanner.

        Args:
            adapter_id: Unique identifier for this scanner's BLE adapter.
        """
        self.adapter_id = adapter_id

    async def scan(
        self,
        duration: float,
    ) -> list[Advertisement]:
        """Run a BLE scan for *duration* seconds.

        Uses bleak's discover method to scan for BLE devices.
        Converts discovered devices to Advertisement objects.
        On any error, logs the issue and returns an empty list.

        Args:
            duration: How long to scan, in seconds.

        Returns:
            Advertised BLE devices heard during the window.
        """
        try:
            scanner = _BleakScanner()
            devices = await scanner.discover(timeout=duration)
            
            advertisements = []
            for device in devices:
                ad = self._convert_device(device)
                if ad is not None:
                    advertisements.append(ad)
            
            return advertisements
        except Exception:
            logger.exception("BLE scan failed, degrading to empty result")
            return []

    def _convert_device(self, device: Any) -> Advertisement | None:
        """Convert a bleak device to an Advertisement.

        Args:
            device: A device discovered by bleak.

        Returns:
            Advertisement object, or None if conversion fails.
        """
        try:
            # Extract manufacturer data (bytes -> hex string)
            manufacturer_data: dict[str, str] = {}
            if hasattr(device, "metadata") and device.metadata:
                raw_mfr = device.metadata.get("manufacturer_data", {})
                for key, value in raw_mfr.items():
                    manufacturer_data[str(key)] = value.hex()

            # Extract service data (bytes -> hex string)
            service_data: dict[str, str] = {}
            if hasattr(device, "metadata") and device.metadata:
                raw_svc = device.metadata.get("service_data", {})
                for key, value in raw_svc.items():
                    service_data[str(key)] = value.hex()

            # Extract service UUIDs
            service_uuids: list[str] = []
            if hasattr(device, "uuids") and device.uuids:
                service_uuids = list(device.uuids)

            # Extract tx_power
            tx_power: int | None = None
            if hasattr(device, "metadata") and device.metadata:
                tx_power = device.metadata.get("tx_power")

            return Advertisement(
                mac=device.address,
                rssi=device.rssi,
                local_name=device.name,
                service_uuids=service_uuids,
                manufacturer_data=manufacturer_data,
                service_data=service_data,
                tx_power=tx_power,
                timestamp=time.time(),
                adapter_id=self.adapter_id,
            )
        except (AttributeError, ValueError, TypeError):
            logger.warning(
                "Failed to convert device %s, skipping",
                getattr(device, "address", "unknown"),
            )
            return None
