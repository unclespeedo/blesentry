# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Approach Detector backend (A3 / ADR-0007).

``[detection] backend = "approach"``. I/O-free ``observe`` wraps A2's
tracker and A1's predicate; the cycle consumer enqueues. Fire-once
per address visit; fade-eviction (A2) resets. No raw address on the
event (SECURITY.md).
"""

from __future__ import annotations

from blesentry.detection.approach import (
    APPROACH_DETECTOR_ID,
    APPROACH_KIND,
)
from blesentry.detection.features import proximity_band
from blesentry.detection.models import DetectionEvent, DetectionWindow
from blesentry.detection.trajectory import TrajectoryTracker


def format_approach_alert(event: DetectionEvent) -> str:
    """Plain-text operator line: band + RSSI + rising; never metres.

    Args:
        event: An ``approaching`` event with ``rssi`` and ``band``.

    Returns:
        Snapshot-stable alert text.

    Raises:
        ValueError: ``rssi`` or ``band`` is missing, or ``rising``
            is not ``True``.
    """
    if event.rssi is None or event.band is None or event.rising is not True:
        raise ValueError("approach alert requires rssi, band, and rising")
    return (
        f"Approaching BLE device ({event.band}, "
        f"RSSI {event.rssi} dBm, rising)."
    )


class ApproachDetector:
    """Rising-RSSI Detector: one ``approaching`` event per visit."""

    def __init__(self) -> None:
        """Start with an empty tracker and fire-once set."""
        self._tracker = TrajectoryTracker()
        self._alerted: set[str] = set()

    def observe(self, window: DetectionWindow) -> tuple[DetectionEvent, ...]:
        """Update tracks; emit fire-once approach events.

        Reads ``advertisements`` only (ADR-0007 pre-fusion). ``heard``
        is ignored so a snapshot replay cannot silently look like a
        fixture replay.

        Args:
            window: Clock-free scan window. ``index`` must strictly
                increase (A2 fail-loud).

        Returns:
            Zero or more ``approaching`` events. Empty is success.
        """
        snapshots = self._tracker.observe(window, source="advertisements")
        self._alerted.intersection_update(self._tracker.identities)
        events: list[DetectionEvent] = []
        for row in snapshots:
            if not row.rising or row.identity in self._alerted:
                continue
            self._alerted.add(row.identity)
            events.append(
                DetectionEvent(
                    detector=APPROACH_DETECTOR_ID,
                    kind=APPROACH_KIND,
                    window_index=window.index,
                    rssi=row.max_rssi,
                    band=proximity_band(row.max_rssi),
                    rising=True,
                )
            )
        return tuple(events)
