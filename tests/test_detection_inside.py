# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Inside detector spec tests (I1 / #136).

Pins ADR-0009 / docs/inside.md: adjacent band count, exclusion
contract, and consecutive-window sustain helpers — not a Detector
backend yet.
"""

from __future__ import annotations

from blesentry.detection.features import DEFAULT_BANDS
from blesentry.detection.inside import (
    INSIDE_DETECTOR_ID,
    INSIDE_FAR_PER_DAY,
    INSIDE_KIND,
    INSIDE_MIN_DEVICES,
    INSIDE_SOURCE,
    INSIDE_SUSTAIN_WINDOWS,
    inside_count,
    inside_sustain_step,
)


def test_frozen_knobs_match_adr_0009() -> None:
    assert INSIDE_DETECTOR_ID == "inside"
    assert INSIDE_KIND == "inside-adjacent"
    assert INSIDE_FAR_PER_DAY == 1
    assert INSIDE_SOURCE == "heard"
    assert INSIDE_MIN_DEVICES == 1
    assert INSIDE_SUSTAIN_WINDOWS == 8


def test_inside_count_adjacent_from_heard() -> None:
    heard = {1: -55, 2: -65, 3: -85}
    assert inside_count(heard) == 1
    assert heard[2] < DEFAULT_BANDS.adjacent


def test_inside_count_empty_heard() -> None:
    assert inside_count({}) == 0


def test_inside_count_excludes_device_ids() -> None:
    heard = {1: -55, 2: -54, 3: -55}
    assert inside_count(heard, excluded=frozenset({1})) == 2


def test_inside_sustain_step_accumulates_consecutive_windows() -> None:
    streak = 0
    fired = False
    for _ in range(INSIDE_SUSTAIN_WINDOWS - 1):
        streak, fired = inside_sustain_step(streak, 1)
    assert fired is False
    assert streak == INSIDE_SUSTAIN_WINDOWS - 1


def test_inside_sustain_step_fires_at_threshold() -> None:
    streak = INSIDE_SUSTAIN_WINDOWS - 1
    streak, fired = inside_sustain_step(streak, 1)
    assert fired is True
    assert streak == INSIDE_SUSTAIN_WINDOWS


def test_inside_sustain_step_resets_on_quiet_window() -> None:
    streak = INSIDE_SUSTAIN_WINDOWS - 1
    streak, fired = inside_sustain_step(streak, 0)
    assert fired is False
    assert streak == 0


def test_inside_sustain_step_requires_min_devices() -> None:
    streak = 3
    streak, fired = inside_sustain_step(streak, 0, min_devices=2)
    assert fired is False
    assert streak == 0
