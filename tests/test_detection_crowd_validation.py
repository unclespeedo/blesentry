# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Crowd replay validation (C5 / #135).

Asserts recall on positive fixtures and FAR ≤ CROWD_FAR_PER_DAY on
benign corpora with frozen CUSUM / MAD knobs (ADR-0008).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blesentry.detection.crowd import (
    CROWD_CUSUM_H,
    CROWD_CUSUM_K,
    CROWD_FAR_PER_DAY,
    CROWD_MAD_FLOOR,
)
from blesentry.detection.crowd_eval import (
    CROWD_WINDOWS_PER_DAY,
    DEFAULT_QUIET_NEAR,
    CrowdValidationMetrics,
    empty_quiet_day_windows,
    evaluate_benign,
    evaluate_positive,
    merge_metrics,
    steady_quiet_day_windows,
)
from blesentry.detection.replay import (
    detector_for_backend,
    load_heard_fixture,
    replay,
    replay_heard_fixture,
)

REPLAY_DIR = Path(__file__).parent / "fixtures" / "replay"
BUSY_FIXTURE = REPLAY_DIR / "crowd-busy.json"
SPIKE_FIXTURE = REPLAY_DIR / "crowd-spike.json"
BUSY_EVENT_WINDOW_INDEX = 63


def test_frozen_knobs_match_c1_contract() -> None:
    assert CROWD_MAD_FLOOR == 1.5
    assert CROWD_CUSUM_K == 0.5
    assert CROWD_CUSUM_H == 5.0
    assert CROWD_FAR_PER_DAY == 1
    assert CROWD_WINDOWS_PER_DAY == 5760
    assert DEFAULT_QUIET_NEAR == 4


def test_crowd_eval_vacuous_recall() -> None:
    metrics = CrowdValidationMetrics(
        positive_episodes=0,
        positives_detected=0,
        benign_window_count=0,
        false_events=0,
    )
    assert metrics.recall == 1.0
    assert not metrics.meets_c1_targets()


def test_meets_c1_targets_requires_benign_windows() -> None:
    positive_only = CrowdValidationMetrics(1, 1, 0, 0)
    assert not positive_only.meets_c1_targets()


def test_crowd_eval_alerts_per_benign_day_scaling() -> None:
    half_day = CROWD_WINDOWS_PER_DAY // 2
    metrics = CrowdValidationMetrics(
        positive_episodes=0,
        positives_detected=0,
        benign_window_count=half_day,
        false_events=1,
    )
    assert metrics.alerts_per_benign_day == 2.0


def test_merge_metrics_sums_counts() -> None:
    left = CrowdValidationMetrics(1, 1, 100, 0)
    right = CrowdValidationMetrics(0, 0, 200, 1)
    merged = merge_metrics(left, right)
    assert merged.positive_episodes == 1
    assert merged.positives_detected == 1
    assert merged.benign_window_count == 300
    assert merged.false_events == 1


def test_recall_busy_positive() -> None:
    detector = detector_for_backend("crowd")
    windows = load_heard_fixture(BUSY_FIXTURE)
    metrics = evaluate_positive(detector, windows)
    assert metrics.recall == 1.0
    assert metrics.positives_detected == 1


def test_far_spike_zero_events() -> None:
    detector = detector_for_backend("crowd")
    windows = load_heard_fixture(SPIKE_FIXTURE)
    metrics = evaluate_benign(detector, windows)
    assert metrics.false_events == 0


def test_far_quiet_day_zero_events() -> None:
    detector = detector_for_backend("crowd")
    windows = empty_quiet_day_windows()
    assert len(windows) == CROWD_WINDOWS_PER_DAY
    metrics = evaluate_benign(detector, windows)
    assert metrics.false_events == 0
    assert metrics.alerts_per_benign_day == 0.0


def test_far_steady_quiet_day_zero_events() -> None:
    detector = detector_for_backend("crowd")
    windows = steady_quiet_day_windows(near_count=DEFAULT_QUIET_NEAR)
    assert len(windows) == CROWD_WINDOWS_PER_DAY
    metrics = evaluate_benign(detector, windows)
    assert metrics.false_events == 0
    assert metrics.alerts_per_benign_day == 0.0


def test_steady_quiet_day_windows_rejects_bad_near_count() -> None:
    with pytest.raises(TypeError, match="near_count"):
        steady_quiet_day_windows(near_count=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="near_count"):
        steady_quiet_day_windows(near_count=-1)


def test_c5_metrics_meet_c1_targets() -> None:
    busy = evaluate_positive(
        detector_for_backend("crowd"),
        load_heard_fixture(BUSY_FIXTURE),
    )
    benign_parts = [
        evaluate_benign(
            detector_for_backend("crowd"),
            load_heard_fixture(SPIKE_FIXTURE),
        ),
        evaluate_benign(
            detector_for_backend("crowd"),
            empty_quiet_day_windows(),
        ),
        evaluate_benign(
            detector_for_backend("crowd"),
            steady_quiet_day_windows(near_count=DEFAULT_QUIET_NEAR),
        ),
    ]
    combined = merge_metrics(busy, *benign_parts)
    assert combined.recall == 1.0
    for part in benign_parts:
        assert part.meets_benign_far_target()
    assert combined.meets_c1_targets()


def test_replay_harness_busy_cli_equivalent() -> None:
    """Evidence parity with ``blesentry replay --backend crowd``."""
    windows = load_heard_fixture(BUSY_FIXTURE)
    events = replay(detector_for_backend("crowd"), windows)
    assert len(events) == 1
    assert events[0].window_index == BUSY_EVENT_WINDOW_INDEX
    report = replay_heard_fixture(
        BUSY_FIXTURE,
        detector_for_backend("crowd"),
    )
    assert len(report.events) == 1
    assert report.events[0].window_index == BUSY_EVENT_WINDOW_INDEX
