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
    CROWD_MAD_FLOOR,
    CROWD_RESIDUAL_WINDOW,
    CROWD_ROLLING_WINDOWS,
    ewma_alpha,
    floored_mad,
)

BaselineTier = Literal["seasonal", "rolling"]

# Re-anchor install age when consecutive trusted samples jump farther than
# cold start in one step — no real 15 s cadence spans 168 h between windows.
_FORWARD_JUMP_HOURS = float(CROWD_COLD_START_HOURS)
# Max plausible gap between consecutive trusted scan windows (~1 day).
_MAX_TRUSTED_STEP_HOURS = 24.0


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
        """Start empty; first trusted ``observe`` seeds install time."""
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
        self._last_trusted_at: str | None = None
        self._trusted_operating_hours = 0.0
        self._episode_tier: BaselineTier | None = None

    def observe(
        self,
        count_near: int,
        observed_at: str,
        *,
        wall_clock_trusted: bool,
        in_episode: bool,
    ) -> BaselineStep:
        """Advance one window; return baseline, scale, and ``z``.

        Convenience wrapper around :meth:`preview` + :meth:`commit` with
        the same ``in_episode`` flag for both. C4 should call
        ``preview`` → ``cusum_positive`` → ``commit`` so the trigger
        window freezes once ``S > 0`` after CUSUM.

        Args:
            count_near: This window's near-band count (primary feature).
            observed_at: UTC ISO-8601 timestamp with ``Z`` suffix.
            wall_clock_trusted: Whether seasonal buckets may update now.
            in_episode: When ``True`` (CUSUM ``S > 0``), freeze EWMA and
                residual history updates.

        Returns:
            :class:`BaselineStep` for C4's CUSUM input.
        """
        self.begin_window(
            observed_at,
            wall_clock_trusted=wall_clock_trusted,
            in_episode=in_episode,
        )
        step = self.preview(
            count_near,
            observed_at,
            wall_clock_trusted=wall_clock_trusted,
            in_episode=in_episode,
        )
        self.finish_window(
            count_near,
            observed_at,
            wall_clock_trusted=wall_clock_trusted,
            in_episode=in_episode,
            tier=step.tier,
        )
        if not in_episode:
            tier = self._tier_for(wall_clock_trusted)
            return BaselineStep(
                baseline=step.baseline,
                scale=step.scale,
                z=step.z,
                tier=tier,
            )
        return step

    def preview(
        self,
        count_near: int,
        observed_at: str,
        *,
        wall_clock_trusted: bool,
        in_episode: bool,
    ) -> BaselineStep:
        """Compute baseline, scale, and ``z`` without mutating state.

        Call :meth:`begin_window` first so trusted-time accrual and
        hold-and-backfill run before the read.
        """
        count = _require_count(count_near)
        tier = self._tier_for_preview(wall_clock_trusted, in_episode)
        baseline = self._baseline_for(count, observed_at, tier)
        residual = float(count) - baseline
        scale = self._scale_for(
            residual,
            observed_at,
            tier,
            in_episode=in_episode,
        )
        z = residual / scale
        return BaselineStep(
            baseline=baseline,
            scale=scale,
            z=z,
            tier=tier,
        )

    def begin_window(
        self,
        observed_at: str,
        *,
        wall_clock_trusted: bool,
        in_episode: bool,
    ) -> None:
        """Trusted-time accrual, backfill drain/queue, and episode tier pin."""
        if in_episode:
            if self._episode_tier is None:
                self._episode_tier = self._tier_for(wall_clock_trusted)
        else:
            self._episode_tier = None
        if wall_clock_trusted:
            self._note_trusted_time(observed_at)
            if not in_episode:
                self._drain_backfill()

    def finish_window(
        self,
        count_near: int,
        observed_at: str,
        *,
        wall_clock_trusted: bool,
        in_episode: bool,
        tier: BaselineTier,
    ) -> None:
        """Apply EWMA and residual-history updates when not frozen."""
        if in_episode:
            return
        count = _require_count(count_near)
        if not wall_clock_trusted:
            self._backfill.append((observed_at, count))
        baseline = self._baseline_for(count, observed_at, tier)
        residual = float(count) - baseline
        self._record_residual(
            count,
            residual,
            observed_at,
            tier,
            wall_clock_trusted=wall_clock_trusted,
        )
        self._update(count, observed_at, wall_clock_trusted)

    def commit(
        self,
        count_near: int,
        observed_at: str,
        *,
        wall_clock_trusted: bool,
        in_episode: bool,
        tier: BaselineTier,
    ) -> None:
        """Queue untrusted samples and apply EWMA updates for one window."""
        self.finish_window(
            count_near,
            observed_at,
            wall_clock_trusted=wall_clock_trusted,
            in_episode=in_episode,
            tier=tier,
        )

    def _tier_for_preview(
        self,
        wall_clock_trusted: bool,
        in_episode: bool,
    ) -> BaselineTier:
        if in_episode:
            if self._episode_tier is not None:
                return self._episode_tier
            return self._tier_for(wall_clock_trusted)
        return self._tier_for(wall_clock_trusted)

    def _note_trusted_time(self, observed_at: str) -> None:
        """Anchor install age on trusted wall clock; heal clock steps."""
        if self._last_trusted_at is not None:
            step_hours = _hours_between(self._last_trusted_at, observed_at)
            if (
                step_hours < 0
                or step_hours >= _FORWARD_JUMP_HOURS
                or step_hours > _MAX_TRUSTED_STEP_HOURS
            ):
                self._reanchor_trusted_time(observed_at)
                return
            self._trusted_operating_hours += step_hours
        self._last_trusted_at = observed_at
        if self._install_at is None:
            self._install_at = observed_at
            return
        if _parse_utc(observed_at) < _parse_utc(self._install_at):
            self._reanchor_trusted_time(observed_at)

    def _reanchor_trusted_time(self, observed_at: str) -> None:
        """Reset install age after a backward or forward clock correction."""
        self._install_at = observed_at
        self._last_trusted_at = observed_at
        self._trusted_operating_hours = 0.0

    def _tier_for(
        self,
        wall_clock_trusted: bool,
    ) -> BaselineTier:
        if not wall_clock_trusted:
            return "rolling"
        if self._install_at is None:
            return "rolling"
        if self._trusted_operating_hours < CROWD_COLD_START_HOURS:
            return "rolling"
        return "seasonal"

    def _rolling_mean_or(self, count_near: int) -> float:
        if not self._rolling:
            return float(count_near)
        return sum(self._rolling) / len(self._rolling)

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
                return self._rolling_mean_or(count_near)
            return value
        return self._rolling_mean_or(count_near)

    def _scale_for(
        self,
        residual: float,
        observed_at: str,
        tier: BaselineTier,
        *,
        in_episode: bool,
    ) -> float:
        if tier == "seasonal":
            bucket = hour_of_week(observed_at)
            values = self._seasonal_residuals[bucket]
        else:
            values = self._rolling_residuals
        if in_episode:
            if not values:
                return CROWD_MAD_FLOOR
            return floored_mad(values)
        if not values:
            return floored_mad([residual])
        return floored_mad([*values, residual])

    def _seasonal_baseline_or_rolling(
        self,
        bucket: int,
        count_near: int,
    ) -> float:
        value = self._seasonal[bucket]
        if math.isnan(value):
            return self._rolling_mean_or(count_near)
        return value

    def _record_residual(
        self,
        count_near: int,
        residual: float,
        observed_at: str,
        tier: BaselineTier,
        *,
        wall_clock_trusted: bool,
    ) -> None:
        bucket = hour_of_week(observed_at)
        if tier == "seasonal":
            self._seasonal_residuals[bucket].append(residual)
        else:
            self._rolling_residuals.append(residual)
        if wall_clock_trusted and tier == "rolling":
            seasonal_baseline = self._seasonal_baseline_or_rolling(
                bucket,
                count_near,
            )
            seasonal_residual = float(count_near) - seasonal_baseline
            self._seasonal_residuals[bucket].append(seasonal_residual)

    def _update_seasonal_bucket(self, bucket: int, count: float) -> None:
        current = self._seasonal[bucket]
        if math.isnan(current):
            self._seasonal[bucket] = count
        else:
            self._seasonal[bucket] = current + self._alpha * (count - current)

    def _update(
        self,
        count_near: int,
        observed_at: str,
        wall_clock_trusted: bool,
    ) -> None:
        count = float(count_near)
        self._rolling.append(count)
        if wall_clock_trusted:
            bucket = hour_of_week(observed_at)
            self._update_seasonal_bucket(bucket, count)

    def _drain_backfill(self) -> None:
        if self._install_at is None:
            return
        while self._backfill:
            observed_at, count_near = self._backfill.popleft()
            bucket = hour_of_week(observed_at)
            count = float(count_near)
            baseline = self._seasonal_baseline_or_rolling(bucket, count_near)
            self._update_seasonal_bucket(bucket, count)
            self._seasonal_residuals[bucket].append(count - baseline)


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
