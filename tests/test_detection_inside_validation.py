# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Inside replay validation (I4 / #139).

Asserts recall on positive fixtures and FAR ≤ INSIDE_FAR_PER_DAY on
benign corpora with frozen N=1 / M=8 knobs (ADR-0009).
"""

from __future__ import annotations

from pathlib import Path

from blesentry.detection.familiar import FamiliarSet
from blesentry.detection.inside import (
    INSIDE_FAR_PER_DAY,
    INSIDE_MIN_DEVICES,
    INSIDE_SUSTAIN_WINDOWS,
)
from blesentry.detection.inside_detector import InsideDetector
from blesentry.detection.inside_eval import (
    INSIDE_WINDOWS_PER_DAY,
    InsideValidationMetrics,
    empty_quiet_day_windows,
    evaluate_benign,
    evaluate_positive,
    merge_metrics,
)
from blesentry.detection.models import DetectionWindow
from blesentry.detection.replay import (
    detector_for_backend,
    load_heard_fixture,
    replay,
    replay_heard_fixture,
)

REPLAY_DIR = Path(__file__).parent / "fixtures" / "replay"
DWELL_FIXTURE = REPLAY_DIR / "inside-dwell.json"
TRANSIENT_FIXTURE = REPLAY_DIR / "inside-transient.json"
NEAR_FIXTURE = REPLAY_DIR / "inside-near-not-adjacent.json"
OWN_GEAR_ID = 10
STRANGER_ID = 42


def _adjacent_windows(
    device_id: int,
    rssi: int,
    count: int,
) -> list[DetectionWindow]:
    heard = {device_id: rssi}
    return [
        DetectionWindow(index=index, heard=heard) for index in range(count)
    ]


def test_frozen_knobs_match_i1_contract() -> None:
    assert INSIDE_MIN_DEVICES == 1
    assert INSIDE_SUSTAIN_WINDOWS == 8
    assert INSIDE_FAR_PER_DAY == 1
    assert INSIDE_WINDOWS_PER_DAY == 5760


def test_inside_eval_vacuous_recall() -> None:
    metrics = InsideValidationMetrics(
        positive_episodes=0,
        positives_detected=0,
        benign_window_count=0,
        false_events=0,
    )
    assert metrics.recall == 1.0


def test_inside_eval_alerts_per_benign_day_scaling() -> None:
    half_day = INSIDE_WINDOWS_PER_DAY // 2
    metrics = InsideValidationMetrics(
        positive_episodes=0,
        positives_detected=0,
        benign_window_count=half_day,
        false_events=1,
    )
    assert metrics.alerts_per_benign_day == 2.0


def test_merge_metrics_sums_counts() -> None:
    left = InsideValidationMetrics(1, 1, 100, 0)
    right = InsideValidationMetrics(0, 0, 200, 1)
    merged = merge_metrics(left, right)
    assert merged.positive_episodes == 1
    assert merged.positives_detected == 1
    assert merged.benign_window_count == 300
    assert merged.false_events == 1


def test_recall_dwell_positive() -> None:
    detector = detector_for_backend("inside")
    windows = load_heard_fixture(DWELL_FIXTURE)
    metrics = evaluate_positive(detector, windows)
    assert metrics.recall == 1.0
    assert metrics.positives_detected == 1


def test_far_transient_zero_events() -> None:
    detector = detector_for_backend("inside")
    windows = load_heard_fixture(TRANSIENT_FIXTURE)
    metrics = evaluate_benign(detector, windows)
    assert metrics.false_events == 0


def test_far_quiet_day_zero_events() -> None:
    detector = detector_for_backend("inside")
    windows = empty_quiet_day_windows()
    assert len(windows) == INSIDE_WINDOWS_PER_DAY
    metrics = evaluate_benign(detector, windows)
    assert metrics.false_events == 0
    assert metrics.alerts_per_benign_day == 0.0


def test_far_familiar_own_gear_adjacent_excluded() -> None:
    familiar = FamiliarSet(frozenset({OWN_GEAR_ID}))
    detector = InsideDetector(familiar=familiar)
    metrics = evaluate_benign(
        detector,
        _adjacent_windows(OWN_GEAR_ID, -55, INSIDE_SUSTAIN_WINDOWS + 2),
    )
    assert metrics.false_events == 0


def test_far_own_rotating_gear_adjacent_excluded() -> None:
    detector = InsideDetector(own_rotating_gear=frozenset({OWN_GEAR_ID}))
    metrics = evaluate_benign(
        detector,
        _adjacent_windows(OWN_GEAR_ID, -55, INSIDE_SUSTAIN_WINDOWS + 2),
    )
    assert metrics.false_events == 0


def test_far_seven_adjacent_windows_below_m() -> None:
    detector = detector_for_backend("inside")
    metrics = evaluate_benign(
        detector,
        _adjacent_windows(STRANGER_ID, -55, INSIDE_SUSTAIN_WINDOWS - 1),
    )
    assert metrics.false_events == 0


def test_far_adjacent_threshold_minus_one_dbm() -> None:
    detector = detector_for_backend("inside")
    metrics = evaluate_benign(
        detector,
        _adjacent_windows(STRANGER_ID, -56, INSIDE_SUSTAIN_WINDOWS + 2),
    )
    assert metrics.false_events == 0


def test_far_near_band_not_adjacent() -> None:
    detector = detector_for_backend("inside")
    metrics = evaluate_benign(
        detector,
        load_heard_fixture(NEAR_FIXTURE),
    )
    assert metrics.false_events == 0


def test_i4_metrics_meet_i1_targets() -> None:
    dwell = evaluate_positive(
        detector_for_backend("inside"),
        load_heard_fixture(DWELL_FIXTURE),
    )
    benign_parts = [
        evaluate_benign(
            detector_for_backend("inside"),
            load_heard_fixture(TRANSIENT_FIXTURE),
        ),
        evaluate_benign(
            detector_for_backend("inside"),
            empty_quiet_day_windows(),
        ),
        evaluate_benign(
            InsideDetector(familiar=FamiliarSet(frozenset({OWN_GEAR_ID}))),
            _adjacent_windows(OWN_GEAR_ID, -55, INSIDE_SUSTAIN_WINDOWS + 2),
        ),
        evaluate_benign(
            InsideDetector(own_rotating_gear=frozenset({OWN_GEAR_ID})),
            _adjacent_windows(OWN_GEAR_ID, -55, INSIDE_SUSTAIN_WINDOWS + 2),
        ),
        evaluate_benign(
            detector_for_backend("inside"),
            _adjacent_windows(STRANGER_ID, -55, INSIDE_SUSTAIN_WINDOWS - 1),
        ),
        evaluate_benign(
            detector_for_backend("inside"),
            _adjacent_windows(STRANGER_ID, -56, INSIDE_SUSTAIN_WINDOWS + 2),
        ),
        evaluate_benign(
            detector_for_backend("inside"),
            load_heard_fixture(NEAR_FIXTURE),
        ),
    ]
    combined = merge_metrics(dwell, *benign_parts)
    assert combined.recall == 1.0
    assert combined.alerts_per_benign_day <= INSIDE_FAR_PER_DAY
    assert combined.meets_i1_targets()


def test_replay_harness_dwell_cli_equivalent() -> None:
    """Evidence parity with ``blesentry replay --backend inside``."""
    windows = load_heard_fixture(DWELL_FIXTURE)
    events = replay(detector_for_backend("inside"), windows)
    assert len(events) == 1
    assert events[0].window_index == INSIDE_SUSTAIN_WINDOWS - 1
    report = replay_heard_fixture(
        DWELL_FIXTURE,
        detector_for_backend("inside"),
    )
    assert len(report.events) == 1
