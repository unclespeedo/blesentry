# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Crowd online baseline tests (C3 / #133).

Pins docs/crowd-baseline.md: seasonal vs rolling tiers, floored-MAD
scale, episode freeze, and hold-and-backfill — not a Detector backend.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from blesentry.detection.crowd import (
    CROWD_COLD_START_HOURS,
    CROWD_CUSUM_H,
    CROWD_CUSUM_K,
    CROWD_MAD_FLOOR,
    CROWD_RESIDUAL_WINDOW,
    cusum_positive,
    floored_mad,
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
    step = _run_quiet(
        model,
        windows=45,
        start_hours=0,
        step_hours=4.0,
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


def test_episode_freeze_skips_residual_history() -> None:
    model = CrowdBaseline()
    start = float(CROWD_COLD_START_HOURS)
    _run_quiet(model, windows=80, start_hours=start)
    for _ in range(5):
        model.observe(
            20,
            _at(start + 2),
            wall_clock_trusted=True,
            in_episode=True,
        )
    spike = model.observe(
        20,
        _at(start + 2.1),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert spike.z > 3.0


def test_episode_freeze_uses_pre_episode_scale() -> None:
    model = CrowdBaseline()
    start = float(CROWD_COLD_START_HOURS)
    _run_quiet(model, windows=80, start_hours=start)
    pre = model.observe(
        4,
        _at(start + 2),
        wall_clock_trusted=True,
        in_episode=False,
    )
    during = model.observe(
        20,
        _at(start + 2.1),
        wall_clock_trusted=True,
        in_episode=True,
    )
    assert during.scale == pre.scale
    assert during.z > 3.0


def test_seasonal_bucket_falls_back_to_rolling_mean() -> None:
    model = CrowdBaseline()
    start = float(CROWD_COLD_START_HOURS)
    _run_quiet(model, windows=100, start_hours=start, step_hours=2.0)
    spike = model.observe(
        20,
        _at(start + 200),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert spike.tier == "seasonal"
    assert spike.baseline == 4.0
    assert spike.z > 1.0


def test_seasonal_residuals_trained_during_cold_start() -> None:
    model = CrowdBaseline()
    start = float(CROWD_COLD_START_HOURS)
    for i in range(90):
        count = 1 if i % 2 == 0 else 12
        model.observe(
            count,
            _at(i * 2),
            wall_clock_trusted=True,
            in_episode=False,
        )
    seasonal = model.observe(
        4,
        _at(90 * 2),
        wall_clock_trusted=True,
        in_episode=False,
    )
    bucket = hour_of_week(_at(start))
    assert seasonal.tier == "seasonal"
    assert len(model._seasonal_residuals[bucket]) > 1
    assert seasonal.scale >= CROWD_MAD_FLOOR


def test_install_at_ignores_untrusted_clock() -> None:
    model = CrowdBaseline()
    model.observe(
        4,
        _at(500),
        wall_clock_trusted=False,
        in_episode=False,
    )
    step = model.observe(
        4,
        _at(0),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert step.tier == "rolling"
    _run_quiet(
        model,
        windows=50,
        start_hours=0,
        step_hours=float(CROWD_COLD_START_HOURS) / 50,
    )
    later = model.observe(
        4,
        _at(float(CROWD_COLD_START_HOURS)),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert later.tier == "seasonal"


def test_install_at_resets_on_backward_clock_correction() -> None:
    model = CrowdBaseline()
    model.observe(4, _at(200), wall_clock_trusted=True, in_episode=False)
    step = model.observe(
        4,
        _at(0),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert step.tier == "rolling"


def test_forward_clock_jump_does_not_prematurely_enable_seasonal() -> None:
    model = CrowdBaseline()
    model.observe(
        4,
        "1970-01-01T00:00:00.000Z",
        wall_clock_trusted=True,
        in_episode=False,
    )
    step = model.observe(
        4,
        _at(1),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert step.tier == "rolling"


def test_exact_168h_forward_jump_reanchors() -> None:
    model = CrowdBaseline()
    model.observe(4, _at(0), wall_clock_trusted=True, in_episode=False)
    step = model.observe(
        4,
        _at(float(CROWD_COLD_START_HOURS)),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert step.tier == "rolling"


def test_subyear_forward_jump_does_not_prematurely_enable_seasonal() -> None:
    model = CrowdBaseline()
    model.observe(
        4,
        _at(0),
        wall_clock_trusted=True,
        in_episode=False,
    )
    step = model.observe(
        4,
        _at(200),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert step.tier == "rolling"


def test_incremental_forward_correction_does_not_bypass_cold_start() -> None:
    model = CrowdBaseline()
    _run_quiet(model, windows=50, start_hours=0, step_hours=2.0)
    step = model.observe(
        4,
        _at(200),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert step.tier == "rolling"


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
    _run_quiet(
        model,
        windows=45,
        start_hours=0,
        step_hours=4.0,
    )
    hold_at = 45 * 4.0 + 1
    model.observe(
        6,
        _at(hold_at),
        wall_clock_trusted=False,
        in_episode=False,
    )
    step = model.observe(
        20,
        _at(hold_at),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert step.tier == "seasonal"
    assert step.baseline == 6.0
    assert step.z >= 2.0


def test_backfill_waits_until_episode_ends() -> None:
    model = CrowdBaseline()
    _run_quiet(
        model,
        windows=45,
        start_hours=0,
        step_hours=4.0,
    )
    hold_at = 45 * 4.0 + 1
    step = 15 / 3600
    model.observe(
        6,
        _at(hold_at),
        wall_clock_trusted=False,
        in_episode=False,
    )
    during = model.observe(
        20,
        _at(hold_at + step),
        wall_clock_trusted=True,
        in_episode=True,
    )
    assert during.baseline < 5.0
    after = model.observe(
        20,
        _at(hold_at + 2 * step),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert hour_of_week(_at(hold_at)) == hour_of_week(_at(hold_at + 2 * step))
    assert after.baseline == 6.0


def test_backward_trusted_step_reanchors_operating_hours() -> None:
    model = CrowdBaseline()
    _run_quiet(model, windows=26, step_hours=4.0)
    model.observe(4, _at(98), wall_clock_trusted=True, in_episode=False)
    step = model.observe(
        4,
        _at(100),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert step.tier == "rolling"


def test_backfill_drains_during_cold_start() -> None:
    model = CrowdBaseline()
    _run_quiet(model, windows=10, step_hours=1.0)
    bucket = hour_of_week(_at(11))
    model.observe(10, _at(11), wall_clock_trusted=False, in_episode=False)
    model.observe(4, _at(12), wall_clock_trusted=True, in_episode=False)
    assert not math.isnan(model._seasonal[bucket])
    assert len(model._backfill) == 0


def test_episode_pins_tier_across_cold_start_boundary() -> None:
    model = CrowdBaseline()
    step_hours = 4.0
    _run_quiet(model, windows=42, step_hours=step_hours)
    during = model.observe(
        4,
        _at(42 * step_hours),
        wall_clock_trusted=True,
        in_episode=True,
    )
    assert during.tier == "rolling"
    still = model.observe(
        4,
        _at(42 * step_hours + 4),
        wall_clock_trusted=True,
        in_episode=True,
    )
    assert still.tier == "rolling"
    after = model.observe(
        4,
        _at(42 * step_hours + 8),
        wall_clock_trusted=True,
        in_episode=False,
    )
    assert after.tier == "seasonal"


def test_episode_trigger_window_frozen_via_preview_commit() -> None:
    model = CrowdBaseline()
    _run_quiet(model, windows=45, step_hours=4.0)
    model.begin_window(
        _at(200),
        wall_clock_trusted=True,
        in_episode=False,
    )
    quiet = model.preview(
        4,
        _at(200),
        wall_clock_trusted=True,
        in_episode=False,
    )
    model.commit(
        4,
        _at(200),
        wall_clock_trusted=True,
        in_episode=False,
        tier=quiet.tier,
    )
    model.begin_window(
        _at(201),
        wall_clock_trusted=True,
        in_episode=False,
    )
    spike = model.preview(
        40,
        _at(201),
        wall_clock_trusted=True,
        in_episode=False,
    )
    s_new, fired = cusum_positive(
        0.0,
        spike.z,
        k=CROWD_CUSUM_K,
        h=CROWD_CUSUM_H,
    )
    assert s_new > 0
    assert fired
    model.commit(
        40,
        _at(201),
        wall_clock_trusted=True,
        in_episode=s_new > 0,
        tier=spike.tier,
    )
    model.begin_window(
        _at(202),
        wall_clock_trusted=True,
        in_episode=True,
    )
    after = model.preview(
        4,
        _at(202),
        wall_clock_trusted=True,
        in_episode=True,
    )
    assert after.baseline == quiet.baseline


def test_trigger_tier_pins_rolling_at_cold_start_boundary() -> None:
    model = CrowdBaseline()
    step_hours = 4.0
    _run_quiet(model, windows=42, step_hours=step_hours)
    at = _at(42 * step_hours)
    model.begin_window(at, wall_clock_trusted=True, in_episode=False)
    step = model.preview(40, at, wall_clock_trusted=True, in_episode=False)
    assert step.tier == "rolling"
    s_new, _ = cusum_positive(
        0.0,
        step.z,
        k=CROWD_CUSUM_K,
        h=CROWD_CUSUM_H,
    )
    model.commit(
        40,
        at,
        wall_clock_trusted=True,
        in_episode=s_new > 0,
        tier=step.tier,
    )
    model.begin_window(
        _at(42 * step_hours + step_hours),
        wall_clock_trusted=True,
        in_episode=True,
    )
    during = model.preview(
        40,
        _at(42 * step_hours + step_hours),
        wall_clock_trusted=True,
        in_episode=True,
    )
    assert during.tier == "rolling"


def test_seasonal_scale_falls_back_to_rolling_for_unvisited_bucket() -> None:
    model = CrowdBaseline()
    for i in range(45):
        model.observe(
            4 + (i % 3),
            _at(i * 4.0),
            wall_clock_trusted=True,
            in_episode=False,
        )
    at = _at(45 * 4.0)
    for offset in range(1, 24):
        candidate = _at(45 * 4.0 + offset)
        if math.isnan(model._seasonal[hour_of_week(candidate)]):
            at = candidate
            break
    bucket = hour_of_week(at)
    assert math.isnan(model._seasonal[bucket])
    model.begin_window(at, wall_clock_trusted=True, in_episode=False)
    step = model.preview(20, at, wall_clock_trusted=True, in_episode=False)
    assert step.tier == "seasonal"
    residual = 20.0 - step.baseline
    expected = floored_mad([*model._rolling_residuals, residual])
    assert step.scale == expected


def test_seasonal_first_visit_residual_is_zero() -> None:
    model = CrowdBaseline()
    for i in range(45):
        model.observe(
            4 + (i % 3),
            _at(i * 4.0),
            wall_clock_trusted=True,
            in_episode=False,
        )
    at = _at(45 * 4.0)
    for offset in range(1, 24):
        candidate = _at(45 * 4.0 + offset)
        if math.isnan(model._seasonal[hour_of_week(candidate)]):
            at = candidate
            break
    bucket = hour_of_week(at)
    model.observe(20, at, wall_clock_trusted=True, in_episode=False)
    assert model._seasonal_residuals[bucket][0] == 0.0
    model.observe(22, at, wall_clock_trusted=True, in_episode=False)
    assert model._seasonal_residuals[bucket][-1] != 0.0


def test_reanchor_discards_queued_backfill() -> None:
    model = CrowdBaseline()
    _run_quiet(model, windows=10, step_hours=1.0)
    model.observe(10, _at(20), wall_clock_trusted=False, in_episode=False)
    assert len(model._backfill) == 1
    model.observe(4, _at(0), wall_clock_trusted=True, in_episode=False)
    assert len(model._backfill) == 0


def test_reanchor_skips_backfill_drain_before_seasonal_update() -> None:
    model = CrowdBaseline()
    _run_quiet(model, windows=45, step_hours=4.0)
    bucket = hour_of_week(_at(50))
    assert math.isnan(model._seasonal[bucket])
    model.observe(99, _at(50), wall_clock_trusted=False, in_episode=False)
    model.observe(4, _at(0), wall_clock_trusted=True, in_episode=False)
    assert math.isnan(model._seasonal[bucket])
    assert len(model._seasonal_residuals[bucket]) == 0
