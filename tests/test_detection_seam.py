# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Detector seam tests (F2 / #121): protocol, value objects, null, mock.

Pins the contract later detectors (approach / crowd / inside) and the
offline replay harness (F1) build on — without touching the scan loop
or the outbox. ADR-0006 records the frozen surface.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from blesentry.config import (
    Config,
    MockDetectionConfig,
    NoneDetectionConfig,
    build_detector,
)
from blesentry.detection.mock import MockDetector
from blesentry.detection.models import DetectionEvent, DetectionWindow
from blesentry.detection.null import NullDetector
from blesentry.detection.protocol import Detector
from blesentry.scanner.models import Advertisement


def _ad(
    *, address: str = "AA:BB:00:00:00:01", rssi: int = -70
) -> Advertisement:
    return Advertisement(
        address=address,
        rssi=rssi,
        timestamp=1.0,
        adapter_id="test",
    )


def _window(
    *,
    index: int = 0,
    advertisements: tuple[Advertisement, ...] | None = None,
    heard: dict[int, int] | None = None,
) -> DetectionWindow:
    return DetectionWindow(
        index=index,
        advertisements=advertisements if advertisements is not None else (),
        heard=heard if heard is not None else {},
    )


# --- value objects ---------------------------------------------------


def test_detection_event_is_frozen() -> None:
    event = DetectionEvent(detector="mock", kind="probe", window_index=0)
    frozen_field = "kind"
    with pytest.raises(ValidationError):
        setattr(event, frozen_field, "no")


def test_detection_event_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        DetectionEvent(
            detector="mock",
            kind="probe",
            window_index=0,
            extra_field=True,
        )


def test_detection_event_rejects_empty_tokens() -> None:
    with pytest.raises(ValidationError):
        DetectionEvent(detector="", kind="probe", window_index=0)
    with pytest.raises(ValidationError):
        DetectionEvent(detector="mock", kind="", window_index=0)


def test_detection_event_rejects_negative_window_index() -> None:
    with pytest.raises(ValidationError):
        DetectionEvent(detector="mock", kind="probe", window_index=-1)


def test_detection_window_is_frozen_and_copies_heard() -> None:
    heard = {1: -60}
    ads = (_ad(),)
    window = DetectionWindow(index=3, advertisements=ads, heard=heard)
    frozen_field = "index"
    with pytest.raises(ValidationError):
        setattr(window, frozen_field, 4)
    heard[1] = -10
    assert window.heard[1] == -60
    assert window.advertisements == ads
    assert window.index == 3


def test_detection_window_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        DetectionWindow(index=0, wall_clock=1.0)


def test_detection_window_rejects_negative_index() -> None:
    with pytest.raises(ValidationError):
        DetectionWindow(index=-1)


def test_empty_window_is_valid() -> None:
    window = DetectionWindow(index=0)
    assert window.advertisements == ()
    assert dict(window.heard) == {}


# --- protocol conformance -------------------------------------------


def test_mock_detector_satisfies_protocol() -> None:
    assert isinstance(MockDetector(), Detector)


def test_null_detector_satisfies_protocol() -> None:
    assert isinstance(NullDetector(), Detector)


def test_observe_is_synchronous() -> None:
    """A coroutine observe() would stall the scan loop (ADR-0006)."""
    null = NullDetector()
    mock = MockDetector()
    assert not inspect.iscoroutinefunction(null.observe)
    assert not inspect.iscoroutinefunction(mock.observe)
    window = _window()
    assert not inspect.iscoroutine(null.observe(window))
    assert not inspect.iscoroutine(mock.observe(window))


# --- NullDetector ---------------------------------------------------


def test_null_detector_emits_nothing() -> None:
    detector = NullDetector()
    window = _window(
        index=2,
        advertisements=(_ad(),),
        heard={7: -55},
    )
    assert detector.observe(window) == ()


# --- MockDetector ---------------------------------------------------


def test_mock_observe_records_windows_and_emits_nothing_by_default() -> None:
    detector = MockDetector()
    first = _window(index=0, heard={1: -70})
    second = _window(index=1, advertisements=(_ad(),), heard={1: -65})
    assert detector.observe(first) == ()
    assert detector.observe(second) == ()
    assert detector.observed == [first, second]


def test_mock_observe_replays_scripted_events() -> None:
    scripted = DetectionEvent(detector="mock", kind="probe", window_index=0)
    leftover = DetectionEvent(detector="mock", kind="later", window_index=1)
    detector = MockDetector(events=[[scripted], [leftover]])
    assert detector.observe(_window(index=0)) == (scripted,)
    assert detector.observe(_window(index=1)) == (leftover,)
    # Script exhausted → empty, still recorded.
    third = _window(index=2)
    assert detector.observe(third) == ()
    assert detector.observed[-1] is third


def test_mock_observe_does_not_mutate_inputs() -> None:
    ads = [_ad()]
    heard = {1: -70}
    window = DetectionWindow(index=0, advertisements=ads, heard=heard)
    MockDetector().observe(window)
    ads.append(_ad(address="AA:BB:00:00:00:02"))
    heard[1] = -20
    assert len(window.advertisements) == 1
    assert window.heard[1] == -70


# --- config widening ------------------------------------------------


def test_none_is_the_default_detection_backend() -> None:
    cfg = Config(site_id="s", storage={"db": "x.db"})
    assert isinstance(cfg.detection, NoneDetectionConfig)
    assert cfg.detection.backend == "none"


def test_mock_detection_config_parses() -> None:
    cfg = Config(
        site_id="s",
        storage={"db": "x.db"},
        detection={"backend": "mock"},
    )
    assert isinstance(cfg.detection, MockDetectionConfig)


def test_build_detector_none_returns_null() -> None:
    assert isinstance(build_detector(NoneDetectionConfig()), NullDetector)


def test_build_detector_mock_returns_mock() -> None:
    detector = build_detector(MockDetectionConfig(backend="mock"))
    assert isinstance(detector, MockDetector)


def test_unknown_detection_backend_rejected() -> None:
    # load_config path is in test_config; this pins the union at the model.
    with pytest.raises(ValidationError):
        Config(
            site_id="s",
            storage={"db": "x.db"},
            detection={"backend": "crowd"},
        )
