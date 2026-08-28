# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Approach trigger predicate (A1 / ADR-0007).

Stateless 'do these last-W heard samples look like an approach?'
A3's ``ApproachDetector`` is the ``[detection] backend = "approach"``
union member and calls :func:`is_rising_approach` from ``observe``.
A2 feeds it a bounded deque. Reuses F3 ``rssi_slope`` / ``rssi_span``.
"""

from __future__ import annotations

from collections.abc import Sequence

from blesentry.detection.features import rssi_slope, rssi_span

# Frozen knobs — ADR-0007 / docs/approach.md. A2/A3 import these;
# do not copy the integers into the tracker or the backend.
APPROACH_WINDOWS = 8
APPROACH_DELTA_DB = 18
APPROACH_PEAK_FLOOR_DBM = -75
APPROACH_FAR_START_DBM = -85
APPROACH_KIND = "approaching"
APPROACH_DETECTOR_ID = "approach"
APPROACH_FAR_PER_DAY = 1
APPROACH_MONO_FRACTION = 0.5


def is_rising_approach(points: Sequence[tuple[int, int]]) -> bool:
    """Return True if ``points`` match the A1 magnitude trigger.

    Args:
        points: Heard samples ``(window_index, rssi)`` oldest-first.
            Missed windows are already omitted (DC-5). Extra samples
            beyond W are truncated to the last W (F3 rolling window).

    Returns:
        Whether the last W samples satisfy W/Δ/peak/far-start/slope.
        Fewer than W samples is ``False``, not an error.
    """
    samples = list(points)
    if len(samples) < APPROACH_WINDOWS:
        return False
    rolling = samples[-APPROACH_WINDOWS:]
    rssis = [rssi for _index, rssi in rolling]
    span = rssi_span(rssis)
    slope = rssi_slope(rolling)
    if span is None or slope is None:
        return False
    return (
        span >= APPROACH_DELTA_DB
        and slope > 0
        and max(rssis) >= APPROACH_PEAK_FLOOR_DBM
        and rssis[-1] >= APPROACH_PEAK_FLOOR_DBM
        and min(rssis) <= APPROACH_FAR_START_DBM
        and (rssis[-1] - rssis[0]) >= span * APPROACH_MONO_FRACTION
    )
