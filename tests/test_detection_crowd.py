# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Crowd detector spec tests (C1 / #131).

Pins ADR-0008 / docs/crowd.md: band-count features, floored-MAD
scale, and one-sided CUSUM helpers — not a Detector backend yet.
"""

from __future__ import annotations

from blesentry.detection.crowd import (
    CROWD_COLD_START_HOURS,
    CROWD_CUSUM_H,
    CROWD_CUSUM_K,
    CROWD_DETECTOR_ID,
    CROWD_EWMA_SPAN,
    CROWD_FAR_PER_DAY,
    CROWD_HOUR_OF_WEEK_BUCKETS,
    CROWD_KIND,
    CROWD_MAD_FLOOR,
    CROWD_ROLLING_WINDOWS,
    CROWD_SOURCE,
    crowd_counts,
    cusum_positive,
    ewma_alpha,
    floored_mad,
)
from blesentry.detection.features import DEFAULT_BANDS


def test_frozen_knobs_match_adr_0008() -> None:
    assert CROWD_DETECTOR_ID == "crowd"
    assert CROWD_KIND == "crowd-busy"
    assert CROWD_FAR_PER_DAY == 1
    assert CROWD_SOURCE == "heard"
    assert CROWD_MAD_FLOOR == 1.5
    assert CROWD_CUSUM_K == 0.5
    assert CROWD_CUSUM_H == 5.0
    assert CROWD_EWMA_SPAN == 56
    assert CROWD_HOUR_OF_WEEK_BUCKETS == 168
    assert CROWD_ROLLING_WINDOWS == 40320
    assert CROWD_COLD_START_HOURS == 168


def test_ewma_alpha_from_span() -> None:
    assert ewma_alpha(CROWD_EWMA_SPAN) == 2 / 57


def test_crowd_counts_from_heard_near_and_all() -> None:
    heard = {1: -65, 2: -68, 3: -85, 4: -55}
    near, total = crowd_counts(heard)
    assert total == 4
    assert near == 3
    assert heard[3] < DEFAULT_BANDS.near


def test_crowd_counts_empty_heard() -> None:
    assert crowd_counts({}) == (0, 0)


def test_floored_mad_at_zero_floor() -> None:
    assert floored_mad([4.0, 4.0, 4.0, 4.0]) == CROWD_MAD_FLOOR


def test_floored_mad_uses_sample_mad_when_above_floor() -> None:
    values = [1.0, 3.0, 5.0, 7.0, 9.0]
    assert floored_mad(values) == 2.0


def test_floored_mad_rejects_empty() -> None:
    try:
        floored_mad([])
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_cusum_positive_accumulates_sustained_excess() -> None:
    state = 0.0
    fired = False
    for _ in range(3):
        state, fired = cusum_positive(
            state,
            2.0,
            k=CROWD_CUSUM_K,
            h=CROWD_CUSUM_H,
        )
    assert fired is False
    assert state == 4.5


def test_cusum_positive_fires_at_threshold() -> None:
    state = 4.5
    state, fired = cusum_positive(
        state,
        2.0,
        k=CROWD_CUSUM_K,
        h=CROWD_CUSUM_H,
    )
    assert fired is True
    assert state == 6.0


def test_cusum_positive_resets_on_deep_negative_excess() -> None:
    state = 3.0
    state, fired = cusum_positive(
        state,
        -2.0,
        k=CROWD_CUSUM_K,
        h=CROWD_CUSUM_H,
    )
    assert fired is False
    assert state == 0.0


def test_single_window_spike_does_not_fire() -> None:
    state, fired = cusum_positive(
        0.0,
        4.0,
        k=CROWD_CUSUM_K,
        h=CROWD_CUSUM_H,
    )
    assert fired is False
    assert state == 3.5
