# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Inside detector spec helpers (I1 / ADR-0009).

Frozen knobs and pure math for adjacent-to-Pi sustain detection.
I2 owns own-gear exclusion wiring; I3 owns the Detector backend.
Reuses F3 :func:`~blesentry.detection.features.band_counts`.
"""

from __future__ import annotations

from collections.abc import Mapping

from blesentry.detection.features import DEFAULT_BANDS, BandEdges, band_counts

# Frozen knobs — ADR-0009 / docs/inside.md. I2/I3 import these;
# do not copy the integers into the backend module.
INSIDE_DETECTOR_ID = "inside"
INSIDE_KIND = "inside-adjacent"
INSIDE_FAR_PER_DAY = 1
INSIDE_SOURCE = "heard"
INSIDE_MIN_DEVICES = 1
INSIDE_SUSTAIN_WINDOWS = 8


def inside_count(
    heard: Mapping[int, int],
    *,
    excluded: frozenset[int] | set[int] = frozenset(),
    bands: BandEdges = DEFAULT_BANDS,
) -> int:
    """Return ``count_adjacent`` from a post-resolve heard map.

    ``excluded`` is the own-gear / familiar allow-list subtraction
    (I2 wires F6 + rotating own gear). This helper only applies the
    set; it does not resolve familiarity.
    """
    keyed = {
        str(device_id): rssi
        for device_id, rssi in heard.items()
        if device_id not in excluded
    }
    return band_counts(keyed, bands).count_adjacent


def inside_sustain_step(
    streak: int,
    count: int,
    *,
    min_devices: int = INSIDE_MIN_DEVICES,
    sustain_windows: int = INSIDE_SUSTAIN_WINDOWS,
) -> tuple[int, bool]:
    """Advance consecutive-window sustain state for one window.

    Args:
        streak: Prior consecutive windows with ``count >= min_devices``.
        count: This window's adjacent count (after exclusion).
        min_devices: Minimum adjacent identities to sustain (N).
        sustain_windows: Consecutive windows required to fire (M).

    Returns:
        ``(new_streak, fired)``. Resets to ``0`` when ``count`` is
        below ``min_devices``. Fires when the new streak reaches
        ``sustain_windows``; I3 owns fire-once per episode.
    """
    if not isinstance(streak, int) or isinstance(streak, bool):
        raise TypeError("streak must be int")
    if streak < 0:
        raise ValueError("streak must be >= 0")
    if not isinstance(min_devices, int) or isinstance(min_devices, bool):
        raise TypeError("min_devices must be int")
    if min_devices < 1:
        raise ValueError("min_devices must be >= 1")
    if not isinstance(sustain_windows, int) or isinstance(
        sustain_windows,
        bool,
    ):
        raise TypeError("sustain_windows must be int")
    if sustain_windows < 1:
        raise ValueError("sustain_windows must be >= 1")
    if count >= min_devices:
        new_streak = streak + 1
        return new_streak, new_streak >= sustain_windows
    return 0, False
