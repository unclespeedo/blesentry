# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""BlueZ passive-scan advertisement-monitor patterns.

Shared by the CLI (``--or-pattern`` flags) and the config system
(``[scanner] or_patterns``). Lives in the scanner package rather than
the CLI so the config seam does not depend on the argparse frontend to
build a scanner. Imports only lightweight bleak arg types, never the
heavy ``BleakScanner`` backend.
"""

from __future__ import annotations

from bleak.args.bluez import OrPattern
from bleak.assigned_numbers import AdvertisementDataType

# Provisional BlueZ passive-scan patterns: common AD FLAGS values
# (0x01 = flags AD type). Misses flag-less nonconnectable beacons —
# a BlueZ limitation to be characterized on hardware (#68).
DEFAULT_OR_PATTERNS = (
    "0:01:06",
    "0:01:1a",
    "0:01:05",
    "0:01:02",
    "0:01:04",
)


def parse_or_pattern(raw: str) -> OrPattern:
    """Parse ``START:ADTYPE:HEXBYTES`` (e.g. ``0:01:06``) into an OrPattern.

    Raises:
        ValueError: if the string is not three colon-separated fields
            with an integer start, hex AD type, and non-empty even-length
            hex content.
    """
    parts = raw.split(":")
    if len(parts) != 3:
        raise ValueError(f"or-pattern {raw!r} must be START:ADTYPE:HEXBYTES")
    start_raw, ad_type_raw, content_raw = parts
    try:
        start = int(start_raw, 10)
        ad_type = AdvertisementDataType(int(ad_type_raw, 16))
        content = bytes.fromhex(content_raw)
    except ValueError as exc:
        raise ValueError(f"or-pattern {raw!r}: {exc}") from exc
    if not content:
        raise ValueError(f"or-pattern {raw!r} has empty content")
    return OrPattern(start, ad_type, content)
