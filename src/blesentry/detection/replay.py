# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Offline replay harness (F1): history → DetectionWindows → events.

Clock-free, deterministic, read-only (DC-9). Does not enqueue to the
outbox and does not run inside ``run_cycle``. See ``docs/replay.md``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from blesentry.detection.models import DetectionEvent, DetectionWindow
from blesentry.detection.protocol import Detector
from blesentry.scanner.models import Advertisement
from blesentry.storage.repository import ObservationRow

# Scan cadence: [scan] window + [scan] pause defaults (10 s + 5 s).
DEFAULT_REPLAY_PERIOD = 15.0


class WindowSummary(BaseModel):
    """Per-window counts for the JSON report (no addresses/payloads)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    advertisement_count: int = Field(ge=0)
    heard: tuple[tuple[int, int], ...] = ()


class ReplayReport(BaseModel):
    """Deterministic replay output, golden-file friendly."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    period: float = Field(gt=0)
    window_count: int = Field(ge=0)
    windows: tuple[WindowSummary, ...] = ()
    events: tuple[DetectionEvent, ...] = ()


def _require_period(period: float) -> float:
    """Reject a non-finite or non-positive bucket width."""
    if not math.isfinite(period) or period <= 0:
        raise ValueError("period must be finite and > 0")
    return period


def parse_observed_at(value: str) -> float:
    """Parse schema UTC text (``…Z``) to epoch seconds."""
    if not value.endswith("Z"):
        raise ValueError(f"observed_at must end with Z: {value!r}")
    return datetime.fromisoformat(value).timestamp()


def windows_from_advertisements(
    advertisements: Sequence[Advertisement],
    *,
    period: float = DEFAULT_REPLAY_PERIOD,
) -> list[DetectionWindow]:
    """Bucket advertisements into dense 0-based windows.

    ``heard`` is empty: no resolver ran. Gaps become empty windows.

    Args:
        advertisements: Pre-fusion observations, any order.
        period: Bucket width in seconds (must be ``> 0``).

    Returns:
        One ``DetectionWindow`` per index from 0 through the last
        occupied bucket, inclusive.
    """
    period = _require_period(period)
    if not advertisements:
        return []
    t0 = min(ad.timestamp for ad in advertisements)
    buckets: dict[int, list[Advertisement]] = {}
    for ad in advertisements:
        index = int((ad.timestamp - t0) // period)
        buckets.setdefault(index, []).append(ad)
    last = max(buckets)
    return [
        DetectionWindow(
            index=index,
            advertisements=tuple(buckets.get(index, ())),
        )
        for index in range(last + 1)
    ]


def windows_from_observations(
    rows: Sequence[ObservationRow],
    *,
    period: float = DEFAULT_REPLAY_PERIOD,
) -> list[DetectionWindow]:
    """Bucket observation rows into dense 0-based windows.

    ``advertisements`` is empty: payloads are not on this table.
    ``heard`` is per-``device_id`` best (max) RSSI in the window.

    Args:
        rows: Site-scoped observation rows.
        period: Bucket width in seconds (must be ``> 0``).

    Returns:
        One ``DetectionWindow`` per index from 0 through the last
        occupied bucket, inclusive.
    """
    period = _require_period(period)
    if not rows:
        return []
    times = [parse_observed_at(row["observed_at"]) for row in rows]
    t0 = min(times)
    heard_buckets: dict[int, dict[int, int]] = {}
    last = 0
    for row, ts in zip(rows, times, strict=True):
        index = int((ts - t0) // period)
        last = max(last, index)
        device_id = row["device_id"]
        rssi = row["rssi"]
        bucket = heard_buckets.setdefault(index, {})
        current = bucket.get(device_id)
        if current is None or rssi > current:
            bucket[device_id] = rssi
    return [
        DetectionWindow(index=index, heard=heard_buckets.get(index, {}))
        for index in range(last + 1)
    ]


def replay(
    detector: Detector,
    windows: Sequence[DetectionWindow],
) -> list[DetectionEvent]:
    """Feed windows through ``observe`` and concatenate events.

    Does not enqueue, persist, or touch a repository. Empty windows
    are still observed so miss-counting detectors see gaps.

    Args:
        detector: Any ``Detector`` implementation.
        windows: Clock-free windows in index order.

    Returns:
        Events in window order; empty if nothing fired.
    """
    events: list[DetectionEvent] = []
    for window in windows:
        events.extend(detector.observe(window))
    return events


def make_report(
    windows: Sequence[DetectionWindow],
    events: Sequence[DetectionEvent],
    *,
    period: float,
) -> ReplayReport:
    """Build the JSON report; ``heard`` pairs sorted by device id."""
    period = _require_period(period)
    summaries = tuple(
        WindowSummary(
            index=window.index,
            advertisement_count=len(window.advertisements),
            heard=tuple(sorted(window.heard.items())),
        )
        for window in windows
    )
    return ReplayReport(
        period=period,
        window_count=len(summaries),
        windows=summaries,
        events=tuple(events),
    )


def format_report(report: ReplayReport) -> str:
    """Serialize a report with stable key order for golden files."""
    return json.dumps(
        report.model_dump(mode="json", exclude_none=True),
        indent=2,
        sort_keys=True,
    )


def load_advertisement_fixture(path: Path) -> list[Advertisement]:
    """Load a JSON array of advertisements (capture or synthetic)."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TypeError(f"{path} is not a JSON array")
    return [Advertisement.model_validate(item) for item in raw]


def load_heard_fixture(path: Path) -> list[DetectionWindow]:
    """Load a JSON array of heard-window buckets for inside/crowd replay."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise TypeError(f"{path} is not a JSON array")
    windows: list[DetectionWindow] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise TypeError(f"{path}[{index}] is not an object")
        if "heard" not in item and (
            "address" in item or "rssi" in item or "timestamp" in item
        ):
            raise TypeError(
                f"{path}[{index}] looks like an Advertisement; "
                "use --backend approach (or heard-window JSON)"
            )
        heard_raw = item.get("heard", [])
        if not isinstance(heard_raw, list):
            raise TypeError(f"{path}[{index}].heard is not an array")
        heard: dict[int, int] = {}
        for pair in heard_raw:
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not isinstance(pair[0], int)
                or isinstance(pair[0], bool)
                or not isinstance(pair[1], int)
                or isinstance(pair[1], bool)
            ):
                raise TypeError(
                    f"{path}[{index}].heard entries must be [device_id, rssi]"
                )
            device_id, rssi = pair
            current = heard.get(device_id)
            if current is None or rssi > current:
                heard[device_id] = rssi
        windows.append(DetectionWindow(index=index, heard=heard))
    return windows


def replay_heard_fixture(
    path: str | Path,
    detector: Detector,
    *,
    require_nonempty_heard: bool = True,
) -> ReplayReport:
    """Replay a sanitized heard-window fixture through ``detector``.

    ``require_nonempty_heard`` defaults to ``True`` for inside (catches
    empty/misrouted fixtures). Crowd quiet corpora may be all-empty;
    the CLI passes ``False`` for ``--backend crowd``.
    """
    windows = load_heard_fixture(Path(path))
    if (
        require_nonempty_heard
        and windows
        and not any(window.heard for window in windows)
    ):
        raise ValueError(
            f"{path} has no heard entries; "
            "inside replay needs heard-window JSON"
        )
    period = DEFAULT_REPLAY_PERIOD
    return make_report(windows, replay(detector, windows), period=period)


def replay_fixture(
    path: str | Path,
    detector: Detector,
    period: float = DEFAULT_REPLAY_PERIOD,
) -> ReplayReport:
    """Replay a sanitized advertisement fixture through ``detector``."""
    windows = windows_from_advertisements(
        load_advertisement_fixture(Path(path)),
        period=period,
    )
    return make_report(windows, replay(detector, windows), period=period)


async def replay_snapshot(
    path: str | Path,
    site_id: str,
    detector: Detector,
    period: float = DEFAULT_REPLAY_PERIOD,
) -> ReplayReport:
    """Replay a read-only observations snapshot for one site.

    Opens ``path`` with ``connect_readonly`` (DC-9). The caller must
    pass a copied snapshot, not the live daemon database.

    Args:
        path: SQLite file (immutable copy).
        site_id: Rows for this site only.
        detector: Any ``Detector`` implementation.
        period: Bucket width in seconds.

    Returns:
        The JSON-serializable replay report.
    """
    import aiosqlite

    from blesentry.storage.database import connect_readonly
    from blesentry.storage.repository import ObservationRepository

    try:
        conn = await connect_readonly(path)
    except aiosqlite.Error as exc:
        raise ValueError(f"cannot open snapshot {path}: {exc}") from exc
    try:
        rows = await ObservationRepository(conn, site_id).list_ordered()
    except aiosqlite.Error as exc:
        raise ValueError(f"cannot read snapshot {path}: {exc}") from exc
    finally:
        await conn.close()
    windows = windows_from_observations(rows, period=period)
    return make_report(windows, replay(detector, windows), period=period)


def detector_for_backend(backend: str) -> Detector:
    """Build a configured Detector backend for replay/CLI."""
    from blesentry.config import (
        ApproachDetectionConfig,
        CrowdDetectionConfig,
        InsideDetectionConfig,
        MockDetectionConfig,
        NoneDetectionConfig,
        build_detector,
    )

    if backend == "mock":
        return build_detector(MockDetectionConfig(backend="mock"))
    if backend == "approach":
        return build_detector(ApproachDetectionConfig(backend="approach"))
    if backend == "inside":
        return build_detector(InsideDetectionConfig(backend="inside"))
    if backend == "crowd":
        return build_detector(CrowdDetectionConfig(backend="crowd"))
    if backend == "none":
        return build_detector(NoneDetectionConfig())
    raise ValueError(f"unknown detection backend: {backend}")
