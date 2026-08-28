# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Detection feature-vector tests (F3 / #122).

Pins the canonical formulas in docs/features.md — band counts, churn,
rolling RSSI slope/span, dwell, first-seen — without a detector, radio,
or outbox.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from blesentry.detection.features import (
    DEFAULT_BANDS,
    DEFAULT_SLOPE_WINDOWS,
    BandCounts,
    BandEdges,
    IdentityFeatures,
    WindowFeatures,
    band_counts,
    extract_features,
    max_rssi_by_identity,
    proximity_band,
    rssi_slope,
    rssi_span,
)
from blesentry.detection.models import DetectionWindow
from blesentry.scanner.models import Advertisement

T0 = 1_700_000_000.0
ADDR_A = "AA:BB:00:00:00:0A"
ADDR_B = "AA:BB:00:00:00:0B"
ADDR_C = "AA:BB:00:00:00:0C"
ADDR_D = "AA:BB:00:00:00:0D"


def _ad(*, address: str, rssi: int, timestamp: float = T0) -> Advertisement:
    return Advertisement(
        address=address,
        rssi=rssi,
        timestamp=timestamp,
        adapter_id="test",
    )


def _window(
    index: int,
    *pairs: tuple[str, int],
) -> DetectionWindow:
    ads = tuple(_ad(address=address, rssi=rssi) for address, rssi in pairs)
    return DetectionWindow(index=index, advertisements=ads)


# --- BandEdges -------------------------------------------------------


def test_default_band_edges_match_detection_plan() -> None:
    assert DEFAULT_BANDS.adjacent == -55
    assert DEFAULT_BANDS.near == -70
    assert DEFAULT_BANDS.far == -80
    assert DEFAULT_SLOPE_WINDOWS == 8


def test_band_edges_require_adjacent_gt_near_gt_far() -> None:
    with pytest.raises(ValidationError):
        BandEdges(adjacent=-80, near=-70, far=-55)
    with pytest.raises(ValidationError):
        BandEdges(adjacent=-55, near=-55, far=-80)


def test_band_edges_are_frozen() -> None:
    frozen_field = "near"
    with pytest.raises(ValidationError):
        setattr(DEFAULT_BANDS, frozen_field, -60)


def test_proximity_band_is_exclusive_and_uses_same_edges() -> None:
    assert proximity_band(-55) == "adjacent"
    assert proximity_band(-56) == "near"
    assert proximity_band(-70) == "near"
    assert proximity_band(-71) == "far"
    assert proximity_band(-80) == "far"
    assert proximity_band(-81) == "beyond-far"
    assert proximity_band(-72) == "far"


# --- max_rssi / band counts ------------------------------------------


def test_max_rssi_takes_loudest_ad_per_address() -> None:
    window = DetectionWindow(
        index=0,
        advertisements=(
            _ad(address=ADDR_A, rssi=-90),
            _ad(address=ADDR_A, rssi=-60),
            _ad(address=ADDR_B, rssi=-75),
        ),
    )
    assert max_rssi_by_identity(window, "advertisements") == {
        ADDR_A: -60,
        ADDR_B: -75,
    }


def test_max_rssi_from_heard_uses_decimal_device_id() -> None:
    window = DetectionWindow(index=0, heard={7: -55, 3: -90})
    assert max_rssi_by_identity(window, "heard") == {"3": -90, "7": -55}


def test_max_rssi_source_ignores_the_other_stream() -> None:
    window = DetectionWindow(
        index=0,
        advertisements=(_ad(address=ADDR_A, rssi=-70),),
        heard={1: -40},
    )
    assert max_rssi_by_identity(window, "advertisements") == {ADDR_A: -70}
    assert max_rssi_by_identity(window, "heard") == {"1": -40}


def test_unknown_source_rejected() -> None:
    with pytest.raises(ValueError, match="source"):
        # Runtime guard; "both" is not a Source union member.
        max_rssi_by_identity(_window(0), "both")  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match="source"):
        extract_features((_window(0),), source="both")  # ty: ignore[invalid-argument-type]


def test_inclusive_nested_band_counts() -> None:
    # -50 adjacent+near+far+all; -65 near+far+all; -75 far+all; -85 all.
    counts = band_counts(
        {ADDR_A: -50, ADDR_B: -65, ADDR_C: -75, ADDR_D: -85},
        DEFAULT_BANDS,
    )
    assert counts.count_all == 4
    assert counts.count_far == 3
    assert counts.count_near == 2
    assert counts.count_adjacent == 1


def test_band_counts_include_exact_edges() -> None:
    counts = band_counts(
        {ADDR_A: -55, ADDR_B: -70, ADDR_C: -80},
        DEFAULT_BANDS,
    )
    assert counts.count_adjacent == 1
    assert counts.count_near == 2
    assert counts.count_far == 3
    assert counts.count_all == 3


def test_band_counts_reject_unnested_values() -> None:
    with pytest.raises(ValidationError, match="nested|adjacent"):
        BandCounts(
            count_all=1,
            count_far=2,
            count_near=0,
            count_adjacent=0,
        )


def test_empty_max_rssi_is_zero_counts() -> None:
    counts = band_counts({}, DEFAULT_BANDS)
    assert counts.count_all == 0
    assert counts.count_far == 0
    assert counts.count_near == 0
    assert counts.count_adjacent == 0


# --- slope / span helpers --------------------------------------------


def test_ols_slope_falling_is_negative() -> None:
    assert rssi_slope(((0, -70), (1, -80), (2, -90))) == -10.0


def test_ols_slope_none_with_fewer_than_two_points() -> None:
    assert rssi_slope(()) is None
    assert rssi_slope(((0, -70),)) is None


def test_ols_slope_none_when_x_variance_is_zero() -> None:
    assert rssi_slope(((3, -90), (3, -70))) is None


def test_span_is_max_minus_min() -> None:
    assert rssi_span((-90, -80, -70)) == 20
    assert rssi_span((-55,)) == 0
    assert rssi_span(()) is None


# --- extract_features: empty / quiet ---------------------------------


def test_empty_windows_yield_empty_tuple() -> None:
    assert extract_features(()) == ()


def test_quiet_window_is_zero_aggregates() -> None:
    features = extract_features((_window(0),))
    assert len(features) == 1
    row = features[0]
    assert row.index == 0
    assert row.source == "advertisements"
    assert row.count_all == 0
    assert row.churn == 0
    assert row.appeared == 0
    assert row.disappeared == 0
    assert row.identities == ()


# --- extract_features: aggregates ------------------------------------


def test_extract_band_counts_and_sorted_identities() -> None:
    window = _window(
        0, (ADDR_D, -85), (ADDR_A, -50), (ADDR_C, -75), (ADDR_B, -65)
    )
    row = extract_features((window,))[0]
    assert row.count_all == 4
    assert row.count_far == 3
    assert row.count_near == 2
    assert row.count_adjacent == 1
    assert [item.identity for item in row.identities] == [
        ADDR_A,
        ADDR_B,
        ADDR_C,
        ADDR_D,
    ]
    assert [item.max_rssi for item in row.identities] == [-50, -65, -75, -85]


def test_churn_appeared_and_disappeared() -> None:
    windows = (
        _window(0, (ADDR_A, -70)),
        _window(1, (ADDR_A, -70), (ADDR_B, -60)),
        _window(2, (ADDR_B, -60)),
        _window(3),
    )
    rows = extract_features(windows)
    assert (rows[0].appeared, rows[0].disappeared, rows[0].churn) == (1, 0, 1)
    assert (rows[1].appeared, rows[1].disappeared, rows[1].churn) == (1, 0, 1)
    assert (rows[2].appeared, rows[2].disappeared, rows[2].churn) == (0, 1, 1)
    assert (rows[3].appeared, rows[3].disappeared, rows[3].churn) == (0, 1, 1)
    assert rows[3].identities == ()
    b_at_one = rows[1].identities[1]
    assert b_at_one.identity == ADDR_B
    assert b_at_one.first_seen_index == 1
    assert b_at_one.age_windows == 1


# --- extract_features: trajectories ----------------------------------


def test_rising_trajectory_slope_span_dwell_first_seen() -> None:
    windows = (
        _window(0, (ADDR_A, -90)),
        _window(1, (ADDR_A, -80)),
        _window(2, (ADDR_A, -70)),
    )
    rows = extract_features(windows)
    last = rows[2].identities[0]
    assert last.identity == ADDR_A
    assert last.max_rssi == -70
    assert last.slope == 10.0
    assert last.span == 20
    assert last.dwell == 3
    assert last.first_seen_index == 0
    assert last.age_windows == 3
    assert last.windows_seen == 3
    assert last.duty == 1.0
    assert rows[0].identities[0].slope is None
    assert rows[0].identities[0].span == 0
    assert rows[0].identities[0].dwell == 1


def test_dwell_resets_after_a_miss_and_duty_counts_gaps() -> None:
    windows = (
        _window(0, (ADDR_A, -70)),
        _window(1),
        _window(2, (ADDR_A, -60)),
    )
    rows = extract_features(windows)
    returned = rows[2].identities[0]
    assert returned.dwell == 1
    assert returned.first_seen_index == 0
    assert returned.age_windows == 3
    assert returned.windows_seen == 2
    assert returned.duty == pytest.approx(2 / 3)
    # Gap is omitted from slope points: (0, -70) and (2, -60).
    assert returned.slope == pytest.approx(5.0)
    assert returned.span == 10


def test_dwell_follows_window_index_not_list_position() -> None:
    # Caller omitted the empty middle window; index still jumps 0 → 2.
    windows = (
        _window(0, (ADDR_A, -70)),
        _window(2, (ADDR_A, -60)),
    )
    returned = extract_features(windows)[1].identities[0]
    assert returned.dwell == 1
    assert returned.windows_seen == 2
    assert returned.age_windows == 3


def test_slope_uses_last_w_heard_samples() -> None:
    # Five heard samples; W=3 so slope is over indexes 2,3,4 at -90,-80,-70.
    windows = tuple(
        _window(i, (ADDR_A, rssi))
        for i, rssi in enumerate((-50, -50, -90, -80, -70))
    )
    last = extract_features(windows, slope_windows=3)[4].identities[0]
    assert last.slope == 10.0
    assert last.span == 20
    assert last.windows_seen == 5
    assert last.dwell == 5


def test_heard_source_keys_by_device_id() -> None:
    windows = (
        DetectionWindow(index=0, heard={7: -90}),
        DetectionWindow(index=1, heard={7: -80}),
        DetectionWindow(index=2, heard={7: -70}),
    )
    rows = extract_features(windows, source="heard")
    assert rows[2].source == "heard"
    ident = rows[2].identities[0]
    assert ident.identity == "7"
    assert ident.slope == 10.0
    assert ident.dwell == 3


def test_heard_churn_and_band_counts_two_devices() -> None:
    windows = (
        DetectionWindow(index=0, heard={3: -70, 7: -55}),
        DetectionWindow(index=1, heard={7: -55}),
    )
    rows = extract_features(windows, source="heard")
    assert rows[0].count_all == 2
    assert rows[0].count_adjacent == 1
    assert rows[0].count_near == 2
    assert (rows[1].appeared, rows[1].disappeared, rows[1].churn) == (0, 1, 1)
    assert [item.identity for item in rows[1].identities] == ["7"]


def test_extract_uses_custom_band_edges() -> None:
    bands = BandEdges(adjacent=-40, near=-50, far=-60)
    window = _window(0, (ADDR_A, -45), (ADDR_B, -70))
    row = extract_features((window,), bands=bands)[0]
    assert row.count_all == 2
    assert row.count_far == 1
    assert row.count_near == 1
    assert row.count_adjacent == 0


def test_identities_keep_independent_trajectories() -> None:
    windows = (
        _window(0, (ADDR_A, -90), (ADDR_B, -50)),
        _window(1, (ADDR_A, -80), (ADDR_B, -50)),
        _window(2, (ADDR_A, -70)),
    )
    rows = extract_features(windows)
    a_last = next(
        item for item in rows[2].identities if item.identity == ADDR_A
    )
    b_mid = next(
        item for item in rows[1].identities if item.identity == ADDR_B
    )
    assert a_last.slope == 10.0
    assert a_last.dwell == 3
    assert b_mid.slope == 0.0
    assert b_mid.dwell == 2
    assert b_mid.span == 0


def test_rejects_bool_slope_windows() -> None:
    with pytest.raises(TypeError, match="slope_windows"):
        extract_features(
            (_window(0),),
            slope_windows=True,
        )


def test_rejects_slope_windows_below_two() -> None:
    with pytest.raises(ValueError, match="slope_windows"):
        extract_features((_window(0, (ADDR_A, -70)),), slope_windows=1)
    with pytest.raises(ValueError, match="slope_windows"):
        extract_features((_window(0),), slope_windows=0)


@pytest.mark.parametrize("slope_windows", [float("inf"), float("nan"), -1.0])
def test_rejects_non_int_or_non_finite_slope_windows(
    slope_windows: float,
) -> None:
    with pytest.raises((ValueError, TypeError)):
        # inf/nan/float must fail at the finite-int gate, not silently coerce.
        extract_features(
            (_window(0),),
            slope_windows=slope_windows,  # ty: ignore[invalid-argument-type]
        )


def test_vectors_are_frozen_and_closed() -> None:
    row = extract_features((_window(0, (ADDR_A, -70)),))[0]
    frozen_field = "churn"
    with pytest.raises(ValidationError):
        setattr(row, frozen_field, 99)
    ident_field = "dwell"
    with pytest.raises(ValidationError):
        setattr(row.identities[0], ident_field, 9)
    counts_field = "count_all"
    with pytest.raises(ValidationError):
        setattr(
            band_counts({ADDR_A: -70}, DEFAULT_BANDS),
            counts_field,
            99,
        )
    with pytest.raises(ValidationError):
        WindowFeatures(
            index=0,
            source="advertisements",
            count_all=0,
            count_far=0,
            count_near=0,
            count_adjacent=0,
            appeared=0,
            disappeared=0,
            churn=0,
            extra=True,
        )
    with pytest.raises(ValidationError):
        IdentityFeatures(
            identity=ADDR_A,
            max_rssi=-70,
            slope=None,
            span=0,
            dwell=1,
            first_seen_index=0,
            age_windows=1,
            windows_seen=1,
            duty=1.0,
            extra=True,
        )


def test_extract_is_deterministic() -> None:
    windows = (
        _window(0, (ADDR_B, -80), (ADDR_A, -70)),
        _window(1, (ADDR_A, -60)),
    )
    assert extract_features(windows) == extract_features(windows)


def test_duty_is_finite_unit_interval() -> None:
    row = extract_features((_window(0, (ADDR_A, -70)),))[0]
    duty = row.identities[0].duty
    assert math.isfinite(duty)
    assert 0.0 < duty <= 1.0
