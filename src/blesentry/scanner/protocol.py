# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Scanner protocol — the BLE scan seam.

The ``Scanner`` protocol defines the single interface the rest of
``blesentry`` uses to observe BLE advertisements.  Concrete
implementations (BleakScanner for production, MockScanner for tests)
are selected via config (P1-9) and never imported by name outside the
scanner module.

ADR-0002 (extension-point architecture) records this seam.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from blesentry.scanner.models import Advertisement


@runtime_checkable
class Scanner(Protocol):
    """BLE advertisement scanner seam.

    Every scanner backend — bleak, raw HCI, mock — implements this
    protocol.  The rest of the system depends only on this interface;
    swapping implementations is a config change, not a code change.
    """

    async def scan(
        self,
        duration: float,
    ) -> list[Advertisement]:
        """Run a passive BLE scan for *duration* seconds.

        Returns a list of ``Advertisement`` objects observed during the
        scan window.  Implementations may return duplicate MACs (the
        fingerprint/resolver layer handles deduplication).

        Args:
            duration: How long to scan, in seconds.

        Returns:
            Advertised BLE devices heard during the window.
        """
        ...
