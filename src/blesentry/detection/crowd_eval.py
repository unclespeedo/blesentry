# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Crowd detector replay validation metrics (C5 / ADR-0008).

Pure helpers for recall and false-alarm rate on heard-window corpora.
Consumed by ``tests/test_detection_crowd_validation.py``; F5 may
generalize later.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from blesentry.detection.crowd import CROWD_FAR_PER_DAY
from blesentry.detection.models import DetectionWindow
from blesentry.detection.protocol import Detector
from blesentry.detection.replay import DEFAULT_REPLAY_PERIOD, replay

# Default cadence: [scan] window + pause (10 s + 5 s).
CROWD_WINDOWS_PER_DAY = int(86400 // DEFAULT_REPLAY_PERIOD)

# Steady quiet near-band count from the anonymized plan baseline.
DEFAULT_QUIET_NEAR = 4
_QUIET_RSSI_DBM = -65


@dataclass(frozen=True, slots=True)
class CrowdValidationMetrics:
    """Recall on positives and alerts/day on benign replay corpora."""

    positive_episodes: int
    positives_detected: int
    benign_window_count: int
    false_events: int

    @property
    def recall(self) -> float:
        """Fraction of positive episodes that fired at least once."""
        if self.positive_episodes == 0:
            return 1.0
        return self.positives_detected / self.positive_episodes

    @property
    def alerts_per_benign_day(self) -> float:
        """False events scaled to a 24 h benign corpus at default cadence."""
        if self.benign_window_count == 0:
            return 0.0
        return (
            self.false_events
            * CROWD_WINDOWS_PER_DAY
            / self.benign_window_count
        )

    def meets_c1_targets(self) -> bool:
        """Return True when recall is full and FAR ≤ CROWD_FAR_PER_DAY.

        Requires at least one scored positive episode and one benign window
        so empty or partial metrics cannot pass vacuously.
        """
        if self.positive_episodes <= 0 or self.benign_window_count <= 0:
            return False
        return (
            self.recall >= 1.0
            and self.alerts_per_benign_day <= CROWD_FAR_PER_DAY
        )

    def meets_benign_far_target(self) -> bool:
        """Return True when this slice alone meets the C1 FAR target."""
        if self.benign_window_count <= 0:
            return False
        return self.alerts_per_benign_day <= CROWD_FAR_PER_DAY


def count_events(
    detector: Detector,
    windows: Sequence[DetectionWindow],
) -> int:
    """Return event count for a heard-window sequence."""
    return len(replay(detector, windows))


def empty_quiet_day_windows() -> list[DetectionWindow]:
    """Build 24 h of empty heard buckets at default cadence."""
    return [
        DetectionWindow(index=index, heard={})
        for index in range(CROWD_WINDOWS_PER_DAY)
    ]


def steady_quiet_day_windows(
    *,
    near_count: int = DEFAULT_QUIET_NEAR,
) -> list[DetectionWindow]:
    """Build 24 h of steady quiet near-band occupancy (plan mean ≈ 4)."""
    if not isinstance(near_count, int) or isinstance(near_count, bool):
        raise TypeError("near_count must be an int")
    if near_count < 0:
        raise ValueError("near_count must be >= 0")
    heard = {offset + 1: _QUIET_RSSI_DBM for offset in range(near_count)}
    return [
        DetectionWindow(index=index, heard=heard)
        for index in range(CROWD_WINDOWS_PER_DAY)
    ]


def merge_metrics(*parts: CrowdValidationMetrics) -> CrowdValidationMetrics:
    """Sum episode and window counts across validation runs."""
    return CrowdValidationMetrics(
        positive_episodes=sum(p.positive_episodes for p in parts),
        positives_detected=sum(p.positives_detected for p in parts),
        benign_window_count=sum(p.benign_window_count for p in parts),
        false_events=sum(p.false_events for p in parts),
    )


def evaluate_positive(
    detector: Detector,
    windows: Sequence[DetectionWindow],
    *,
    expect_events: int = 1,
) -> CrowdValidationMetrics:
    """Score one positive episode (recall numerator/denominator)."""
    detected = 1 if count_events(detector, windows) >= expect_events else 0
    return CrowdValidationMetrics(
        positive_episodes=1,
        positives_detected=detected,
        benign_window_count=0,
        false_events=0,
    )


def evaluate_benign(
    detector: Detector,
    windows: Sequence[DetectionWindow],
) -> CrowdValidationMetrics:
    """Score one benign corpus slice (false events only)."""
    return CrowdValidationMetrics(
        positive_episodes=0,
        positives_detected=0,
        benign_window_count=len(windows),
        false_events=count_events(detector, windows),
    )
