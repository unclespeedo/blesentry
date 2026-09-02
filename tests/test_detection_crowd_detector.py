# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""CrowdDetector tests (C4 / #134).

Pins the crowd-busy backend: C3 baseline + CUSUM, fire-once per
episode, heard-only observe, replay DoD (sustained vs spike), and
run_cycle enqueue (DC-1).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from blesentry.cli import main
from blesentry.config import (
    Config,
    CrowdDetectionConfig,
    build_detector,
)
from blesentry.detection.crowd import (
    CROWD_CUSUM_H,
    CROWD_CUSUM_K,
    CROWD_DETECTOR_ID,
    CROWD_KIND,
)
from blesentry.detection.crowd_detector import (
    CrowdDetector,
    format_crowd_alert,
)
from blesentry.detection.models import DetectionEvent, DetectionWindow
from blesentry.detection.protocol import Detector
from blesentry.detection.replay import (
    detector_for_backend,
    format_report,
    load_advertisement_fixture,
    replay_heard_fixture,
)
from blesentry.loop import run_cycle
from blesentry.notifier.models import OutboundMessage
from blesentry.scanner.mock import MockScanner
from blesentry.storage.database import apply_migrations, connect
from blesentry.storage.repository import (
    DeviceRepository,
    ObservationRepository,
    OutboxRepository,
)

REPLAY_DIR = Path(__file__).parent / "fixtures" / "replay"
BUSY_FIXTURE = REPLAY_DIR / "crowd-busy.json"
BUSY_GOLDEN = REPLAY_DIR / "crowd-busy-golden.json"
SPIKE_FIXTURE = REPLAY_DIR / "crowd-spike.json"
CYCLE_AD_FIXTURE = REPLAY_DIR / "inside-cycle-ad.json"
QUIET_NEAR = 4
BUSY_NEAR = 7
SPIKE_NEAR = 10
WARMUP_WINDOWS = 60


def _heard_near(count: int, *, start_id: int = 1) -> dict[int, int]:
    return {start_id + offset: -65 for offset in range(count)}


def _window(
    index: int,
    heard: dict[int, int],
) -> DetectionWindow:
    return DetectionWindow(index=index, heard=heard)


def _feed(
    detector: CrowdDetector,
    heard_windows: list[dict[int, int]],
    *,
    start: int = 0,
) -> list[DetectionEvent]:
    events: list[DetectionEvent] = []
    for offset, heard in enumerate(heard_windows):
        events.extend(detector.observe(_window(start + offset, heard)))
    return events


def _warmup(detector: CrowdDetector, *, count: int = WARMUP_WINDOWS) -> None:
    _feed(detector, [_heard_near(QUIET_NEAR)] * count)


# --- seam -------------------------------------------------------------


def test_crowd_detector_satisfies_protocol() -> None:
    detector = CrowdDetector()
    assert isinstance(detector, Detector)
    assert not inspect.iscoroutinefunction(detector.observe)
    window = _window(0, _heard_near(1))
    assert not inspect.iscoroutine(detector.observe(window))


def test_build_detector_crowd_returns_backend() -> None:
    detector = build_detector(CrowdDetectionConfig(backend="crowd"))
    assert isinstance(detector, CrowdDetector)


def test_crowd_config_parses() -> None:
    cfg = Config.model_validate(
        {
            "site_id": "test",
            "storage": {"db": "/tmp/test.db"},
            "detection": {"backend": "crowd"},
        }
    )
    assert cfg.detection.backend == "crowd"


def test_observe_ignores_advertisements() -> None:
    detector = CrowdDetector()
    ad = load_advertisement_fixture(CYCLE_AD_FIXTURE)[0]
    events = detector.observe(
        DetectionWindow(index=0, advertisements=(ad,), heard={})
    )
    assert not events


# --- CUSUM + fire-once ------------------------------------------------


def test_single_window_spike_is_silent_after_warmup() -> None:
    detector = CrowdDetector()
    _warmup(detector)
    events = _feed(
        detector,
        [_heard_near(SPIKE_NEAR)],
        start=WARMUP_WINDOWS,
    )
    assert not events


def test_sustained_busy_fires_once() -> None:
    detector = CrowdDetector()
    _warmup(detector)
    events = _feed(
        detector,
        [_heard_near(BUSY_NEAR)] * 4,
        start=WARMUP_WINDOWS,
    )
    assert len(events) == 1
    event = events[0]
    assert event.detector == CROWD_DETECTOR_ID
    assert event.kind == CROWD_KIND
    assert event.count == BUSY_NEAR
    assert event.count_all == BUSY_NEAR
    assert event.contributors == tuple(range(1, BUSY_NEAR + 1))
    assert event.window_index == WARMUP_WINDOWS + 3


def test_sustained_busy_stays_fire_once_until_episode_ends() -> None:
    detector = CrowdDetector()
    _warmup(detector)
    start = WARMUP_WINDOWS
    first = _feed(detector, [_heard_near(BUSY_NEAR)] * 6, start=start)
    assert len(first) == 1
    quiet = _feed(
        detector,
        [{}] * 3,
        start=start + 6,
    )
    assert not quiet
    second = _feed(
        detector,
        [_heard_near(BUSY_NEAR)] * 4,
        start=start + 9,
    )
    assert len(second) == 1


def test_format_crowd_alert() -> None:
    event = DetectionEvent(
        detector=CROWD_DETECTOR_ID,
        kind=CROWD_KIND,
        window_index=12,
        count=3,
        count_all=5,
        contributors=(1, 2, 3),
    )
    assert format_crowd_alert(event) == (
        "Unusual site busyness: 3 near / 5 total "
        "(device 1, device 2, device 3)."
    )


def test_format_crowd_alert_requires_fields() -> None:
    with pytest.raises(ValueError, match="count"):
        format_crowd_alert(
            DetectionEvent(
                detector=CROWD_DETECTOR_ID,
                kind=CROWD_KIND,
                window_index=0,
            )
        )


# --- replay DoD -------------------------------------------------------


def test_replay_busy_matches_golden() -> None:
    detector = detector_for_backend("crowd")
    report = replay_heard_fixture(BUSY_FIXTURE, detector)
    assert json.loads(format_report(report)) == json.loads(
        BUSY_GOLDEN.read_text(encoding="utf-8")
    )
    assert report.events[0].kind == CROWD_KIND


def test_replay_spike_is_silent() -> None:
    detector = detector_for_backend("crowd")
    report = replay_heard_fixture(SPIKE_FIXTURE, detector)
    assert report.events == ()


def test_detector_for_backend_crowd() -> None:
    assert isinstance(detector_for_backend("crowd"), CrowdDetector)


def test_replay_cli_accepts_crowd_backend(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "replay",
            "--fixture",
            str(BUSY_FIXTURE),
            "--backend",
            "crowd",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == json.loads(
        BUSY_GOLDEN.read_text(encoding="utf-8")
    )


# --- run_cycle / outbox (DC-1) ----------------------------------------


@pytest.mark.asyncio
async def test_run_cycle_crowd_heard_enqueue(tmp_path: Path) -> None:
    conn = await connect(tmp_path / "c4-heard.db")
    try:
        await apply_migrations(conn)
        devices = DeviceRepository(conn, "test-site")
        observations = ObservationRepository(conn, "test-site")
        outbox = OutboxRepository(conn, "test-site")
        detector = CrowdDetector()
        cycle_ad = load_advertisement_fixture(CYCLE_AD_FIXTURE)[0]
        for index in range(WARMUP_WINDOWS + 4):
            near_count = BUSY_NEAR if index >= WARMUP_WINDOWS else QUIET_NEAR
            ads = [
                cycle_ad.model_copy(
                    update={
                        "address": f"AA:BB:00:00:00:{offset:02X}",
                        "rssi": -65,
                        "timestamp": float(index),
                    },
                )
                for offset in range(near_count)
            ]
            await run_cycle(
                MockScanner(scenarios=[ads]),
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
        assert texts[0].startswith("Unusual site busyness:")
        assert "near /" in texts[0]
        assert cycle_ad.address not in texts[0]
    finally:
        await conn.close()


def test_frozen_cusum_knobs_match_c1() -> None:
    assert CROWD_CUSUM_K == 0.5
    assert CROWD_CUSUM_H == 5.0
