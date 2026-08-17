# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Scanner seam: BLE advertisement value objects and identity keys.

P1-1 ships the ``Advertisement`` and ``Fingerprint`` models consumed by the
rest of the system; the ``Scanner`` protocol and ``MockScanner`` fixture
replay land in P1-2, with the real ``BleakScanner`` adapter following in P1-3.
"""

from blesentry.scanner.bleak import BleakScanner
from blesentry.scanner.mock import MockScanner
from blesentry.scanner.models import Advertisement, Fingerprint
from blesentry.scanner.protocol import Scanner

__all__ = [
    "Advertisement",
    "BleakScanner",
    "Fingerprint",
    "MockScanner",
    "Scanner",
]
