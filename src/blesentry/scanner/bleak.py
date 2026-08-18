# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""BleakScanner adapter — real BLE scanning via bleak.

Implements the Scanner protocol using the bleak library. On macOS,
uses CoreBluetooth (active scanning only — CoreBluetooth limitation).
On Linux, uses BlueZ via D-Bus (passive scanning, which requires
``or_patterns`` — see docs/risks.md).

Error contract (ADR-0002, #63): fail fast. Configuration errors raise
at construction; scan-level failures (D-Bus, adapter loss) propagate
to the caller. A sentinel that cannot scan must never look like a
quiet site. The only sanctioned degradation is per-advertisement:
a single malformed advertisement is logged and skipped.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Sequence

from bleak import BleakScanner as _BleakScanner
from bleak.args.bluez import BlueZScannerArgs, OrPatternLike
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

from blesentry.scanner.models import Advertisement

logger = logging.getLogger(__name__)


class BleakScanner:
    """Real BLE scanner using the bleak library.

    Implements the Scanner protocol. On macOS, uses CoreBluetooth
    (which only supports active scanning). On Linux, uses BlueZ
    via D-Bus with passive scanning.

    Args:
        adapter_id: Semantic label stamped on every ``Advertisement``
            (e.g. ``"bluez-linux"``, ``"macos-corebluetooth"`` — the
            corpus convention from ``scripts/capture_scan.py``). Never
            passed to bleak.
        adapter: BlueZ adapter device name (e.g. ``"hci0"``), or
            ``None`` for the system default. Linux only; ignored on
            macOS.
        or_patterns: BlueZ advertisement-monitor patterns. Required on
            Linux — passive scanning raises without them (docs/
            risks.md) — so construction fails fast when they are
            missing. Ignored on macOS.

    Raises:
        ValueError: on Linux when ``or_patterns`` is missing or empty.
    """

    def __init__(
        self,
        adapter_id: str,
        *,
        adapter: str | None = None,
        or_patterns: Sequence[OrPatternLike] | None = None,
    ) -> None:
        """Validate configuration for this platform and store it."""
        self.adapter_id = adapter_id
        self._adapter = adapter
        self._or_patterns = (
            list(or_patterns) if or_patterns is not None else None
        )
        if sys.platform != "darwin" and not self._or_patterns:
            raise ValueError(
                "BlueZ passive scanning requires or_patterns "
                "(see docs/risks.md); pass or_patterns=[...]"
            )

    async def scan(
        self,
        duration: float,
    ) -> list[Advertisement]:
        """Run a BLE scan for *duration* seconds.

        Platform differences:
        - macOS: active scanning (CoreBluetooth limitation)
        - Linux: passive scanning with BlueZ ``or_patterns``

        Args:
            duration: How long to scan, in seconds.

        Returns:
            Advertised BLE devices heard during the window. A quiet
            window is an empty list; that is a successful scan.

        Raises:
            BleakError, OSError: on scanner or transport failure —
                errors propagate so the caller (P1-8 scan loop) can
                escalate instead of mistaking failure for silence.
        """
        results = await self._run_scan(duration)
        advertisements = []
        for device, adv_data in results.values():
            ad = self._convert_device(device, adv_data)
            if ad is not None:
                advertisements.append(ad)
        return advertisements

    async def _run_scan(
        self,
        duration: float,
    ) -> dict[str, tuple[BLEDevice, AdvertisementData]]:
        """Drive one scan window on a configured scanner instance.

        The only method that touches bleak's runtime; behaviour tests
        mock this seam and contract tests cover what it calls. Never
        use ``_BleakScanner.discover`` here — it is a classmethod that
        builds a fresh default scanner, discarding the configuration
        from ``_create_scanner``.
        """
        scanner = self._create_scanner()
        async with scanner:
            await asyncio.sleep(duration)
        return scanner.discovered_devices_and_advertisement_data

    def _create_scanner(self) -> _BleakScanner:
        """Create a platform-appropriate, fully configured scanner.

        On macOS, CoreBluetooth only supports active scanning. On
        Linux, BlueZ scans passively with ``or_patterns`` on the
        configured adapter.

        Returns:
            Configured BleakScanner instance (not started).
        """
        if sys.platform == "darwin":
            return _BleakScanner(scanning_mode="active")
        if not self._or_patterns:
            raise ValueError("BlueZ passive scanning requires or_patterns")
        bluez_args: BlueZScannerArgs = {"or_patterns": self._or_patterns}
        if self._adapter is not None:
            bluez_args["adapter"] = self._adapter
        return _BleakScanner(scanning_mode="passive", bluez=bluez_args)

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
            Advertisement object, or None if this one advertisement is
            malformed (logged and skipped — data-level degradation
            only; scanner-level failures raise in ``scan``).
        """
        try:
            manufacturer_data: dict[str, str] = {}
            for key, value in adv_data.manufacturer_data.items():
                manufacturer_data[str(key)] = value.hex()

            service_data: dict[str, str] = {}
            for key, value in adv_data.service_data.items():
                service_data[str(key)] = value.hex()

            return Advertisement(
                address=device.address,
                address_type=self._address_type(device),
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

    @staticmethod
    def _address_type(device: BLEDevice) -> str | None:
        """Authoritative address provenance (#56), where the OS has it.

        BlueZ reports ``AddressType`` as ``public`` or ``random`` in
        the Device1 properties bleak carries in ``device.details``;
        for ``random`` the subtype is encoded in the top two bits of
        the most significant octet (11 static, 01 resolvable private,
        00 non-resolvable). CoreBluetooth exposes no address type
        (its 'address' is a host-local UUID) — returns None there.
        ``adv_type`` (the PDU type) is not exposed by either D-Bus or
        CoreBluetooth; the field stays None until a raw-HCI backend.
        """
        details = getattr(device, "details", None)
        if not isinstance(details, dict):
            return None
        props = details.get("props")
        if not isinstance(props, dict):
            return None
        reported = props.get("AddressType")
        if reported != "random":
            return reported
        try:
            top_two = int(device.address.split(":")[0], 16) >> 6
        except (ValueError, IndexError):
            return "random"
        if top_two == 0b11:
            return "random_static"
        if top_two == 0b01:
            return "rpa"
        if top_two == 0b00:
            return "non_resolvable_rpa"
        return "random"
