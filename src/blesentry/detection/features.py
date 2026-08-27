# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Canonical detection feature vectors (F3).

Offline batch over ``DetectionWindow`` sequences. Formulas are pinned
in ``docs/features.md``. A2 reuses :func:`rssi_slope`, :func:`rssi_span`,
and :func:`band_counts` — do not fork them. Not a Detector backend.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from blesentry.detection.models import DetectionWindow

type Source = Literal["advertisements", "heard"]

DEFAULT_SLOPE_WINDOWS = 8


class BandEdges(BaseModel):
    """Inclusive RSSI lower bounds (dBm). Higher is closer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adjacent: int = -55
    near: int = -70
    far: int = -80

    @model_validator(mode="after")
    def _ordered(self) -> BandEdges:
        """Require adjacent > near > far (closer = larger dBm)."""
        if not (self.adjacent > self.near > self.far):
            raise ValueError("band edges must satisfy adjacent > near > far")
        return self


DEFAULT_BANDS = BandEdges()


class BandCounts(BaseModel):
    """Inclusive nested identity counts for one window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    count_all: int = Field(ge=0)
    count_far: int = Field(ge=0)
    count_near: int = Field(ge=0)
    count_adjacent: int = Field(ge=0)

    @model_validator(mode="after")
    def _nested(self) -> BandCounts:
        """Require adjacent ⊆ near ⊆ far ⊆ all."""
        if not (
            self.count_adjacent
            <= self.count_near
            <= self.count_far
            <= self.count_all
        ):
            raise ValueError(
                "band counts must satisfy adjacent <= near <= far <= all"
            )
        return self


class IdentityFeatures(BaseModel):
    """Per-identity trajectory at one window (heard this window)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    identity: str = Field(min_length=1)
    max_rssi: int
    slope: float | None = None
    span: int = Field(ge=0)
    dwell: int = Field(ge=1)
    first_seen_index: int = Field(ge=0)
    age_windows: int = Field(ge=1)
    windows_seen: int = Field(ge=1)
    duty: float = Field(gt=0, le=1)


class WindowFeatures(BaseModel):
    """Aggregates plus per-identity rows for one window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    source: Source
    count_all: int = Field(ge=0)
    count_far: int = Field(ge=0)
    count_near: int = Field(ge=0)
    count_adjacent: int = Field(ge=0)
    appeared: int = Field(ge=0)
    disappeared: int = Field(ge=0)
    churn: int = Field(ge=0)
    identities: tuple[IdentityFeatures, ...] = ()

    @model_validator(mode="after")
    def _churn_matches(self) -> WindowFeatures:
        """Require churn == appeared + disappeared and nested bands."""
        expected = self.appeared + self.disappeared
        if self.churn != expected:
            raise ValueError(
                f"churn {self.churn} != appeared+disappeared {expected}"
            )
        if not (
            self.count_adjacent
            <= self.count_near
            <= self.count_far
            <= self.count_all
        ):
            raise ValueError(
                "band counts must satisfy adjacent <= near <= far <= all"
            )
        return self


def _require_slope_windows(value: object) -> int:
    """Reject non-ints, bools, and a window too short to slope."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("slope_windows must be int")
    if value < 2:
        raise ValueError("slope_windows must be >= 2")
    return value


def max_rssi_by_identity(
    window: DetectionWindow,
    source: Source,
) -> dict[str, int]:
    """Best RSSI per identity in ``window`` for ``source``.

    Args:
        window: One scan window.
        source: ``advertisements`` (address) or ``heard`` (device id).

    Returns:
        Identity string → max RSSI (dBm).

    Raises:
        ValueError: Unknown ``source``.
    """
    if source == "advertisements":
        out: dict[str, int] = {}
        for advertisement in window.advertisements:
            previous = out.get(advertisement.address)
            if previous is None or advertisement.rssi > previous:
                out[advertisement.address] = advertisement.rssi
        return out
    if source == "heard":
        return {
            str(device_id): rssi for device_id, rssi in window.heard.items()
        }
    raise ValueError(f"unknown feature source: {source}")


def band_counts(
    max_rssi: Mapping[str, int],
    bands: BandEdges = DEFAULT_BANDS,
) -> BandCounts:
    """Inclusive nested counts of identities by RSSI band.

    Args:
        max_rssi: Per-identity best RSSI this window.
        bands: Inclusive lower bounds (dBm).

    Returns:
        ``count_adjacent ≤ count_near ≤ count_far ≤ count_all``.
    """
    values = list(max_rssi.values())
    return BandCounts(
        count_all=len(values),
        count_far=sum(1 for rssi in values if rssi >= bands.far),
        count_near=sum(1 for rssi in values if rssi >= bands.near),
        count_adjacent=sum(1 for rssi in values if rssi >= bands.adjacent),
    )


def rssi_slope(points: Sequence[tuple[int, int]]) -> float | None:
    """OLS slope of ``(window_index, rssi)`` in dBm per window index.

    Missed windows are omitted by the caller (DC-5). ``None`` when
    there are fewer than two points or x-variance is zero.

    Args:
        points: Heard samples ``(index, rssi)`` in index order.

    Returns:
        Slope, or ``None`` if it is not defined.
    """
    n = len(points)
    if n < 2:
        return None
    x_mean = sum(index for index, _rssi in points) / n
    y_mean = sum(rssi for _index, rssi in points) / n
    denom = sum((index - x_mean) ** 2 for index, _rssi in points)
    if denom == 0:
        return None
    numer = sum((index - x_mean) * (rssi - y_mean) for index, rssi in points)
    return numer / denom


def rssi_span(rssis: Sequence[int]) -> int | None:
    """``max − min`` over RSSI samples; ``0`` for one point.

    Args:
        rssis: RSSI values (dBm).

    Returns:
        Non-negative span, or ``None`` if ``rssis`` is empty.
    """
    if not rssis:
        return None
    return max(rssis) - min(rssis)


def extract_features(
    windows: Sequence[DetectionWindow],
    *,
    source: Source = "advertisements",
    slope_windows: int = DEFAULT_SLOPE_WINDOWS,
    bands: BandEdges = DEFAULT_BANDS,
) -> tuple[WindowFeatures, ...]:
    """Build one feature vector per window (including quiet gaps).

    Args:
        windows: Clock-free windows in strictly increasing unique
            ``index`` order (caller-owned; F1 replay and the live
            cycle both produce that). Out-of-order input is undefined.
        source: Which stream to read (see ``docs/features.md``).
        slope_windows: Trailing heard-sample window W for slope/span.
        bands: Inclusive RSSI band edges.

    Returns:
        Frozen vectors, one per input window.

    Raises:
        TypeError: ``slope_windows`` is not an ``int``.
        ValueError: ``slope_windows < 2`` or unknown ``source``.
    """
    slope_windows = _require_slope_windows(slope_windows)
    if source not in ("advertisements", "heard"):
        raise ValueError(f"unknown feature source: {source}")

    history: dict[str, list[tuple[int, int]]] = {}
    first_seen: dict[str, int] = {}
    last_heard_index: dict[str, int] = {}
    dwell_streak: dict[str, int] = {}
    previous: set[str] = set()
    rows: list[WindowFeatures] = []

    for window in windows:
        current = max_rssi_by_identity(window, source)
        current_ids = set(current)
        appeared = len(current_ids - previous)
        disappeared = len(previous - current_ids)
        counts = band_counts(current, bands)

        identities: list[IdentityFeatures] = []
        for identity in sorted(current_ids):
            rssi = current[identity]
            samples = history.setdefault(identity, [])
            samples.append((window.index, rssi))
            first_seen.setdefault(identity, window.index)
            last = last_heard_index.get(identity)
            # Consecutive-index streak, O(1) per identity (not a rescan).
            if last is not None and last == window.index - 1:
                dwell = dwell_streak[identity] + 1
            else:
                dwell = 1
            dwell_streak[identity] = dwell
            last_heard_index[identity] = window.index
            rolling = samples[-slope_windows:]
            rssis = [sample[1] for sample in rolling]
            span = max(rssis) - min(rssis)
            first = first_seen[identity]
            age_windows = window.index - first + 1
            windows_seen = len(samples)
            identities.append(
                IdentityFeatures(
                    identity=identity,
                    max_rssi=rssi,
                    slope=rssi_slope(rolling),
                    span=span,
                    dwell=dwell,
                    first_seen_index=first,
                    age_windows=age_windows,
                    windows_seen=windows_seen,
                    duty=windows_seen / age_windows,
                )
            )

        rows.append(
            WindowFeatures(
                index=window.index,
                source=source,
                count_all=counts.count_all,
                count_far=counts.count_far,
                count_near=counts.count_near,
                count_adjacent=counts.count_adjacent,
                appeared=appeared,
                disappeared=disappeared,
                churn=appeared + disappeared,
                identities=tuple(identities),
            )
        )
        previous = current_ids

    return tuple(rows)
