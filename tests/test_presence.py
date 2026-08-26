# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""PresenceTracker state-machine tests (P2-1, #22).

Exhaustive scripted-window scenarios, including the v1 acceptance
criterion: a car driving past (a strong signal for 1–2 windows) must
never reach PRESENT, so it produces no transition and nothing to alert
on. Thresholds are counted in windows, so the tracker is deterministic
without any clock.
"""

from __future__ import annotations

import pytest

from blesentry.presence import (
    PresenceState,
    PresenceTracker,
    PresenceTransition,
)
from blesentry.scanner.models import Advertisement

PRESENT = PresenceState.PRESENT
ABSENT = PresenceState.ABSENT


def _tracker(**kw: int | None) -> PresenceTracker:
    params: dict[str, int | None] = {
        "appear_windows": 3,
        "disappear_windows": 3,
        "rssi_threshold": -80,
        "cooldown_windows": 0,
    }
    params.update(kw)
    return PresenceTracker(**params)  # type: ignore[arg-type]


# --- appear / disappear debounce -------------------------------------


def test_present_after_n_consecutive_hits() -> None:
    t = _tracker(appear_windows=3)
    assert t.update({1: -60}) == []
    assert t.update({1: -60}) == []
    assert t.update({1: -60}) == [PresenceTransition(1, PRESENT)]


def test_no_duplicate_present_while_present() -> None:
    t = _tracker(appear_windows=1)
    assert t.update({1: -60}) == [PresenceTransition(1, PRESENT)]
    assert t.update({1: -60}) == []
    assert t.update({1: -60}) == []


def test_absent_after_m_consecutive_misses() -> None:
    t = _tracker(appear_windows=1, disappear_windows=3)
    t.update({1: -60})  # PRESENT
    assert t.update({}) == []
    assert t.update({}) == []
    assert t.update({}) == [PresenceTransition(1, ABSENT)]


def test_hits_reset_on_a_miss() -> None:
    t = _tracker(appear_windows=3)
    t.update({1: -60})  # hit 1
    t.update({1: -60})  # hit 2
    t.update({})  # miss → reset
    t.update({1: -60})  # hit 1
    t.update({1: -60})  # hit 2
    assert t.update({1: -60}) == [PresenceTransition(1, PRESENT)]


def test_misses_reset_on_a_hit() -> None:
    t = _tracker(appear_windows=1, disappear_windows=3)
    t.update({1: -60})  # PRESENT
    t.update({})  # miss 1
    t.update({})  # miss 2
    t.update({1: -60})  # heard again → misses reset
    assert t.update({}) == []  # miss 1 only
    assert t.update({}) == []  # miss 2


# --- the v1 acceptance criterion: car pass-by ------------------------


def test_car_passby_never_reaches_present() -> None:
    t = _tracker(appear_windows=3)
    # Strong signal, but only two windows before it's gone.
    assert t.update({7: -45}) == []
    assert t.update({7: -45}) == []
    assert t.update({}) == []  # gone → never PRESENT
    assert t.update({}) == []
    # A subsequent car with one strong window is likewise silent.
    assert t.update({8: -40}) == []
    assert t.update({}) == []


# --- rssi threshold --------------------------------------------------


def test_below_threshold_counts_as_a_miss() -> None:
    t = _tracker(appear_windows=1, rssi_threshold=-80)
    assert t.update({1: -90}) == []  # too weak → not a hit
    assert t.update({1: -70}) == [PresenceTransition(1, PRESENT)]


def test_weak_signal_drops_a_present_device() -> None:
    t = _tracker(appear_windows=1, disappear_windows=1, rssi_threshold=-80)
    t.update({1: -60})  # PRESENT
    assert t.update({1: -95}) == [PresenceTransition(1, ABSENT)]  # weak = miss


# --- multiple devices ------------------------------------------------


def test_devices_are_tracked_independently() -> None:
    t = _tracker(appear_windows=2)
    t.update({1: -60, 2: -60})  # both hit 1
    transitions = t.update({1: -60, 2: -60})  # both hit 2 → PRESENT
    assert transitions == [
        PresenceTransition(1, PRESENT),
        PresenceTransition(2, PRESENT),
    ]


# --- cooldown --------------------------------------------------------


def test_cooldown_suppresses_return_within_window() -> None:
    t = _tracker(appear_windows=1, disappear_windows=1, cooldown_windows=2)
    assert t.update({1: -60}) == [PresenceTransition(1, PRESENT)]  # emit
    assert t.update({}) == [PresenceTransition(1, ABSENT)]  # leave, cd=2
    # Returns within the cooldown → same visit; ABSENT paired-suppressed.
    assert t.update({1: -60}) == []
    assert t.update({}) == []


def test_cooldown_allows_return_after_window() -> None:
    t = _tracker(appear_windows=1, disappear_windows=1, cooldown_windows=2)
    t.update({1: -60})  # PRESENT
    t.update({})  # ABSENT, cd=2
    t.update({})  # still gone, cd=1
    t.update({})  # still gone, cd=0
    # Gone longer than the cooldown → a fresh visit emits.
    assert t.update({1: -60}) == [PresenceTransition(1, PRESENT)]


def test_cooldown_effective_with_realistic_windows() -> None:
    # The panel's dead-zone case: with appear=3/disappear=3 a returning
    # device reconfirms over 3 windows, so cooldown must exceed appear.
    t = _tracker(appear_windows=3, disappear_windows=3, cooldown_windows=4)
    for _ in range(3):
        t.update({1: -60})  # → PRESENT
    for _ in range(3):
        t.update({})  # → ABSENT, cd=4
    returned: list[PresenceTransition] = []
    for _ in range(3):
        returned += t.update({1: -60})  # reconfirms at window 3, cd=1>0
    assert returned == []  # suppressed — one visit


def test_zero_cooldown_emits_every_visit() -> None:
    t = _tracker(appear_windows=1, disappear_windows=1, cooldown_windows=0)
    assert t.update({1: -60}) == [PresenceTransition(1, PRESENT)]
    assert t.update({}) == [PresenceTransition(1, ABSENT)]
    assert t.update({1: -60}) == [PresenceTransition(1, PRESENT)]


# --- memory pruning --------------------------------------------------


def test_absent_devices_are_pruned() -> None:
    t = _tracker(appear_windows=1, disappear_windows=1, prune_after_windows=2)
    t.update({1: -60})  # tracked, PRESENT
    t.update({})  # ABSENT, miss 1
    t.update({})  # miss 2 → pruned
    assert 1 not in t._devices


def test_never_present_transient_is_pruned() -> None:
    t = _tracker(appear_windows=3, prune_after_windows=2)
    t.update({9: -50})  # hit once, tracked, still ABSENT
    t.update({})  # miss 1
    t.update({})  # miss 2 → pruned (never was PRESENT)
    assert 9 not in t._devices


# --- validation ------------------------------------------------------


def test_rejects_bad_thresholds() -> None:
    with pytest.raises(ValueError):
        PresenceTracker(appear_windows=0)
    with pytest.raises(ValueError):
        PresenceTracker(disappear_windows=0)
    with pytest.raises(ValueError):
        PresenceTracker(cooldown_windows=-1)


# --- integration: MockScanner -> scan loop -> presence_events --------


class _WindowClock:
    """A clock advancing one scan window per call (for occurred_at)."""

    def __init__(self) -> None:
        self.t = 1_700_000_000.0

    def __call__(self) -> float:
        self.t += 15.0
        return self.t


def _ad(address: str, rssi: int, ts: float = 1.0) -> Advertisement:
    return Advertisement(
        address=address, rssi=rssi, timestamp=ts, adapter_id="test"
    )


async def test_scan_loop_emits_presence_and_ignores_car_passby() -> None:
    from blesentry.loop import run_loop
    from blesentry.scanner.mock import MockScanner
    from blesentry.storage import (
        DeviceRepository,
        ObservationRepository,
        PresenceEventRepository,
        apply_migrations,
        connect,
    )

    conn = await connect(":memory:")
    await apply_migrations(conn)
    site = "presence-site"
    devices = DeviceRepository(conn, site)
    observations = ObservationRepository(conn, site)
    presence_events = PresenceEventRepository(conn, site)
    tracker = PresenceTracker(
        appear_windows=3, disappear_windows=3, rssi_threshold=-80
    )

    resident = "AA:AA:AA:AA:AA:AA"
    car = "BB:BB:BB:BB:BB:BB"
    # 6 windows: resident present 3 windows then gone 3; car only 2 windows.
    scenarios = [
        [_ad(resident, -50), _ad(car, -45)],  # w1
        [_ad(resident, -50), _ad(car, -45)],  # w2
        [_ad(resident, -50)],  # w3: car gone after 2 windows
        [],  # w4
        [],  # w5
        [],  # w6
    ]
    try:
        await run_loop(
            MockScanner(scenarios=scenarios),
            devices,
            observations,
            duration=0.0,
            pause=0.0,
            max_cycles=6,
            presence=tracker,
            presence_events=presence_events,
            now=_WindowClock(),
        )
        all_devices = await devices.list_devices()
        by_address = {d["address"]: d["id"] for d in all_devices}

        resident_events = await presence_events.list_for_device(
            by_address[resident]
        )
        assert [e["event_type"] for e in resident_events] == [
            "PRESENT",
            "ABSENT",
        ]
        # The car passed by (2 strong windows) → never PRESENT → no event.
        car_events = await presence_events.list_for_device(by_address[car])
        assert car_events == []
    finally:
        await conn.close()


async def test_rssi_sequence_approach_presents_spike_does_not() -> None:
    """#58: sequence helper drives presence.

    Gradual approach reaches PRESENT; a 2-window spike stays silent.
    """
    from blesentry.loop import run_loop
    from blesentry.scanner.mock import MockScanner
    from blesentry.storage import (
        DeviceRepository,
        ObservationRepository,
        PresenceEventRepository,
        apply_migrations,
        connect,
    )

    conn = await connect(":memory:")
    await apply_migrations(conn)
    site = "rssi-seq-site"
    devices = DeviceRepository(conn, site)
    observations = ObservationRepository(conn, site)
    presence_events = PresenceEventRepository(conn, site)
    tracker = PresenceTracker(
        appear_windows=3, disappear_windows=3, rssi_threshold=-80
    )

    walker = "CC:CC:CC:CC:CC:CC"
    car = "DD:DD:DD:DD:DD:DD"
    templates = {
        walker: _ad(walker, -99),
        car: _ad(car, -99),
    }
    # Walker rises through the gate and stays; car is a 2-window spike.
    sequences = {
        walker: [-90, -70, -60, -55, -55, -55],
        car: [-40, -42],
    }
    try:
        await run_loop(
            MockScanner.from_rssi_sequences(
                templates=templates, sequences=sequences
            ),
            devices,
            observations,
            duration=0.0,
            pause=0.0,
            max_cycles=6,
            presence=tracker,
            presence_events=presence_events,
            now=_WindowClock(),
        )
        all_devices = await devices.list_devices()
        by_address = {d["address"]: d["id"] for d in all_devices}

        walker_events = await presence_events.list_for_device(
            by_address[walker]
        )
        assert [e["event_type"] for e in walker_events] == ["PRESENT"]
        car_events = await presence_events.list_for_device(by_address[car])
        assert car_events == []
    finally:
        await conn.close()


async def test_presence_requires_both_tracker_and_repo() -> None:
    from blesentry.loop import run_cycle
    from blesentry.scanner.mock import MockScanner
    from blesentry.storage import (
        DeviceRepository,
        ObservationRepository,
        apply_migrations,
        connect,
    )

    conn = await connect(":memory:")
    await apply_migrations(conn)
    try:
        with pytest.raises(ValueError, match="together"):
            await run_cycle(
                MockScanner(scenarios=[[]]),
                DeviceRepository(conn, "s"),
                ObservationRepository(conn, "s"),
                0.0,
                presence=PresenceTracker(),  # no presence_events
            )
    finally:
        await conn.close()
