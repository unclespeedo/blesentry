# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Crowd online baseline model (C3 / ADR-0008).

Seasonal + rolling EWMA, floored-MAD scale, episode freeze, and
hold-and-backfill. C4's crowd backend calls :class:`CrowdBaseline`
each window; CUSUM stays in :mod:`blesentry.detection.crowd`.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from blesentry.detection.crowd import (
    CROWD_COLD_START_HOURS,
    CROWD_EWMA_SPAN,
    CROWD_HOUR_OF_WEEK_BUCKETS,
    CROWD_RESIDUAL_WINDOW,
    CROWD_ROLLING_WINDOWS,
    ewma_alpha,
    floored_mad,
)

BaselineTier = Literal["seasonal", "rolling"]


@dataclass(frozen=True, slots=True)
class BaselineStep:
    """One window's baseline, scale, standardized excess, and tier."""

    baseline: float
    scale: float
    z: float
    tier: BaselineTier


def hour_of_week(observed_at: str) -> int:
    """Map a UTC ISO timestamp to hour-of-week bucket ``0..167``."""
    dt = _parse_utc(observed_at)
    return dt.weekday() * 24 + dt.hour


class CrowdBaseline:
    """Online crowd baseline with seasonal and rolling tiers (DC-4)."""

    def __init__(self) -> None:
        """Start empty; first ``observe`` seeds install time."""
        self._alpha = ewma_alpha(CROWD_EWMA_SPAN)
        self._install_at: str | None = None
        self._seasonal = [math.nan] * CROWD_HOUR_OF_WEEK_BUCKETS
        self._rolling: deque[float] = deque(maxlen=CROWD_ROLLING_WINDOWS)
        self._seasonal_residuals = [
            deque(maxlen=CROWD_RESIDUAL_WINDOW)
            for _ in range(CROWD_HOUR_OF_WEEK_BUCKETS)
        ]
        self._rolling_residuals: deque[float] = deque(
            maxlen=CROWD_RESIDUAL_WINDOW,
        )
        self._backfill: deque[tuple[str, int]] = deque()

    def observe(
        self,
        count_near: int,
        observed_at: str,
        *,
        wall_clock_trusted: bool,
        in_episode: bool,
    ) -> BaselineStep:
        """Advance one window; return baseline, scale, and ``z``.

        Args:
            count_near: This window's near-band count (primary feature).
            observed_at: UTC ISO-8601 timestamp with ``Z`` suffix.
            wall_clock_trusted: Whether seasonal buckets may update now.
            in_episode: When ``True`` (CUSUM ``S > 0``), skip EWMA updates.

        Returns:
            :class:`BaselineStep` for C4's CUSUM input.
        """
        count = _require_count(count_near)
        if self._install_at is None:
            self._install_at = observed_at
        if wall_clock_trusted:
            self._drain_backfill(observed_at)
        elif not in_episode:
            self._backfill.append((observed_at, count))
        tier = self._select_tier(observed_at, wall_clock_trusted)
        baseline = self._baseline_for(count, observed_at, tier)
        residual = float(count) - baseline
        scale = self._scale_for(residual, observed_at, tier)
        z = residual / scale
        self._record_residual(residual, observed_at, tier)
        if not in_episode:
            self._update(count, observed_at, tier, wall_clock_trusted)
        return BaselineStep(
            baseline=baseline,
            scale=scale,
            z=z,
            tier=tier,
        )

    def _select_tier(
        self,
        observed_at: str,
        wall_clock_trusted: bool,
    ) -> BaselineTier:
        if not wall_clock_trusted:
            return "rolling"
        if self._install_at is None:
            return "rolling"
        age_hours = _hours_between(self._install_at, observed_at)
        if age_hours < CROWD_COLD_START_HOURS:
            return "rolling"
        return "seasonal"

    def _baseline_for(
        self,
        count_near: int,
        observed_at: str,
        tier: BaselineTier,
    ) -> float:
        if tier == "seasonal":
            bucket = hour_of_week(observed_at)
            value = self._seasonal[bucket]
            if math.isnan(value):
                return float(count_near)
            return value
        if not self._rolling:
            return float(count_near)
        return sum(self._rolling) / len(self._rolling)

    def _scale_for(
        self,
        residual: float,
        observed_at: str,
        tier: BaselineTier,
    ) -> float:
        if tier == "seasonal":
            bucket = hour_of_week(observed_at)
            values = self._seasonal_residuals[bucket]
        else:
            values = self._rolling_residuals
        if not values:
            return floored_mad([residual])
        return floored_mad([*values, residual])

    def _record_residual(
        self,
        residual: float,
        observed_at: str,
        tier: BaselineTier,
    ) -> None:
        if tier == "seasonal":
            bucket = hour_of_week(observed_at)
            self._seasonal_residuals[bucket].append(residual)
        else:
            self._rolling_residuals.append(residual)

    def _update(
        self,
        count_near: int,
        observed_at: str,
        tier: BaselineTier,
        wall_clock_trusted: bool,
    ) -> None:
        count = float(count_near)
        self._rolling.append(count)
        if tier == "seasonal" and wall_clock_trusted:
            bucket = hour_of_week(observed_at)
            current = self._seasonal[bucket]
            if math.isnan(current):
                self._seasonal[bucket] = count
            else:
                self._seasonal[bucket] = current + self._alpha * (
                    count - current
                )

    def _drain_backfill(self, now: str) -> None:
        if self._install_at is None:
            return
        if _hours_between(self._install_at, now) < CROWD_COLD_START_HOURS:
            return
        while self._backfill:
            observed_at, count_near = self._backfill.popleft()
            bucket = hour_of_week(observed_at)
            current = self._seasonal[bucket]
            count = float(count_near)
            if math.isnan(current):
                self._seasonal[bucket] = count
            else:
                self._seasonal[bucket] = current + self._alpha * (
                    count - current
                )


def _parse_utc(observed_at: str) -> datetime:
    if not isinstance(observed_at, str):
        raise TypeError("observed_at must be str")
    if not observed_at.endswith("Z"):
        raise ValueError("observed_at must be UTC with Z suffix")
    return datetime.fromisoformat(observed_at)


def _hours_between(start: str, end: str) -> float:
    delta = _parse_utc(end) - _parse_utc(start)
    return delta.total_seconds() / 3600.0


def _require_count(count_near: int) -> int:
    if isinstance(count_near, bool) or not isinstance(count_near, int):
        raise TypeError("count_near must be int")
    if count_near < 0:
        raise ValueError("count_near must be >= 0")
    return count_near
