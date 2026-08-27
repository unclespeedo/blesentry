# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Online per-identity RSSI trajectory tracker (A2 / DC-2).

Bounded deques of last-W heard samples, F3 slope/span/dwell, A1
``is_rising_approach``. Not a Detector backend — A3 owns ``observe``
on the seam and will hold one of these.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from blesentry.detection.approach import (
    APPROACH_WINDOWS,
    is_rising_approach,
)
from blesentry.detection.features import (
    Source,
    max_rssi_by_identity,
    rssi_slope,
    rssi_span,
)
from blesentry.detection.models import DetectionWindow

TRACKER_MAX_ADDRESSES = 256
TRACKER_FADE_AFTER_WINDOWS = 12
TRACKER_MAX_SAMPLES = TRACKER_MAX_ADDRESSES * APPROACH_WINDOWS

__all__ = [
    "TRACKER_FADE_AFTER_WINDOWS",
    "TRACKER_MAX_ADDRESSES",
    "TRACKER_MAX_SAMPLES",
    "AddressTrajectory",
    "TrajectoryTracker",
]


class AddressTrajectory(BaseModel):
    """One identity's state at a window it was heard and admitted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str = Field(min_length=1)
    max_rssi: int
    slope: float | None = None
    span: int = Field(ge=0)
    dwell: int = Field(ge=1)
    visit_min: int
    first_seen_index: int = Field(ge=0)
    windows_seen: int = Field(ge=1)
    age_windows: int = Field(ge=1)
    rising: bool


def _require_int(value: object, *, name: str, minimum: int) -> int:
    """Reject bools, non-ints, and values below ``minimum``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


@dataclass
class _Track:
    """Mutable per-identity rolling RSSI (not a Pydantic model)."""

    samples: deque[tuple[int, int]] = field(init=False)
    visit_min: int = 0
    last_heard_index: int = 0
    first_seen_index: int = 0
    windows_seen: int = 0
    dwell: int = 0

    def __post_init__(self) -> None:
        """Allocate the bounded deque once at A1 W."""
        self.samples = deque(maxlen=APPROACH_WINDOWS)

    def push(self, index: int, rssi: int) -> None:
        """Append one heard sample and update counters."""
        if self.windows_seen == 0:
            self.first_seen_index = index
            self.visit_min = rssi
            self.dwell = 1
        else:
            self.visit_min = min(self.visit_min, rssi)
            if self.last_heard_index == index - 1:
                self.dwell += 1
            else:
                self.dwell = 1
        self.samples.append((index, rssi))
        self.last_heard_index = index
        self.windows_seen += 1

    def snapshot(self, identity: str, index: int) -> AddressTrajectory:
        """Freeze this window's view of the track."""
        rssis = [rssi for _index, rssi in self.samples]
        span = rssi_span(rssis)
        if span is None:
            raise RuntimeError("track snapshot with empty samples")
        return AddressTrajectory(
            identity=identity,
            max_rssi=rssis[-1],
            slope=rssi_slope(self.samples),
            span=span,
            dwell=self.dwell,
            visit_min=self.visit_min,
            first_seen_index=self.first_seen_index,
            windows_seen=self.windows_seen,
            age_windows=index - self.first_seen_index + 1,
            rising=is_rising_approach(self.samples),
        )


class TrajectoryTracker:
    """Bounded per-identity RSSI tracks for the approach detector."""

    def __init__(
        self,
        *,
        max_addresses: int = TRACKER_MAX_ADDRESSES,
        fade_after_windows: int = TRACKER_FADE_AFTER_WINDOWS,
    ) -> None:
        """Configure the hard cap and fade.

        Args:
            max_addresses: Hard cap on live tracks (DC-2).
            fade_after_windows: Evict when ``index - last_heard`` is at
                least this and the identity was not heard this window.
        """
        self._max_addresses = _require_int(
            max_addresses, name="max_addresses", minimum=1
        )
        self._fade_after = _require_int(
            fade_after_windows, name="fade_after_windows", minimum=1
        )
        self._tracks: dict[str, _Track] = {}
        self._last_index: int | None = None

    @property
    def sample_windows(self) -> int:
        """Deque maxlen — always A1 W, not a constructor knob."""
        return APPROACH_WINDOWS

    @property
    def tracked_count(self) -> int:
        """Live tracks after the last ``observe``."""
        return len(self._tracks)

    @property
    def sample_count(self) -> int:
        """RSSI samples across all deques (≤ cap × W)."""
        return sum(len(track.samples) for track in self._tracks.values())

    def observe(
        self,
        window: DetectionWindow,
        *,
        source: Source = "advertisements",
    ) -> tuple[AddressTrajectory, ...]:
        """Update tracks from ``window``; return this window's admitted.

        Args:
            window: Clock-free scan window. ``index`` must strictly
                increase across calls.
            source: F3 stream (default pre-fusion advertisements).

        Returns:
            Frozen snapshots, sorted by identity. Empty if nobody
            heard was admitted.

        Raises:
            ValueError: ``window.index`` is not strictly increasing,
                or ``source`` is unknown.
        """
        if self._last_index is not None and window.index <= self._last_index:
            raise ValueError(
                "window index must be strictly increasing "
                f"(got {window.index}, last {self._last_index})"
            )

        current = max_rssi_by_identity(window, source)
        self._last_index = window.index

        expired = [
            identity
            for identity, track in self._tracks.items()
            if identity not in current
            and window.index - track.last_heard_index >= self._fade_after
        ]
        for identity in expired:
            del self._tracks[identity]

        new_ids = [
            identity for identity in current if identity not in self._tracks
        ]
        overflow = len(self._tracks) + len(new_ids) - self._max_addresses
        if overflow > 0:
            victims = sorted(
                (
                    identity
                    for identity in self._tracks
                    if identity not in current
                ),
                key=lambda identity: (
                    self._tracks[identity].last_heard_index,
                    identity,
                ),
            )
            for victim in victims[:overflow]:
                del self._tracks[victim]
            room = self._max_addresses - len(self._tracks)
            if len(new_ids) > room:
                new_ids = sorted(
                    new_ids,
                    key=lambda identity: (-current[identity], identity),
                )[:room]
        admitted = set(new_ids)

        snapshots: list[AddressTrajectory] = []
        for identity, rssi in current.items():
            track = self._tracks.get(identity)
            if track is None:
                if identity not in admitted:
                    continue
                track = _Track()
                self._tracks[identity] = track
            track.push(window.index, rssi)
            snapshots.append(track.snapshot(identity, window.index))
        snapshots.sort(key=lambda row: row.identity)
        return tuple(snapshots)
