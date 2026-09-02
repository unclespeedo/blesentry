# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Crowd Detector backend (C4 / ADR-0008).

``[detection] backend = "crowd"``. I/O-free ``observe`` wraps C3's
baseline and C1's CUSUM; the cycle consumer enqueues. Fire-once
per episode; CUSUM reset clears. No raw address on the event
(SECURITY.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta

from blesentry.detection.crowd import (
    CROWD_CUSUM_H,
    CROWD_CUSUM_K,
    CROWD_DETECTOR_ID,
    CROWD_KIND,
    crowd_counts,
    cusum_positive,
)
from blesentry.detection.crowd_baseline import CrowdBaseline
from blesentry.detection.features import DEFAULT_BANDS, BandEdges
from blesentry.detection.models import DetectionEvent, DetectionWindow

REPLAY_EPOCH = "2026-01-01T00:00:00.000Z"
WINDOW_PERIOD_SECONDS = 15


def _near_contributors(
    heard: Mapping[int, int],
    *,
    bands: BandEdges = DEFAULT_BANDS,
) -> tuple[int, ...]:
    threshold = bands.near
    return tuple(
        sorted(
            device_id for device_id, rssi in heard.items() if rssi >= threshold
        )
    )


def format_crowd_alert(event: DetectionEvent) -> str:
    """Plain-text operator line: near/all counts + roster; never metres.

    Args:
        event: A ``crowd-busy`` event with ``count``, ``count_all``,
            and ``contributors``.

    Returns:
        Snapshot-stable alert text.

    Raises:
        ValueError: required fields are missing.
    """
    if (
        event.count is None
        or event.count_all is None
        or not event.contributors
    ):
        raise ValueError(
            "crowd alert requires count, count_all, and contributors"
        )
    roster = ", ".join(
        f"device {device_id}" for device_id in event.contributors
    )
    return (
        f"Unusual site busyness: {event.count} near / "
        f"{event.count_all} total ({roster})."
    )


class CrowdDetector:
    """CUSUM crowd-busy detector: one event per sustained episode."""

    def __init__(self, *, wall_clock_trusted: bool = True) -> None:
        """Start with empty baseline, CUSUM, and fire-once state."""
        self._baseline = CrowdBaseline()
        self._cusum = 0.0
        self._alerted = False
        self._wall_clock_trusted = wall_clock_trusted
        self._observed_at: str | None = None

    def prepare_window(
        self,
        *,
        observed_at: str,
        wall_clock_trusted: bool | None = None,
    ) -> None:
        """Inject live wall-clock context before the next ``observe``."""
        self._observed_at = observed_at
        if wall_clock_trusted is not None:
            self._wall_clock_trusted = wall_clock_trusted

    def observe(self, window: DetectionWindow) -> tuple[DetectionEvent, ...]:
        """Update baseline + CUSUM; emit fire-once crowd-busy events.

        Reads ``heard`` only (ADR-0008 post-resolve). ``advertisements``
        is ignored so advertisement-only replay cannot silently drive
        this backend.

        Args:
            window: Clock-free scan window.

        Returns:
            Zero or one ``crowd-busy`` event. Empty is success.
        """
        heard = window.heard
        count_near, count_all = crowd_counts(heard)
        observed_at = self._observed_at_for_window(window.index)
        in_episode = self._cusum > 0.0

        self._baseline.begin_window(
            observed_at,
            wall_clock_trusted=self._wall_clock_trusted,
            in_episode=in_episode,
        )
        step = self._baseline.preview(
            count_near,
            observed_at,
            wall_clock_trusted=self._wall_clock_trusted,
            in_episode=in_episode,
        )
        self._cusum, fired = cusum_positive(
            self._cusum,
            step.z,
            k=CROWD_CUSUM_K,
            h=CROWD_CUSUM_H,
        )
        self._baseline.commit(
            count_near,
            observed_at,
            wall_clock_trusted=self._wall_clock_trusted,
            in_episode=self._cusum > 0.0,
            tier=step.tier,
        )
        self._observed_at = None

        if self._cusum == 0.0:
            self._alerted = False

        if not fired or self._alerted:
            return ()

        contributors = _near_contributors(heard)
        if count_near != len(contributors):
            raise ValueError("count_near must match contributor roster size")

        self._alerted = True
        return (
            DetectionEvent(
                detector=CROWD_DETECTOR_ID,
                kind=CROWD_KIND,
                window_index=window.index,
                count=count_near,
                count_all=count_all,
                contributors=contributors,
            ),
        )

    def _observed_at_for_window(self, index: int) -> str:
        if self._observed_at is not None:
            return self._observed_at
        base = datetime.fromisoformat(REPLAY_EPOCH)
        dt = base + timedelta(seconds=index * WINDOW_PERIOD_SECONDS)
        millis = dt.microsecond // 1000
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"
