# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Approach alert display-name enrichment (#186).

Runs in ``run_cycle`` after ``Detector.observe`` so backends stay
I/O-free (ADR-0006). Operator label wins over advertisement
``local_name``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from blesentry.detection.approach import APPROACH_DETECTOR_ID, APPROACH_KIND
from blesentry.detection.models import DetectionEvent
from blesentry.scanner.models import Advertisement
from blesentry.storage.repository import DeviceRepository


async def enrich_approach_event(
    event: DetectionEvent,
    *,
    advertisements: Sequence[Advertisement],
    address_to_device_id: Mapping[str, int],
    devices: DeviceRepository,
) -> DetectionEvent:
    """Attach ``device_name`` for approach alerts; strip internal identity.

    Args:
        event: One ``observe`` result (may carry ``approach_identity``).
        advertisements: Pre-fusion ads from the same window.
        address_to_device_id: Addresses resolved this cycle.
        devices: Site-scoped device repo (same cycle connection).

    Returns:
        A copy with ``device_name`` set when resolvable; non-approach
        events are returned unchanged.
    """
    if (
        event.detector != APPROACH_DETECTOR_ID
        or event.kind != APPROACH_KIND
        or event.approach_identity is None
    ):
        return event
    address = event.approach_identity
    device_name: str | None = None
    device_id = address_to_device_id.get(address)
    if device_id is not None:
        row = await devices.get(device_id)
        label = row["label"] if row is not None else None
        if label and label.strip():
            device_name = label
    if device_name is None:
        for ad in advertisements:
            if ad.address == address and ad.local_name:
                device_name = ad.local_name
                break
    return event.model_copy(
        update={"device_name": device_name, "approach_identity": None}
    )
