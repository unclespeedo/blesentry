# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Crowd online baseline tests (C3 / #133).

Pins docs/crowd-baseline.md: seasonal vs rolling tiers, floored-MAD
scale, episode freeze, and hold-and-backfill — not a Detector backend.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from blesentry.detection.crowd import (
    CROWD_COLD_START_HOURS,
    CROWD_MAD_FLOOR,
    CROWD_RESIDUAL_WINDOW,
)
from blesentry.detection.crowd_baseline import (
    BaselineStep,
    CrowdBaseline,
    hour_of_week,
)

_T0 = datetime(2026, 1, 5, 12, 0, 0, tzinfo=UTC)  # Monday noon


def _at(offset_hours: float) -> str:
    dt = _T0 + timedelta(hours=offset_hours)
    millis = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


def _run_quiet(
    model: CrowdBaseline,
    *,
    windows: int,
    count: int = 4,
    start_hours: float = 0.0,
    step_hours: float = 15 / 3600,
    wall_clock_trusted: bool = True,
    in_episode: bool = False,
) -> BaselineStep:
    step = BaselineStep(0.0, CROWD_MAD_FLOOR, 0.0, "rolling")
    for i in range(windows):
        step = model.observe(
            count,
            _at(start_hours + i * step_hours),
            wall_clock_trusted=wall_clock_trusted,
            in_episode=in_episode,
        )
    return step


def test_hour_of_week_monday_noon_utc() -> None:
    assert hour_of_week(_at(0)) == 12


def test_hour_of_week_rejects_non_utc() -> None:
    try:
        hour_of_week("2026-01-05T12:00:00+00:00")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_residual_window_matches_ewma_span() -> None:
    assert CROWD_RESIDUAL_WINDOW == 56


def test_rolling_tier_during_cold_start() -> None:
    model = CrowdBaseline()
    step = model.observe(
        4,
        _at(0),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert step.tier == "rolling"


def test_seasonal_tier_after_cold_start() -> None:
    model = CrowdBaseline()
    _run_quiet(model, windows=5, start_hours=0)
    step = _run_quiet(
        model,
        windows=5,
        start_hours=float(CROWD_COLD_START_HOURS),
    )
    assert step.tier == "seasonal"


def test_mad_floor_when_residuals_are_flat() -> None:
    model = CrowdBaseline()
    step = _run_quiet(model, windows=CROWD_RESIDUAL_WINDOW + 5)
    assert step.scale == CROWD_MAD_FLOOR


def test_outlier_spike_produces_large_z() -> None:
    model = CrowdBaseline()
    _run_quiet(model, windows=80, start_hours=float(CROWD_COLD_START_HOURS))
    quiet = model.observe(
        4,
        _at(CROWD_COLD_START_HOURS + 2),
        wall_clock_trusted=True,
        in_episode=False,
    )
    spike = model.observe(
        20,
        _at(CROWD_COLD_START_HOURS + 2.1),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert abs(quiet.z) < 1.0
    assert spike.z > 3.0


def test_outlier_does_not_drag_baseline_to_spike_level() -> None:
    model = CrowdBaseline()
    start = float(CROWD_COLD_START_HOURS)
    _run_quiet(model, windows=100, start_hours=start)
    before = model.observe(
        4,
        _at(start + 2),
        wall_clock_trusted=True,
        in_episode=False,
    ).baseline
    model.observe(
        20,
        _at(start + 2.1),
        wall_clock_trusted=True,
        in_episode=False,
    )
    after = model.observe(
        4,
        _at(start + 2.2),
        wall_clock_trusted=True,
        in_episode=False,
    ).baseline
    assert before == 4.0
    assert after < 6.0


def test_episode_freeze_skips_ewma_update() -> None:
    model = CrowdBaseline()
    start = float(CROWD_COLD_START_HOURS)
    _run_quiet(model, windows=50, start_hours=start)
    frozen = model.observe(
        20,
        _at(start + 1),
        wall_clock_trusted=True,
        in_episode=True,
    )
    thawed = model.observe(
        4,
        _at(start + 1.1),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert frozen.baseline == 4.0
    assert thawed.baseline == 4.0


def test_hold_and_backfill_uses_rolling_until_trusted() -> None:
    model = CrowdBaseline()
    untrusted = model.observe(
        8,
        _at(0),
        wall_clock_trusted=False,
        in_episode=False,
    )
    assert untrusted.tier == "rolling"
    assert untrusted.baseline == 8.0


def test_hold_and_backfill_drains_queue_when_trusted() -> None:
    model = CrowdBaseline()
    start = float(CROWD_COLD_START_HOURS)
    model.observe(6, _at(0), wall_clock_trusted=False, in_episode=False)
    model.observe(6, _at(1), wall_clock_trusted=False, in_episode=False)
    step = model.observe(
        4,
        _at(start),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert step.tier == "seasonal"
    assert step.baseline == 6.0
