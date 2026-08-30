# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Familiar / resident baseline (F6).

Auto-learned allow-list of resolved ``device_id`` values seen across
≥K distinct UTC calendar days, plus every labeled device. Built at
startup and refreshed periodically — not per-cycle (DC-1). Bounded
auto-learn pool (DC-2). I2 wires ``is_familiar`` into detectors.
"""

from __future__ import annotations

from blesentry.storage.repository import (
    DeviceRepository,
    ObservationRepository,
    SiteStateRepository,
)

# Frozen knobs — docs/familiar.md. I2/C4 import these; do not fork.
FAMILIAR_MIN_DAYS = 3
FAMILIAR_MAX_DEVICES = 48
_FAMILIAR_REFRESH_MARKER = "familiar.last_refresh"


class FamiliarSet:
    """In-memory membership for ``is_familiar`` checks."""

    def __init__(self, device_ids: frozenset[int] = frozenset()) -> None:
        """Capture the current familiar ``device_id`` set."""
        self._device_ids = device_ids

    def is_familiar(self, device_id: int) -> bool:
        """Return whether ``device_id`` is in the familiar allow-list."""
        if not isinstance(device_id, int) or isinstance(device_id, bool):
            raise TypeError("device_id must be int")
        return device_id in self._device_ids

    def replace(self, device_ids: frozenset[int]) -> None:
        """Replace membership (used by :class:`FamiliarSetRefresher`)."""
        self._device_ids = device_ids


async def build_familiar_device_ids(
    devices: DeviceRepository,
    observations: ObservationRepository,
    *,
    min_days: int = FAMILIAR_MIN_DAYS,
    max_devices: int = FAMILIAR_MAX_DEVICES,
) -> frozenset[int]:
    """Build familiar ids from labeled devices and K-day observation history.

    Labeled devices are always included. Auto-learned ids fill the
    bounded pool (``max_devices``) by distinct-day count.
    """
    if not isinstance(min_days, int) or isinstance(min_days, bool):
        raise TypeError("min_days must be int")
    if min_days < 1:
        raise ValueError("min_days must be >= 1")
    if not isinstance(max_devices, int) or isinstance(max_devices, bool):
        raise TypeError("max_devices must be int")
    if max_devices < 1:
        raise ValueError("max_devices must be >= 1")
    labeled = frozenset(await devices.list_labeled_device_ids())
    auto_learned = await observations.list_devices_by_distinct_days(
        min_days,
        limit=max_devices,
        exclude_device_ids=tuple(labeled),
    )
    return labeled | frozenset(auto_learned)


class FamiliarSetRefresher:
    """Startup + daily refresh of :class:`FamiliarSet` from storage."""

    def __init__(
        self,
        devices: DeviceRepository,
        observations: ObservationRepository,
        *,
        site_state: SiteStateRepository | None = None,
        min_days: int = FAMILIAR_MIN_DAYS,
        max_devices: int = FAMILIAR_MAX_DEVICES,
    ) -> None:
        """Wire repositories for rebuild queries."""
        self._devices = devices
        self._observations = observations
        self._site_state = site_state
        self._min_days = min_days
        self._max_devices = max_devices
        self._familiar = FamiliarSet()

    @property
    def familiar(self) -> FamiliarSet:
        """Current familiar set (updated by :meth:`build` / refresh)."""
        return self._familiar

    async def build(self) -> None:
        """Rebuild membership from history (startup)."""
        ids = await build_familiar_device_ids(
            self._devices,
            self._observations,
            min_days=self._min_days,
            max_devices=self._max_devices,
        )
        self._familiar.replace(ids)

    async def refresh_if_due(self, now: str) -> bool:
        """Rebuild at most once per UTC calendar day.

        Returns:
            ``True`` when a rebuild ran; ``False`` when skipped.
        """
        if self._site_state is None:
            return False
        last = await self._site_state.get(_FAMILIAR_REFRESH_MARKER)
        today = now[:10]
        if last is not None and last[:10] == today:
            return False
        await self.build()
        await self._site_state.set(_FAMILIAR_REFRESH_MARKER, now)
        return True
