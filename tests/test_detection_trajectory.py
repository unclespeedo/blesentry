# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Online per-address trajectory tracker tests (A2 / #127).

Pins docs/approach.md: bounded deque, F3 slope/span/dwell, A1
rising predicate, hard cap + evict-on-fade. Not a Detector backend.
"""

from __future__ import annotations

import ast
from inspect import signature
from pathlib import Path

import pytest
from pydantic import ValidationError

from blesentry.detection import trajectory as trajectory_mod
from blesentry.detection.approach import (
    APPROACH_WINDOWS,
    is_rising_approach,
)
from blesentry.detection.features import rssi_slope, rssi_span
from blesentry.detection.models import DetectionEvent, DetectionWindow
from blesentry.detection.trajectory import (
    TRACKER_FADE_AFTER_WINDOWS,
    TRACKER_MAX_ADDRESSES,
    TRACKER_MAX_SAMPLES,
    AddressTrajectory,
    TrajectoryTracker,
)
from blesentry.scanner.models import Advertisement

T0 = 1_700_000_000.0
ADDR_A = "AA:BB:00:00:00:0A"
ADDR_B = "AA:BB:00:00:00:0B"
ADDR_C = "AA:BB:00:00:00:0C"

# Motivating walk-by (anonymized): ≈ −99 → −72 over W samples.
WALKBY = [-99, -96, -92, -88, -84, -80, -76, -72]


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


def _feed(
    tracker: TrajectoryTracker,
    identity: str,
    rssis: list[int],
    *,
    start: int = 0,
) -> AddressTrajectory:
    row = None
    for offset, rssi in enumerate(rssis):
        rows = tracker.observe(_window(start + offset, (identity, rssi)))
        assert len(rows) == 1
        row = rows[0]
    assert row is not None
    return row


def test_tracker_does_not_import_band_counts() -> None:
    assert "band_counts" not in trajectory_mod.__dict__
    path = trajectory_mod.__file__
    assert path is not None
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ImportFrom, ast.Import)):
            imported.update(alias.name for alias in node.names)
    assert "band_counts" not in imported


def test_stated_memory_knobs_match_docs() -> None:
    assert TRACKER_MAX_ADDRESSES == 256
    assert TRACKER_FADE_AFTER_WINDOWS == 12
    assert TRACKER_MAX_SAMPLES == TRACKER_MAX_ADDRESSES * APPROACH_WINDOWS
    assert TRACKER_MAX_SAMPLES == 2048


def test_deque_maxlen_is_a1_w_not_a_copied_integer() -> None:
    tracker = TrajectoryTracker()
    assert tracker.sample_windows == APPROACH_WINDOWS


def test_walkby_rise_is_rising_on_the_last_window() -> None:
    tracker = TrajectoryTracker()
    last = _feed(tracker, ADDR_A, WALKBY)
    assert last.rising is True
    assert last.identity == ADDR_A
    assert last.max_rssi == WALKBY[-1]
    assert last.dwell == APPROACH_WINDOWS
    assert last.windows_seen == APPROACH_WINDOWS
    assert last.visit_min == min(WALKBY)
    assert last.span == rssi_span(WALKBY)
    assert last.slope == rssi_slope(
        [(i, rssi) for i, rssi in enumerate(WALKBY)]
    )
    assert is_rising_approach([(i, rssi) for i, rssi in enumerate(WALKBY)])


def test_fall_is_not_rising() -> None:
    tracker = TrajectoryTracker()
    last = _feed(tracker, ADDR_A, list(reversed(WALKBY)))
    assert last.rising is False
    assert last.span == rssi_span(list(reversed(WALKBY)))


def test_fewer_than_w_samples_is_not_rising() -> None:
    tracker = TrajectoryTracker()
    last = _feed(tracker, ADDR_A, WALKBY[:-1])
    assert last.rising is False
    assert last.windows_seen == APPROACH_WINDOWS - 1


def test_visit_min_survives_deque_truncation() -> None:
    """Last-W far-start is last-W; visit_min keeps the aged-out floor."""
    tracker = TrajectoryTracker()
    linger = [-99] * 4 + WALKBY
    last = _feed(tracker, ADDR_A, linger)
    assert last.windows_seen == len(linger)
    assert last.visit_min == -99
    # Truncated tail is the walk-by; predicate still matches.
    assert last.rising is True
    assert last.span == rssi_span(WALKBY)


def test_truncated_span_ignores_aged_loud_prefix() -> None:
    """A loud prefix would inflate span if the deque kept it."""
    tracker = TrajectoryTracker()
    prefix = [-50]
    last = _feed(tracker, ADDR_A, prefix + WALKBY)
    kept = rssi_span(WALKBY)
    with_prefix = rssi_span(prefix + WALKBY)
    assert kept is not None
    assert with_prefix is not None
    assert with_prefix != kept
    assert last.span == kept
    assert last.visit_min == min(prefix + WALKBY)


def test_rising_stays_true_on_later_windows_of_same_visit() -> None:
    """Fire-once is A3; the tracker keeps reporting the climb."""
    tracker = TrajectoryTracker()
    last = _feed(tracker, ADDR_A, WALKBY)
    assert last.rising is True
    # Two more terminal samples: last-W still satisfies A1.
    for offset in (1, 2):
        follow = tracker.observe(
            _window(len(WALKBY) + offset - 1, (ADDR_A, WALKBY[-1]))
        )
        assert follow[0].rising is True
        assert follow[0].windows_seen == len(WALKBY) + offset


def test_dwell_resets_after_a_missed_index() -> None:
    tracker = TrajectoryTracker()
    tracker.observe(_window(0, (ADDR_A, -90)))
    tracker.observe(_window(1, (ADDR_A, -88)))
    rows = tracker.observe(_window(3, (ADDR_A, -86)))
    assert rows[0].dwell == 1
    assert rows[0].windows_seen == 3
    assert rows[0].age_windows == 4


def test_loudest_advertisement_wins_this_window() -> None:
    tracker = TrajectoryTracker()
    window = DetectionWindow(
        index=0,
        advertisements=(
            _ad(address=ADDR_A, rssi=-90),
            _ad(address=ADDR_A, rssi=-60),
        ),
    )
    rows = tracker.observe(window)
    assert rows[0].max_rssi == -60
    assert rows[0].visit_min == -60


def test_heard_source_uses_device_id_strings() -> None:
    tracker = TrajectoryTracker()
    window = DetectionWindow(index=0, heard={7: -55})
    rows = tracker.observe(window, source="heard")
    assert rows[0].identity == "7"
    assert rows[0].max_rssi == -55


def test_quiet_window_returns_empty_and_fades() -> None:
    tracker = TrajectoryTracker(fade_after_windows=2)
    tracker.observe(_window(0, (ADDR_A, -90)))
    assert tracker.tracked_count == 1
    assert tracker.observe(_window(1)) == ()
    assert tracker.tracked_count == 1
    assert tracker.observe(_window(2)) == ()
    assert tracker.tracked_count == 0


def test_return_after_fade_is_a_new_visit() -> None:
    tracker = TrajectoryTracker(fade_after_windows=2)
    tracker.observe(_window(0, (ADDR_A, -50)))
    tracker.observe(_window(1))
    tracker.observe(_window(2))
    rows = tracker.observe(_window(3, (ADDR_A, -90)))
    assert rows[0].visit_min == -90
    assert rows[0].windows_seen == 1
    assert rows[0].first_seen_index == 3


def test_lru_evicts_oldest_unheard_when_cap_binds() -> None:
    tracker = TrajectoryTracker(max_addresses=2, fade_after_windows=100)
    tracker.observe(_window(0, (ADDR_A, -90), (ADDR_B, -80)))
    rows = tracker.observe(_window(1, (ADDR_C, -70)))
    assert {row.identity for row in rows} == {ADDR_C}
    assert tracker.tracked_count == 2
    # ADDR_A was inserted first at the same last_heard as B; LRU
    # drops the oldest-then-identity victim (A). B remains.
    still = tracker.observe(_window(2, (ADDR_B, -80)))
    assert {row.identity for row in still} == {ADDR_B}
    assert tracker.tracked_count == 2


def test_single_window_keeps_strongest_newcomers_when_over_cap() -> None:
    tracker = TrajectoryTracker(max_addresses=2, fade_after_windows=100)
    rows = tracker.observe(
        _window(0, (ADDR_A, -90), (ADDR_B, -80), (ADDR_C, -70))
    )
    assert {row.identity for row in rows} == {ADDR_B, ADDR_C}
    assert tracker.tracked_count == 2
    assert tracker.sample_count == 2


def test_all_heard_full_cap_rejects_weakest_newcomer() -> None:
    """No LRU victims when everyone is heard; room == 0 drops C."""
    tracker = TrajectoryTracker(max_addresses=2, fade_after_windows=100)
    tracker.observe(_window(0, (ADDR_A, -90), (ADDR_B, -80)))
    rows = tracker.observe(
        _window(1, (ADDR_A, -90), (ADDR_B, -80), (ADDR_C, -99))
    )
    assert {row.identity for row in rows} == {ADDR_A, ADDR_B}
    assert tracker.tracked_count == 2
    assert ADDR_C not in tracker.identities


def test_established_track_survives_stronger_newcomer_flood() -> None:
    tracker = TrajectoryTracker(max_addresses=2, fade_after_windows=100)
    tracker.observe(_window(0, (ADDR_A, -95)))
    tracker.observe(_window(1, (ADDR_A, -95)))
    rows = tracker.observe(
        _window(2, (ADDR_A, -95), (ADDR_B, -50), (ADDR_C, -51))
    )
    assert ADDR_A in {row.identity for row in rows}
    assert tracker.tracked_count == 2
    follow = tracker.observe(_window(3, (ADDR_A, -90)))
    assert follow[0].windows_seen == 4
    assert follow[0].visit_min == -95


def test_rpa_churn_respects_stated_memory_cap() -> None:
    tracker = TrajectoryTracker()
    minted = 0
    peak_tracked = 0
    peak_samples = 0
    for index in range(40):
        pairs = tuple((f"rpa-{minted + i:05d}", -90) for i in range(30))
        minted += 30
        tracker.observe(_window(index, *pairs))
        assert tracker.tracked_count <= TRACKER_MAX_ADDRESSES
        assert tracker.sample_count <= TRACKER_MAX_SAMPLES
        peak_tracked = max(peak_tracked, tracker.tracked_count)
        peak_samples = max(peak_samples, tracker.sample_count)
    assert minted > TRACKER_MAX_ADDRESSES * 2
    assert peak_tracked == TRACKER_MAX_ADDRESSES
    assert peak_samples <= TRACKER_MAX_SAMPLES
    assert tracker.tracked_count == TRACKER_MAX_ADDRESSES


def test_full_deques_hit_but_do_not_exceed_sample_cap() -> None:
    tracker = TrajectoryTracker()
    identities = tuple(f"fix-{i:03d}" for i in range(TRACKER_MAX_ADDRESSES))
    pairs = tuple((identity, -90) for identity in identities)
    for index in range(APPROACH_WINDOWS):
        tracker.observe(_window(index, *pairs))
        assert tracker.sample_count <= TRACKER_MAX_SAMPLES
    assert tracker.tracked_count == TRACKER_MAX_ADDRESSES
    assert tracker.sample_count == TRACKER_MAX_SAMPLES
    tracker.observe(_window(APPROACH_WINDOWS, *pairs))
    assert tracker.sample_count == TRACKER_MAX_SAMPLES


def test_unknown_source_does_not_consume_index() -> None:
    tracker = TrajectoryTracker()
    with pytest.raises(ValueError, match="source"):
        tracker.observe(
            _window(0, (ADDR_A, -90)),
            # Runtime unknown-source path; "both" is not a Source member.
            source="both",  # ty: ignore[invalid-argument-type]
        )
    rows = tracker.observe(_window(0, (ADDR_A, -90)))
    assert rows[0].identity == ADDR_A
    assert rows[0].windows_seen == 1


def test_out_of_order_window_index_is_fail_loud() -> None:
    tracker = TrajectoryTracker()
    tracker.observe(_window(2, (ADDR_A, -90)))
    with pytest.raises(ValueError, match="index"):
        tracker.observe(_window(2, (ADDR_A, -88)))
    with pytest.raises(ValueError, match="index"):
        tracker.observe(_window(1, (ADDR_A, -88)))


def test_constructor_rejects_non_int_and_non_positive_knobs() -> None:
    with pytest.raises(TypeError):
        TrajectoryTracker(max_addresses=True)
    with pytest.raises(TypeError):
        # Runtime guard: bool is a subtype of int, float is not.
        TrajectoryTracker(fade_after_windows=12.0)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError):
        TrajectoryTracker(max_addresses=0)
    params = signature(TrajectoryTracker.__init__).parameters
    assert "sample_windows" not in params


def test_snapshot_is_frozen() -> None:
    tracker = TrajectoryTracker()
    row = tracker.observe(_window(0, (ADDR_A, -90)))[0]
    frozen_field = "rising"
    with pytest.raises(ValidationError):
        setattr(row, frozen_field, True)


def test_identity_is_omitted_from_default_repr() -> None:
    """Field access is fine; repr/str must not leak the address."""
    tracker = TrajectoryTracker()
    row = tracker.observe(_window(0, (ADDR_A, -90)))[0]
    assert row.identity == ADDR_A
    dumped = f"{row!r} {row!s}"
    assert ADDR_A not in dumped


def test_tracker_does_not_emit_detection_events() -> None:
    tracker = TrajectoryTracker()
    rows = tracker.observe(_window(0, (ADDR_A, -90)))
    assert isinstance(rows[0], AddressTrajectory)
    assert not isinstance(rows[0], DetectionEvent)
    params = signature(TrajectoryTracker.observe).parameters
    assert "familiar" not in params
    assert list(params)[1] == "window"
