# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Crowd detector spec helpers (C1 / ADR-0008).

Frozen knobs and pure math for band-count baselines + one-sided
CUSUM. C3 owns the online baseline; C4 owns the Detector backend.
Reuses F3 :func:`~blesentry.detection.features.band_counts`.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence

from blesentry.detection.features import DEFAULT_BANDS, BandEdges, band_counts

# Frozen knobs — ADR-0008 / docs/crowd.md. C3/C4 import these;
# do not copy the integers into the baseline or backend modules.
CROWD_DETECTOR_ID = "crowd"
CROWD_KIND = "crowd-busy"
CROWD_FAR_PER_DAY = 1
CROWD_SOURCE = "heard"
CROWD_MAD_FLOOR = 1.5
CROWD_CUSUM_K = 0.5
CROWD_CUSUM_H = 5.0
CROWD_EWMA_SPAN = 56
CROWD_HOUR_OF_WEEK_BUCKETS = 168
# Seven days at the default 15 s cadence (scan.window + scan.pause).
CROWD_ROLLING_WINDOWS = 40320
CROWD_COLD_START_HOURS = 168


def ewma_alpha(span: int) -> float:
    """Return ``2 / (span + 1)`` for an effective EWMA sample count."""
    if not isinstance(span, int) or isinstance(span, bool):
        raise TypeError("span must be int")
    if span < 1:
        raise ValueError("span must be >= 1")
    return 2 / (span + 1)


def crowd_counts(
    heard: Mapping[int, int],
    *,
    bands: BandEdges = DEFAULT_BANDS,
) -> tuple[int, int]:
    """Return ``(count_near, count_all)`` from a post-resolve heard map."""
    keyed = {str(device_id): rssi for device_id, rssi in heard.items()}
    counts = band_counts(keyed, bands)
    return counts.count_near, counts.count_all


def floored_mad(
    values: Sequence[float],
    *,
    floor: float = CROWD_MAD_FLOOR,
) -> float:
    """Median absolute deviation with a count-scale floor (DC-3)."""
    if not values:
        raise ValueError("values must not be empty")
    if not isinstance(floor, (int, float)) or isinstance(floor, bool):
        raise TypeError("floor must be float")
    if not math.isfinite(floor) or floor <= 0:
        raise ValueError("floor must be a finite positive number")
    median = statistics.median(values)
    deviations = [abs(value - median) for value in values]
    mad = statistics.median(deviations)
    return max(mad, float(floor))


def cusum_positive(
    accumulator: float,
    z: float,
    *,
    k: float = CROWD_CUSUM_K,
    h: float = CROWD_CUSUM_H,
) -> tuple[float, bool]:
    """One upper Page-CUSUM step on a standardized excess ``z``.

    Args:
        accumulator: Prior positive CUSUM state (≥ 0).
        z: Standardized count excess ``(count - baseline) / scale``.
        k: Allowance in sigma units (slack before accumulation).
        h: Decision threshold — fire when the new state ≥ ``h``.

    Returns:
        ``(new_accumulator, fired)``. Resets to ``0`` when ``z <= -k``.
    """
    if z <= -k:
        return 0.0, False
    new_state = max(0.0, accumulator + z - k)
    return new_state, new_state >= h
