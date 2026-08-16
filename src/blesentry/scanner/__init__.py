# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Scanner seam: BLE advertisement value objects and identity keys.

P1-1 ships the ``Advertisement`` and ``Fingerprint`` models consumed by the
rest of the system; the ``Scanner`` protocol and its implementations land in
P1-2/P1-3 on the same seam.
"""

from blesentry.scanner.models import Advertisement, Fingerprint

__all__ = ["Advertisement", "Fingerprint"]
