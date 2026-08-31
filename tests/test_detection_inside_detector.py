# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""InsideDetector tests (I3 / #138).

Pins the sustained adjacent-to-Pi backend: I1 count + sustain, I2
exclusion hooks, fire-once per episode, heard-only observe, replay
DoD (dwell vs transient), and run_cycle enqueue (DC-1).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from blesentry.config import (
    Config,
    InsideDetectionConfig,
    build_detector,
)
from blesentry.detection.familiar import FamiliarSet
from blesentry.detection.inside import (
    INSIDE_DETECTOR_ID,
    INSIDE_KIND,
    INSIDE_SUSTAIN_WINDOWS,
)
from blesentry.detection.inside_detector import (
    InsideDetector,
    format_inside_alert,
)
from blesentry.detection.models import DetectionEvent, DetectionWindow
from blesentry.detection.protocol import Detector
from blesentry.detection.replay import (
    detector_for_backend,
    format_report,
    replay_heard_fixture,
)
from blesentry.loop import run_cycle
from blesentry.notifier.models import OutboundMessage
from blesentry.scanner.mock import MockScanner
from blesentry.scanner.models import Advertisement
from blesentry.storage.database import apply_migrations, connect
from blesentry.storage.repository import (
    DeviceRepository,
    ObservationRepository,
    OutboxRepository,
)

REPLAY_DIR = Path(__file__).parent / "fixtures" / "replay"
DWELL_FIXTURE = REPLAY_DIR / "inside-dwell.json"
DWELL_GOLDEN = REPLAY_DIR / "inside-dwell-golden.json"
TRANSIENT_FIXTURE = REPLAY_DIR / "inside-transient.json"
STRANGER_ID = 42
DWELL_ALERT = "Sustained adjacent-to-Pi: 1 device(s) (device 42)."


def _window(
    index: int,
    heard: dict[int, int],
) -> DetectionWindow:
    return DetectionWindow(index=index, heard=heard)


def _feed(
    detector: InsideDetector,
    heard_windows: list[dict[int, int]],
    *,
    start: int = 0,
) -> list[DetectionEvent]:
    events: list[DetectionEvent] = []
    for offset, heard in enumerate(heard_windows):
        events.extend(detector.observe(_window(start + offset, heard)))
    return events


# --- seam ------------------------------------------------------------


def test_inside_detector_satisfies_protocol() -> None:
    detector = InsideDetector()
    assert isinstance(detector, Detector)
    assert not inspect.iscoroutinefunction(detector.observe)
    window = _window(0, {STRANGER_ID: -55})
    assert not inspect.iscoroutine(detector.observe(window))


def test_build_detector_inside_returns_backend() -> None:
    detector = build_detector(InsideDetectionConfig(backend="inside"))
    assert isinstance(detector, InsideDetector)


def test_inside_config_parses() -> None:
    cfg = Config.model_validate(
        {
            "site_id": "test",
            "storage": {"db": "/tmp/test.db"},
            "detection": {"backend": "inside"},
        }
    )
    assert cfg.detection.backend == "inside"


def test_observe_ignores_advertisements() -> None:
    from blesentry.scanner.models import Advertisement

    detector = InsideDetector()
    ad = Advertisement(
        address="AA:BB:00:00:00:01",
        rssi=-55,
        timestamp=1.0,
        adapter_id="test",
    )
    events = detector.observe(
        DetectionWindow(index=0, advertisements=(ad,), heard={})
    )
    assert events == ()


# --- sustain + fire-once ---------------------------------------------


def test_sustained_dwell_fires_once_at_eighth_window() -> None:
    detector = InsideDetector()
    heard = {STRANGER_ID: -55}
    events = _feed(detector, [heard] * INSIDE_SUSTAIN_WINDOWS)
    assert len(events) == 1
    event = events[0]
    assert event.detector == INSIDE_DETECTOR_ID
    assert event.kind == INSIDE_KIND
    assert event.window_index == INSIDE_SUSTAIN_WINDOWS - 1
    assert event.count == 1
    assert event.contributors == (STRANGER_ID,)


def test_continued_dwell_does_not_re_fire() -> None:
    detector = InsideDetector()
    heard = {STRANGER_ID: -55}
    events = _feed(detector, [heard] * (INSIDE_SUSTAIN_WINDOWS + 3))
    assert len(events) == 1


def test_quiet_window_resets_episode() -> None:
    detector = InsideDetector()
    heard = {STRANGER_ID: -55}
    quiet: dict[int, int] = {}
    first = _feed(detector, [heard] * INSIDE_SUSTAIN_WINDOWS)
    assert len(first) == 1
    between = _feed(detector, [quiet] * 2, start=INSIDE_SUSTAIN_WINDOWS)
    assert between == []
    second = _feed(
        detector,
        [heard] * INSIDE_SUSTAIN_WINDOWS,
        start=INSIDE_SUSTAIN_WINDOWS + 2,
    )
    assert len(second) == 1
    assert second[0].window_index == INSIDE_SUSTAIN_WINDOWS + 2 + 7


def test_transient_adjacent_pass_is_silent() -> None:
    detector = InsideDetector()
    heard = {STRANGER_ID: -55}
    quiet: dict[int, int] = {}
    events = _feed(detector, [heard, heard, quiet, quiet])
    assert events == []


def test_exclusion_subtracts_familiar_and_own_rotating() -> None:
    familiar = FamiliarSet(frozenset({1}))
    detector = InsideDetector(
        familiar=familiar,
        own_rotating_gear=frozenset({2}),
    )
    heard = {1: -55, 2: -54, STRANGER_ID: -53}
    events = _feed(detector, [heard] * INSIDE_SUSTAIN_WINDOWS)
    assert len(events) == 1
    assert events[0].count == 1
    assert events[0].contributors == (STRANGER_ID,)


# --- alert text ------------------------------------------------------


def test_format_inside_alert() -> None:
    event = DetectionEvent(
        detector=INSIDE_DETECTOR_ID,
        kind=INSIDE_KIND,
        window_index=7,
        count=1,
        contributors=(STRANGER_ID,),
    )
    assert format_inside_alert(event) == DWELL_ALERT


def test_format_inside_alert_requires_fields() -> None:
    with pytest.raises(ValueError):
        format_inside_alert(
            DetectionEvent(
                detector=INSIDE_DETECTOR_ID,
                kind=INSIDE_KIND,
                window_index=0,
            )
        )


# --- replay DoD ------------------------------------------------------


def test_replay_dwell_matches_golden() -> None:
    detector = detector_for_backend("inside")
    report = replay_heard_fixture(DWELL_FIXTURE, detector)
    assert json.loads(format_report(report)) == json.loads(
        DWELL_GOLDEN.read_text(encoding="utf-8")
    )
    assert report.events[0].window_index == 7
    assert report.events[0].count == 1


def test_replay_transient_is_silent() -> None:
    detector = detector_for_backend("inside")
    report = replay_heard_fixture(TRANSIENT_FIXTURE, detector)
    assert report.events == ()


def test_detector_for_backend_inside() -> None:
    assert isinstance(detector_for_backend("inside"), InsideDetector)


# --- run_cycle / outbox (DC-1) ---------------------------------------


@pytest.mark.asyncio
async def test_run_cycle_inside_heard_enqueue(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "i3-heard.db")
    try:
        await apply_migrations(conn)
        devices = DeviceRepository(conn, "test-site")
        observations = ObservationRepository(conn, "test-site")
        outbox = OutboxRepository(conn, "test-site")
        detector = InsideDetector()
        for index in range(INSIDE_SUSTAIN_WINDOWS):
            await run_cycle(
                MockScanner(
                    scenarios=[
                        [
                            Advertisement(
                                address="AA:BB:00:00:00:0B",
                                rssi=-55,
                                timestamp=float(index),
                                adapter_id="test",
                            )
                        ]
                    ]
                ),
                devices,
                observations,
                0.0,
                window_index=index,
                detector=detector,
                outbox=outbox,
            )
        pending = await outbox.list_pending()
        texts = [
            OutboundMessage.model_validate_json(row["payload"]).text
            for row in pending
        ]
        assert len(texts) == 1
        assert texts[0].startswith(
            "Sustained adjacent-to-Pi: 1 device(s) (device "
        )
        assert texts[0].endswith(").")
        assert "AA:BB" not in texts[0]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_run_cycle_detector_requires_outbox(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "i3.db")
    try:
        await apply_migrations(conn)
        devices = DeviceRepository(conn, "test-site")
        observations = ObservationRepository(conn, "test-site")
        with pytest.raises(ValueError, match="outbox"):
            await run_cycle(
                MockScanner(scenarios=[[]]),
                devices,
                observations,
                0.0,
                window_index=0,
                detector=InsideDetector(),
            )
    finally:
        await conn.close()


def test_replay_heard_fixture_rejects_empty_heard(tmp_path: Path) -> None:
    path = tmp_path / "empty-heard.json"
    path.write_text('[{"heard": []}]', encoding="utf-8")
    with pytest.raises(ValueError, match="no heard entries"):
        replay_heard_fixture(path, InsideDetector())


def test_detection_event_accepts_inside_fields() -> None:
    event = DetectionEvent(
        detector=INSIDE_DETECTOR_ID,
        kind=INSIDE_KIND,
        window_index=0,
        count=2,
        contributors=(1, 2),
    )
    assert event.count == 2
    assert event.contributors == (1, 2)


def test_detection_event_rejects_empty_contributors_when_set() -> None:
    with pytest.raises(ValidationError):
        DetectionEvent(
            detector=INSIDE_DETECTOR_ID,
            kind=INSIDE_KIND,
            window_index=0,
            count=1,
            contributors=(),
        )
