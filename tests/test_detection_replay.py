# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Offline replay harness tests (F1 / #120).

Pins windowing, read-only snapshot replay, and the golden-file report
without a radio, the scan loop, or the outbox. See docs/replay.md.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from blesentry.cli import build_parser, main
from blesentry.detection.mock import MockDetector
from blesentry.detection.models import DetectionEvent, DetectionWindow
from blesentry.detection.null import NullDetector
from blesentry.detection.replay import (
    DEFAULT_REPLAY_PERIOD,
    detector_for_backend,
    format_report,
    load_advertisement_fixture,
    make_report,
    parse_observed_at,
    replay,
    replay_fixture,
    replay_snapshot,
    windows_from_advertisements,
    windows_from_observations,
)
from blesentry.loop import iso_utc
from blesentry.scanner.models import Advertisement
from blesentry.storage.database import (
    apply_migrations,
    connect,
    connect_readonly,
)
from blesentry.storage.repository import (
    DeviceRepository,
    ObservationRepository,
    ObservationRow,
)

REPLAY_DIR = Path(__file__).parent / "fixtures" / "replay"
SPAN = REPLAY_DIR / "span.json"
GOLDEN = REPLAY_DIR / "span-golden.json"
DAY = 86400.0
T0 = 1_700_000_000.0


def _ad(*, address: str, rssi: int, timestamp: float) -> Advertisement:
    return Advertisement(
        address=address,
        rssi=rssi,
        timestamp=timestamp,
        adapter_id="test",
    )


def _obs(
    *,
    device_id: int,
    rssi: int,
    observed_at: str,
    obs_id: int = 1,
) -> ObservationRow:
    return ObservationRow(
        id=obs_id,
        site_id="test-site",
        device_id=device_id,
        rssi=rssi,
        observed_at=observed_at,
        adapter_id="test",
        address_type=None,
        adv_type=None,
    )


# --- windowing: advertisements ---------------------------------------


def test_default_period_is_scan_cadence() -> None:
    assert DEFAULT_REPLAY_PERIOD == 15.0


def test_advertisements_bucket_by_period_and_fill_gaps() -> None:
    ads = [
        _ad(address="AA:BB:00:00:00:01", rssi=-80, timestamp=T0),
        _ad(address="AA:BB:00:00:00:02", rssi=-70, timestamp=T0 + 15),
        _ad(address="AA:BB:00:00:00:01", rssi=-60, timestamp=T0 + 45),
    ]
    windows = windows_from_advertisements(ads, period=15.0)
    assert [w.index for w in windows] == [0, 1, 2, 3]
    assert len(windows[0].advertisements) == 1
    assert len(windows[1].advertisements) == 1
    assert windows[2].advertisements == ()
    assert len(windows[3].advertisements) == 1
    assert dict(windows[0].heard) == {}


def test_empty_advertisements_yield_no_windows() -> None:
    assert windows_from_advertisements(()) == []


def test_windowing_is_deterministic() -> None:
    ads = [
        _ad(address="AA:BB:00:00:00:01", rssi=-80, timestamp=T0),
        _ad(address="AA:BB:00:00:00:02", rssi=-70, timestamp=T0 + 15),
    ]
    first = windows_from_advertisements(ads, period=15.0)
    second = windows_from_advertisements(ads, period=15.0)
    assert first == second


def test_rejects_non_positive_period() -> None:
    ads = [_ad(address="AA:BB:00:00:00:01", rssi=-80, timestamp=T0)]
    with pytest.raises(ValueError, match="period"):
        windows_from_advertisements(ads, period=0)
    with pytest.raises(ValueError, match="period"):
        windows_from_observations(
            [_obs(device_id=1, rssi=-70, observed_at=iso_utc(T0))],
            period=-1,
        )


@pytest.mark.parametrize("period", [float("inf"), float("-inf"), float("nan")])
def test_rejects_non_finite_period(period: float) -> None:
    ads = [_ad(address="AA:BB:00:00:00:01", rssi=-80, timestamp=T0)]
    with pytest.raises(ValueError, match="period"):
        windows_from_advertisements(ads, period=period)


# --- windowing: N-day span -------------------------------------------


def test_two_day_span_includes_empty_middle_day() -> None:
    ads = [
        _ad(address="AA:BB:00:00:00:01", rssi=-80, timestamp=T0),
        _ad(
            address="AA:BB:00:00:00:01",
            rssi=-70,
            timestamp=T0 + 2 * DAY,
        ),
    ]
    windows = windows_from_advertisements(ads, period=DAY)
    assert len(windows) == 3
    assert [len(w.advertisements) for w in windows] == [1, 0, 1]


# --- windowing: observations -----------------------------------------


def test_observations_fill_heard_with_max_rssi() -> None:
    rows = [
        _obs(device_id=7, rssi=-90, observed_at=iso_utc(T0), obs_id=1),
        _obs(device_id=7, rssi=-55, observed_at=iso_utc(T0 + 1), obs_id=2),
        _obs(device_id=3, rssi=-70, observed_at=iso_utc(T0 + 1), obs_id=3),
        _obs(
            device_id=7,
            rssi=-40,
            observed_at=iso_utc(T0 + 15),
            obs_id=4,
        ),
    ]
    windows = windows_from_observations(rows, period=15.0)
    assert [w.index for w in windows] == [0, 1]
    assert windows[0].advertisements == ()
    assert dict(windows[0].heard) == {3: -70, 7: -55}
    assert dict(windows[1].heard) == {7: -40}


def test_empty_observations_yield_no_windows() -> None:
    assert windows_from_observations(()) == []


# --- replay loop -----------------------------------------------------


def test_replay_collects_scripted_events_in_window_order() -> None:
    early = DetectionEvent(detector="mock", kind="a", window_index=0)
    late = DetectionEvent(detector="mock", kind="b", window_index=2)
    detector = MockDetector(events=[[early], [], [late]])
    windows = [
        DetectionWindow(index=0),
        DetectionWindow(index=1),
        DetectionWindow(index=2),
    ]
    assert replay(detector, windows) == [early, late]
    assert [w.index for w in detector.observed] == [0, 1, 2]


def test_replay_does_not_enqueue_or_write() -> None:
    """NullDetector returns () — the harness must not invent outbox I/O."""
    windows = windows_from_advertisements(
        [_ad(address="AA:BB:00:00:00:01", rssi=-80, timestamp=T0)],
        period=15.0,
    )
    assert replay(NullDetector(), windows) == []


def test_format_report_omits_null_optional_event_fields() -> None:
    """Mock events stay three tokens in replay JSON (ADR-0006)."""
    event = DetectionEvent(detector="mock", kind="ping", window_index=0)
    report = make_report([DetectionWindow(index=0)], [event], period=15.0)
    raw = json.loads(format_report(report))
    assert raw["events"] == [
        {"detector": "mock", "kind": "ping", "window_index": 0}
    ]


# --- golden file -----------------------------------------------------


def test_fixture_replay_matches_golden_file() -> None:
    report = replay_fixture(SPAN, NullDetector(), period=15.0)
    assert json.loads(format_report(report)) == json.loads(
        GOLDEN.read_text(encoding="utf-8")
    )


def test_fixture_replay_is_byte_stable() -> None:
    first = format_report(replay_fixture(SPAN, NullDetector(), period=15.0))
    second = format_report(replay_fixture(SPAN, NullDetector(), period=15.0))
    assert first == second


def test_make_report_sorts_heard_pairs() -> None:
    window = DetectionWindow(index=0, heard={9: -80, 2: -60})
    report = make_report([window], (), period=15.0)
    assert report.windows[0].heard == ((2, -60), (9, -80))
    assert report.window_count == 1


# --- snapshot (read-only SQLite) -------------------------------------


@pytest.mark.asyncio
async def test_snapshot_replay_is_read_only_and_site_scoped(
    tmp_path: Path,
) -> None:
    path = tmp_path / "snap.db"
    conn = await connect(path)
    try:
        await apply_migrations(conn)
        here = DeviceRepository(conn, "here")
        there = DeviceRepository(conn, "there")
        hid = await here.upsert(
            fingerprint="fp-h", address="AA:00:00:00:00:01"
        )
        tid = await there.upsert(
            fingerprint="fp-t", address="AA:00:00:00:00:02"
        )
        await ObservationRepository(conn, "here").append(
            device_id=hid,
            rssi=-70,
            observed_at=iso_utc(T0),
        )
        await ObservationRepository(conn, "there").append(
            device_id=tid,
            rssi=-40,
            observed_at=iso_utc(T0),
        )
    finally:
        await conn.close()

    report = await replay_snapshot(path, "here", NullDetector(), period=15.0)
    assert report.window_count == 1
    assert report.windows[0].heard == ((hid, -70),)
    assert report.windows[0].advertisement_count == 0

    ro = await connect_readonly(path)
    try:
        with pytest.raises(Exception, match="readonly"):
            await ro.execute("CREATE TABLE should_fail (x INTEGER)")
            await ro.commit()
    finally:
        await ro.close()


# --- CLI -------------------------------------------------------------


def test_replay_subcommand_parses_fixture() -> None:
    args = build_parser().parse_args(
        ["replay", "--fixture", "span.json", "--period", "15"]
    )
    assert args.command == "replay"
    assert args.fixture == "span.json"
    assert args.period == 15.0
    assert args.backend == "none"


def test_replay_subcommand_parses_db() -> None:
    args = build_parser().parse_args(
        ["replay", "--db", "snap.db", "--site-id", "s", "--backend", "mock"]
    )
    assert args.db == "snap.db"
    assert args.site_id == "s"
    assert args.backend == "mock"


def test_replay_cli_writes_walkby_approach_golden(
    capsys: pytest.CaptureFixture[str],
) -> None:
    walkby = REPLAY_DIR / "walkby.json"
    golden = REPLAY_DIR / "walkby-approach-golden.json"
    code = main(
        [
            "replay",
            "--fixture",
            str(walkby),
            "--period",
            "15",
            "--backend",
            "approach",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == json.loads(
        golden.read_text(encoding="utf-8")
    )


def test_replay_cli_writes_golden_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "replay",
            "--fixture",
            str(SPAN),
            "--period",
            "15",
            "--backend",
            "none",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == json.loads(
        GOLDEN.read_text(encoding="utf-8")
    )


def test_parse_observed_at_rejects_naive() -> None:
    with pytest.raises(ValueError, match="Z"):
        parse_observed_at("2026-01-15T00:00:00.000")


def test_fixture_must_be_a_json_array(tmp_path: Path) -> None:
    path = tmp_path / "not-array.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(TypeError, match="JSON array"):
        load_advertisement_fixture(path)


def test_detector_for_backend_none_mock_approach() -> None:
    from blesentry.detection.approach_detector import ApproachDetector

    assert isinstance(detector_for_backend("none"), NullDetector)
    assert isinstance(detector_for_backend("mock"), MockDetector)
    assert isinstance(detector_for_backend("approach"), ApproachDetector)
    with pytest.raises(ValueError, match="unknown"):
        detector_for_backend("crowd")


def test_replay_cli_requires_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["replay"]) == 1
    assert "requires --fixture" in capsys.readouterr().err


def test_replay_cli_rejects_infinite_period(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["replay", "--fixture", str(SPAN), "--period", "inf"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "period" in err


def test_replay_cli_rejects_fixture_with_db(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "replay",
                "--fixture",
                str(SPAN),
                "--db",
                "snap.db",
                "--site-id",
                "s",
            ]
        )
        == 1
    )
    assert "cannot be combined" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_connect_readonly_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="snapshot not found"):
        await connect_readonly(tmp_path / "missing.db")


def test_replay_cli_bad_snapshot_is_error_not_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "not-a-db.db"
    path.write_text("this is not sqlite", encoding="utf-8")
    assert main(["replay", "--db", str(path), "--site-id", "here"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "Traceback" not in err


def test_replay_cli_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "snap.db"

    async def _seed() -> int:
        conn = await connect(path)
        try:
            await apply_migrations(conn)
            devices = DeviceRepository(conn, "here")
            device_id = await devices.upsert(
                fingerprint="fp-h", address="AA:00:00:00:00:01"
            )
            await ObservationRepository(conn, "here").append(
                device_id=device_id,
                rssi=-70,
                observed_at=iso_utc(T0),
            )
            return device_id
        finally:
            await conn.close()

    device_id = asyncio.run(_seed())
    code = main(
        [
            "replay",
            "--db",
            str(path),
            "--site-id",
            "here",
            "--period",
            "15",
        ]
    )
    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["window_count"] == 1
    assert report["windows"][0]["heard"] == [[device_id, -70]]
