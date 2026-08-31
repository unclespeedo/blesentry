# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Inside Detector backend (I3 / ADR-0009).

``[detection] backend = "inside"``. I/O-free ``observe`` wraps I1
sustain helpers and I2 exclusion; the cycle consumer enqueues.
Fire-once per episode; quiet windows reset. No raw address on the
event (SECURITY.md).
"""

from __future__ import annotations

from collections.abc import Mapping

from blesentry.detection.familiar import FamiliarSet
from blesentry.detection.features import DEFAULT_BANDS, BandEdges
from blesentry.detection.inside import (
    INSIDE_DETECTOR_ID,
    INSIDE_KIND,
    INSIDE_MIN_DEVICES,
    INSIDE_SUSTAIN_WINDOWS,
    build_inside_excluded,
    inside_count,
    inside_sustain_step,
)
from blesentry.detection.models import DetectionEvent, DetectionWindow


def _adjacent_contributors(
    heard: Mapping[int, int],
    *,
    excluded: frozenset[int],
    bands: BandEdges = DEFAULT_BANDS,
) -> tuple[int, ...]:
    threshold = bands.adjacent
    ids = sorted(
        device_id
        for device_id, rssi in heard.items()
        if device_id not in excluded and rssi >= threshold
    )
    return tuple(ids)


def format_inside_alert(event: DetectionEvent) -> str:
    """Plain-text operator line: count + post-resolve roster; no metres.

    Args:
        event: An ``inside-adjacent`` event with ``count`` and
            ``contributors``.

    Returns:
        Snapshot-stable alert text.

    Raises:
        ValueError: ``count`` or ``contributors`` is missing or empty.
    """
    if event.count is None or not event.contributors:
        raise ValueError("inside alert requires count and contributors")
    roster = ", ".join(
        f"device {device_id}" for device_id in event.contributors
    )
    return f"Sustained adjacent-to-Pi: {event.count} device(s) ({roster})."


class InsideDetector:
    """Sustained adjacent-to-Pi detector: one event per dwell episode."""

    def __init__(
        self,
        *,
        familiar: FamiliarSet | None = None,
        own_rotating_gear: frozenset[int] | set[int] = frozenset(),
    ) -> None:
        """Start with empty sustain state and optional exclusion sets."""
        self._familiar = familiar or FamiliarSet()
        self._own_rotating_gear = frozenset(own_rotating_gear)
        self._streak = 0
        self._alerted = False

    def replace_own_rotating_gear(
        self, own_rotating_gear: frozenset[int] | set[int]
    ) -> None:
        """Update rotating own-gear exclusion (daily refresh, I2)."""
        self._own_rotating_gear = frozenset(own_rotating_gear)

    def observe(self, window: DetectionWindow) -> tuple[DetectionEvent, ...]:
        """Update sustain state; emit fire-once inside events.

        Reads ``heard`` only (ADR-0009 post-resolve). ``advertisements``
        is ignored so advertisement-only replay cannot silently drive
        this backend.

        Args:
            window: Clock-free scan window.

        Returns:
            Zero or one ``inside-adjacent`` event. Empty is success.
        """
        heard = window.heard
        excluded = build_inside_excluded(
            heard,
            familiar=self._familiar,
            own_rotating_gear=self._own_rotating_gear,
        )
        count = inside_count(heard, excluded=excluded)
        self._streak, fired = inside_sustain_step(
            self._streak,
            count,
            min_devices=INSIDE_MIN_DEVICES,
            sustain_windows=INSIDE_SUSTAIN_WINDOWS,
        )
        if count < INSIDE_MIN_DEVICES:
            self._alerted = False
        if not fired or self._alerted:
            return ()
        self._alerted = True
        contributors = _adjacent_contributors(heard, excluded=excluded)
        if count != len(contributors):
            raise ValueError("inside count must match contributor roster size")
        return (
            DetectionEvent(
                detector=INSIDE_DETECTOR_ID,
                kind=INSIDE_KIND,
                window_index=window.index,
                count=count,
                contributors=contributors,
            ),
        )
