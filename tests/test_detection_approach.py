# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Approach trigger tests (A1 / #126).

Pins ADR-0007 / docs/approach.md: magnitude rise over W heard
samples, not a Detector backend and not a novelty gate.
"""

from __future__ import annotations

from collections import deque
from inspect import signature

from blesentry.detection.approach import (
    APPROACH_DELTA_DB,
    APPROACH_DETECTOR_ID,
    APPROACH_FAR_PER_DAY,
    APPROACH_FAR_START_DBM,
    APPROACH_KIND,
    APPROACH_MONO_FRACTION,
    APPROACH_PEAK_FLOOR_DBM,
    APPROACH_WINDOWS,
    is_rising_approach,
)
from blesentry.detection.features import (
    DEFAULT_SLOPE_WINDOWS,
    rssi_slope,
    rssi_span,
)


def _points(rssis: list[int], *, start: int = 0) -> list[tuple[int, int]]:
    return [(start + i, rssi) for i, rssi in enumerate(rssis)]


# Motivating walk-by (anonymized): ≈ −99 → −72 over ~2 min = W samples.
WALKBY = [-99, -96, -92, -88, -84, -80, -76, -72]


def test_frozen_knobs_match_adr_0007() -> None:
    assert APPROACH_WINDOWS == 8
    assert APPROACH_DELTA_DB == 18
    assert APPROACH_PEAK_FLOOR_DBM == -75
    assert APPROACH_FAR_START_DBM == -85
    assert APPROACH_KIND == "approaching"
    assert APPROACH_DETECTOR_ID == "approach"
    assert APPROACH_FAR_PER_DAY == 1
    assert APPROACH_MONO_FRACTION == 0.5


def test_trigger_w_coincides_with_f3_eval_default() -> None:
    """Equal today; A1 owns trigger W, F3 owns eval W (docs/approach.md)."""
    assert APPROACH_WINDOWS == 8
    assert DEFAULT_SLOPE_WINDOWS == 8


def test_motivating_walkby_is_an_approach() -> None:
    assert len(WALKBY) == APPROACH_WINDOWS
    assert is_rising_approach(_points(WALKBY))


def test_fewer_than_w_heard_samples_is_not_an_approach() -> None:
    assert is_rising_approach(_points(WALKBY[:-1])) is False
    assert is_rising_approach([]) is False


def test_extra_samples_use_the_last_w() -> None:
    # Prefix is a far stationary linger; the tail is the walk-by.
    linger = [-95] * 4 + WALKBY
    assert is_rising_approach(_points(linger))


def test_span_below_delta_is_not_an_approach() -> None:
    # 17 dB, otherwise a valid far→peak climb ending at the floor.
    rssis = [-92, -91, -90, -89, -88, -87, -77, -75]
    assert rssi_span(rssis) == APPROACH_DELTA_DB - 1
    assert is_rising_approach(_points(rssis)) is False


def test_span_at_delta_with_peak_floor_is_an_approach() -> None:
    rssis = [-93, -91, -89, -87, -85, -83, -77, -75]
    assert rssi_span(rssis) == APPROACH_DELTA_DB
    assert is_rising_approach(_points(rssis))


def test_stationary_rotation_shard_is_not_an_approach() -> None:
    rssis = [-62, -58, -61, -59, -63, -57, -60, -58]
    span = rssi_span(rssis)
    assert span is not None
    assert span < APPROACH_DELTA_DB
    assert is_rising_approach(_points(rssis)) is False


def test_fade_is_not_an_approach() -> None:
    assert is_rising_approach(_points(list(reversed(WALKBY)))) is False


def test_already_near_climb_is_not_an_approach() -> None:
    # Same-room walk: starts above the far-start floor.
    rssis = [-70, -68, -65, -62, -60, -58, -55, -50]
    assert min(rssis) > APPROACH_FAR_START_DBM
    assert is_rising_approach(_points(rssis)) is False


def test_far_start_is_inclusive() -> None:
    # min == −85; span 18 requires terminal ≤ −67.
    rssis = [-85, -83, -81, -79, -77, -74, -71, -67]
    span = rssi_span(rssis)
    assert min(rssis) == APPROACH_FAR_START_DBM
    assert span is not None
    assert span >= APPROACH_DELTA_DB
    assert is_rising_approach(_points(rssis))


def test_terminal_below_peak_floor_is_not_an_approach() -> None:
    # Peaked adjacent in the middle, faded before the last sample.
    rssis = [-99, -90, -80, -70, -55, -70, -80, -90]
    assert max(rssis) >= APPROACH_PEAK_FLOOR_DBM
    assert rssis[-1] < APPROACH_PEAK_FLOOR_DBM
    assert is_rising_approach(_points(rssis)) is False


def test_peak_floor_is_inclusive_on_terminal() -> None:
    rssis = [-93, -91, -89, -87, -85, -83, -77, -75]
    assert rssis[-1] == APPROACH_PEAK_FLOOR_DBM
    assert is_rising_approach(_points(rssis))


def test_mid_window_spike_fails_mostly_monotonic() -> None:
    rssis = [-99, -98, -97, -50, -96, -95, -94, -93]
    span = rssi_span(rssis)
    assert span is not None
    assert (rssis[-1] - rssis[0]) < span * APPROACH_MONO_FRACTION
    assert is_rising_approach(_points(rssis)) is False


def test_net_rise_at_half_span_is_an_approach() -> None:
    # Dip so span is 18 while last-first is exactly half.
    rssis = [-84, -93, -91, -89, -87, -83, -79, -75]
    span = rssi_span(rssis)
    assert span is not None
    assert rssis[-1] - rssis[0] == span * APPROACH_MONO_FRACTION
    assert is_rising_approach(_points(rssis))


def test_ten_db_far_to_floor_is_out_of_class() -> None:
    """Coverage class includes Δ; −85 → −75 (10 dB) is not an approach."""
    rssis = [-85, -84, -83, -82, -81, -80, -77, -75]
    assert rssi_span(rssis) == 10
    assert is_rising_approach(_points(rssis)) is False


def test_positive_slope_is_required() -> None:
    flat = [-90] * APPROACH_WINDOWS
    slope = rssi_slope(_points(flat))
    assert slope == 0.0 or slope is None
    assert is_rising_approach(_points(flat)) is False


def test_gapped_indexes_still_match_walkby() -> None:
    """DC-5: missed windows are omitted; x is window index, not count."""
    points = [
        (0, -99),
        (1, -96),
        (3, -92),
        (4, -88),
        (6, -84),
        (7, -80),
        (9, -76),
        (10, -72),
    ]
    assert len(points) == APPROACH_WINDOWS
    assert is_rising_approach(points)


def test_deque_points_are_accepted() -> None:
    """A2's bounded buffer is a deque; it does not support slicing."""
    buf = deque(maxlen=APPROACH_WINDOWS)
    buf.extend(_points(WALKBY))
    assert is_rising_approach(buf)


def test_predicate_does_not_take_familiarity() -> None:
    """Novelty is not a gate — no familiar/label argument."""
    params = signature(is_rising_approach).parameters
    assert list(params) == ["points"]
