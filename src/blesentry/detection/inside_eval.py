# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Inside detector replay validation metrics (I4 / ADR-0009).

Pure helpers for recall and false-alarm rate on heard-window corpora.
Consumed by ``tests/test_detection_inside_validation.py``; F5 may
generalize later.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from blesentry.detection.inside import INSIDE_FAR_PER_DAY
from blesentry.detection.models import DetectionWindow
from blesentry.detection.protocol import Detector
from blesentry.detection.replay import DEFAULT_REPLAY_PERIOD, replay

# Default cadence: [scan] window + pause (10 s + 5 s).
INSIDE_WINDOWS_PER_DAY = int(86400 // DEFAULT_REPLAY_PERIOD)


@dataclass(frozen=True, slots=True)
class InsideValidationMetrics:
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
            * INSIDE_WINDOWS_PER_DAY
            / self.benign_window_count
        )

    def meets_i1_targets(self) -> bool:
        """Return True when recall is full and FAR ≤ INSIDE_FAR_PER_DAY."""
        return (
            self.recall >= 1.0
            and self.alerts_per_benign_day <= INSIDE_FAR_PER_DAY
        )


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
        for index in range(INSIDE_WINDOWS_PER_DAY)
    ]


def merge_metrics(*parts: InsideValidationMetrics) -> InsideValidationMetrics:
    """Sum episode and window counts across validation runs."""
    return InsideValidationMetrics(
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
) -> InsideValidationMetrics:
    """Score one positive episode (recall numerator/denominator)."""
    detected = 1 if count_events(detector, windows) == expect_events else 0
    return InsideValidationMetrics(
        positive_episodes=1,
        positives_detected=detected,
        benign_window_count=0,
        false_events=0,
    )


def evaluate_benign(
    detector: Detector,
    windows: Sequence[DetectionWindow],
) -> InsideValidationMetrics:
    """Score one benign corpus slice (false events only)."""
    return InsideValidationMetrics(
        positive_episodes=0,
        positives_detected=0,
        benign_window_count=len(windows),
        false_events=count_events(detector, windows),
    )
