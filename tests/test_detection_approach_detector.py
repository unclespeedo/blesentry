# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""ApproachDetector tests (A3 / #128).

Pins the first real Detector backend: A1 predicate + A2 tracker,
fire-once, F3 band of terminal RSSI, no address in the event or
alert text. Replay and run_cycle enqueue live in this file's
siblings / this file.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from blesentry.config import (
    ApproachDetectionConfig,
    Config,
    build_detector,
)
from blesentry.detection.approach import (
    APPROACH_DETECTOR_ID,
    APPROACH_KIND,
    APPROACH_WINDOWS,
)
from blesentry.detection.approach_detector import (
    ApproachDetector,
    format_approach_alert,
)
from blesentry.detection.models import DetectionEvent, DetectionWindow
from blesentry.detection.protocol import Detector
from blesentry.detection.replay import (
    detector_for_backend,
    replay_fixture,
)
from blesentry.detection.trajectory import TRACKER_FADE_AFTER_WINDOWS
from blesentry.loop import run_cycle, run_loop
from blesentry.notifier.models import OutboundMessage
from blesentry.scanner.mock import MockScanner
from blesentry.scanner.models import Advertisement
from blesentry.storage.database import apply_migrations, connect
from blesentry.storage.repository import (
    DeviceRepository,
    ObservationRepository,
    OutboxRepository,
)

# Same climb as test_detection_approach.WALKBY / docs/approach.md.
WALKBY = [-99, -96, -92, -88, -84, -80, -76, -72]
ADDR = "AA:BB:00:00:00:01"
WALKBY_ALERT = "Approaching BLE device (far, RSSI -72 dBm, rising)."
REPLAY_DIR = Path(__file__).parent / "fixtures" / "replay"
WALKBY_FIXTURE = REPLAY_DIR / "walkby.json"
WALKBY_GOLDEN = REPLAY_DIR / "walkby-approach-golden.json"


def _ad(
    rssi: int,
    *,
    address: str = ADDR,
    timestamp: float = 1.0,
) -> Advertisement:
    return Advertisement(
        address=address,
        rssi=rssi,
        timestamp=timestamp,
        adapter_id="test",
    )


def _window(
    index: int,
    rssi: int | None,
    *,
    address: str = ADDR,
    heard: dict[int, int] | None = None,
) -> DetectionWindow:
    ads: tuple[Advertisement, ...] = ()
    if rssi is not None:
        ads = (_ad(rssi, address=address),)
    return DetectionWindow(
        index=index,
        advertisements=ads,
        heard=heard if heard is not None else {},
    )


def _feed(
    detector: ApproachDetector,
    rssis: list[int | None],
    *,
    start: int = 0,
    address: str = ADDR,
) -> list[DetectionEvent]:
    events: list[DetectionEvent] = []
    for offset, rssi in enumerate(rssis):
        events.extend(
            detector.observe(_window(start + offset, rssi, address=address))
        )
    return events


# --- seam ------------------------------------------------------------


def test_approach_detector_satisfies_protocol() -> None:
    detector = ApproachDetector()
    assert isinstance(detector, Detector)
    assert not inspect.iscoroutinefunction(detector.observe)
    window = _window(0, -90)
    assert not inspect.iscoroutine(detector.observe(window))


def test_build_detector_approach_returns_backend() -> None:
    detector = build_detector(ApproachDetectionConfig(backend="approach"))
    assert isinstance(detector, ApproachDetector)


def test_approach_config_parses() -> None:
    cfg = Config(
        site_id="s",
        storage={"db": "x.db"},
        detection={"backend": "approach"},
    )
    assert isinstance(cfg.detection, ApproachDetectionConfig)
    assert cfg.detection.backend == "approach"


# --- trigger + peak --------------------------------------------------


def test_walkby_emits_once_at_peak_window() -> None:
    detector = ApproachDetector()
    events = _feed(detector, list(WALKBY))
    assert len(WALKBY) == APPROACH_WINDOWS
    assert len(events) == 1
    event = events[0]
    assert event.detector == APPROACH_DETECTOR_ID
    assert event.kind == APPROACH_KIND
    assert event.window_index == APPROACH_WINDOWS - 1
    assert event.rssi == -72
    assert event.band == "far"
    assert event.rising is True
    dumped = event.model_dump()
    assert "identity" not in dumped
    assert ADDR not in json.dumps(dumped)


def test_fire_once_until_fade() -> None:
    detector = ApproachDetector()
    first = _feed(detector, list(WALKBY))
    linger = _feed(
        detector,
        [-72, -72, -70],
        start=APPROACH_WINDOWS,
    )
    assert len(first) == 1
    assert linger == []


def test_fade_allows_a_second_visit() -> None:
    detector = ApproachDetector()
    _feed(detector, list(WALKBY))
    quiet_start = APPROACH_WINDOWS
    quiet = [None] * TRACKER_FADE_AFTER_WINDOWS
    assert _feed(detector, quiet, start=quiet_start) == []
    second_start = quiet_start + TRACKER_FADE_AFTER_WINDOWS
    second = _feed(detector, list(WALKBY), start=second_start)
    assert len(second) == 1
    assert second[0].window_index == second_start + APPROACH_WINDOWS - 1


def test_heard_only_windows_do_not_fire() -> None:
    """Approach is pre-fusion; snapshot `heard` is the wrong stream."""
    detector = ApproachDetector()
    events: list[DetectionEvent] = []
    for index, rssi in enumerate(WALKBY):
        events.extend(
            detector.observe(DetectionWindow(index=index, heard={1: rssi}))
        )
    assert events == []


def test_two_identities_two_events_no_addresses() -> None:
    detector = ApproachDetector()
    other = "AA:BB:00:00:00:02"
    events: list[DetectionEvent] = []
    for index, rssi in enumerate(WALKBY):
        events.extend(
            detector.observe(
                DetectionWindow(
                    index=index,
                    advertisements=(
                        _ad(rssi, address=ADDR),
                        _ad(rssi, address=other),
                    ),
                )
            )
        )
    assert len(events) == 2
    text = json.dumps([e.model_dump() for e in events])
    assert ADDR not in text
    assert other not in text


# --- alert text ------------------------------------------------------


def test_alert_text_snapshot() -> None:
    event = DetectionEvent(
        detector=APPROACH_DETECTOR_ID,
        kind=APPROACH_KIND,
        window_index=7,
        rssi=-72,
        band="far",
        rising=True,
    )
    text = format_approach_alert(event)
    assert text == WALKBY_ALERT
    assert ADDR not in text
    assert "metre" not in text.lower()
    assert " meter" not in text.lower()


def test_alert_text_rejects_incomplete_event() -> None:
    with pytest.raises(ValueError, match="rssi"):
        format_approach_alert(
            DetectionEvent(
                detector=APPROACH_DETECTOR_ID,
                kind=APPROACH_KIND,
                window_index=0,
            )
        )
    with pytest.raises(ValueError, match="rising"):
        format_approach_alert(
            DetectionEvent(
                detector=APPROACH_DETECTOR_ID,
                kind=APPROACH_KIND,
                window_index=7,
                rssi=-72,
                band="far",
                rising=False,
            )
        )


# --- replay DoD ------------------------------------------------------


def test_replay_walkby_matches_golden() -> None:
    detector = detector_for_backend("approach")
    report = replay_fixture(WALKBY_FIXTURE, detector, period=15.0)
    assert json.loads(report.model_dump_json()) == json.loads(
        WALKBY_GOLDEN.read_text(encoding="utf-8")
    )
    assert report.events[0].window_index == 7
    assert report.events[0].rssi == -72


# --- run_cycle / outbox (DC-1) ---------------------------------------


@pytest.mark.asyncio
async def test_run_loop_enqueues_approach_alert(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "a3.db")
    try:
        await apply_migrations(conn)
        devices = DeviceRepository(conn, "test-site")
        observations = ObservationRepository(conn, "test-site")
        outbox = OutboxRepository(conn, "test-site")
        scenarios = [
            [_ad(rssi, timestamp=1.0 + i)] for i, rssi in enumerate(WALKBY)
        ]
        await run_loop(
            MockScanner(scenarios=scenarios),
            devices,
            observations,
            duration=0.0,
            pause=0.0,
            max_cycles=len(scenarios),
            detector=ApproachDetector(),
            outbox=outbox,
        )
        pending = await outbox.list_pending()
        texts = [
            OutboundMessage.model_validate_json(row["payload"]).text
            for row in pending
        ]
        assert texts == [WALKBY_ALERT]
        assert ADDR not in texts[0]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_run_cycle_detector_requires_outbox(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "a3.db")
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
                detector=ApproachDetector(),
            )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_run_cycle_outbox_must_share_cycle_connection(
    tmp_path: Path,
) -> None:
    db = tmp_path / "a3.db"
    scan_conn = await connect(db)
    await apply_migrations(scan_conn)
    other_conn = await connect(db)
    try:
        devices = DeviceRepository(scan_conn, "test-site")
        observations = ObservationRepository(scan_conn, "test-site")
        outbox = OutboxRepository(other_conn, "test-site")
        with pytest.raises(ValueError, match="cycle connection"):
            await run_cycle(
                MockScanner(scenarios=[[]]),
                devices,
                observations,
                0.0,
                detector=ApproachDetector(),
                outbox=outbox,
            )
    finally:
        await other_conn.close()
        await scan_conn.close()


@pytest.mark.asyncio
async def test_run_cycle_outbox_must_share_cycle_site(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "a3.db")
    try:
        await apply_migrations(conn)
        devices = DeviceRepository(conn, "test-site")
        observations = ObservationRepository(conn, "test-site")
        outbox = OutboxRepository(conn, "other-site")
        with pytest.raises(ValueError, match="cycle site"):
            await run_cycle(
                MockScanner(scenarios=[[]]),
                devices,
                observations,
                0.0,
                detector=ApproachDetector(),
                outbox=outbox,
            )
    finally:
        await conn.close()


def test_detection_event_rejects_unknown_band() -> None:
    with pytest.raises(ValidationError):
        DetectionEvent.model_validate(
            {
                "detector": "approach",
                "kind": "approaching",
                "window_index": 0,
                "band": "metres",
            }
        )
