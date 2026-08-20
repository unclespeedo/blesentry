# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Per-device presence state machine (P2-1).

``PresenceTracker`` turns a stream of scan windows into ABSENT↔PRESENT
transitions per device, debounced so transient signals never register:

* **PRESENT** after ``appear_windows`` *consecutive* windows in which the
  device is heard at or above ``rssi_threshold``.
* **ABSENT** after ``disappear_windows`` consecutive windows in which it
  is not heard (or heard only below threshold).
* A per-device **cooldown** (in windows) suppresses re-emitting a rapid
  re-PRESENT — a device that leaves and returns within
  ``cooldown_windows`` of going ABSENT is one visit, not two events.
  Because reconfirming PRESENT itself takes ``appear_windows``, set
  ``cooldown_windows`` greater than ``appear_windows`` for it to have
  any effect on a device that returns immediately.

This is the v1 false-positive defense: a car driving past (a strong
signal for 1–2 windows) never reaches ``appear_windows`` consecutive
hits, so it produces no transition and nothing for the alert layer to
fire on. Deciding *which* transitions alert an operator (known vs
unknown, visit mode) is the notifier/bot layer's job (P2-6); this module
only tracks presence and emits transitions.

The tracker is window-driven and clock-free: thresholds are counted in
windows, and the caller stamps each transition with the window's time.
Thresholds are constructor parameters; config wiring is P2-2.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple


class PresenceState(Enum):
    """A device's debounced presence state."""

    ABSENT = "ABSENT"
    PRESENT = "PRESENT"


class PresenceTransition(NamedTuple):
    """A device changing state in a given window."""

    device_id: int
    state: PresenceState


@dataclass
class _DeviceState:
    """Mutable per-device bookkeeping (internal)."""

    state: PresenceState
    hits: int
    misses: int
    cooldown_remaining: int
    present_emitted: bool


class PresenceTracker:
    """Debounced ABSENT↔PRESENT state machine over scan windows."""

    def __init__(
        self,
        *,
        appear_windows: int = 3,
        disappear_windows: int = 3,
        rssi_threshold: int = -80,
        cooldown_windows: int = 0,
        prune_after_windows: int | None = None,
    ) -> None:
        """Configure the debounce and cooldown thresholds (in windows).

        Args:
            appear_windows: Consecutive above-threshold windows to reach
                PRESENT (>= 1).
            disappear_windows: Consecutive missed windows to reach ABSENT
                (>= 1).
            rssi_threshold: Minimum RSSI (dBm) to count a window as a
                hit; weaker signals count as a miss.
            cooldown_windows: Suppress re-emitting PRESENT for this many
                windows after an emitted PRESENT (0 = no suppression).
            prune_after_windows: Drop an ABSENT device from memory after
                this many consecutive misses, bounding tracker memory on
                the constrained target. Defaults to ``4 *
                disappear_windows``.
        """
        if appear_windows < 1 or disappear_windows < 1:
            raise ValueError("appear/disappear windows must be >= 1")
        if cooldown_windows < 0:
            raise ValueError("cooldown_windows must be >= 0")
        self._appear = appear_windows
        self._disappear = disappear_windows
        self._threshold = rssi_threshold
        self._cooldown = cooldown_windows
        self._prune_after = (
            prune_after_windows
            if prune_after_windows is not None
            else 4 * disappear_windows
        )
        self._devices: dict[int, _DeviceState] = {}

    def update(self, heard: Mapping[int, int]) -> list[PresenceTransition]:
        """Advance one scan window; return the transitions it produced.

        Args:
            heard: ``device_id -> best RSSI`` for devices observed this
                window. A device absent from the map, or present with an
                RSSI below ``rssi_threshold``, counts as a miss.

        Returns:
            The PRESENT/ABSENT transitions emitted this window, in
            device-id order.
        """
        seen = {
            device_id
            for device_id, rssi in heard.items()
            if rssi >= self._threshold
        }
        for device_id in seen:
            if device_id not in self._devices:
                self._devices[device_id] = _DeviceState(
                    state=PresenceState.ABSENT,
                    hits=0,
                    misses=0,
                    cooldown_remaining=0,
                    present_emitted=False,
                )

        transitions: list[PresenceTransition] = []
        pruned: list[int] = []
        for device_id in sorted(self._devices):
            device = self._devices[device_id]
            # The cooldown counts down from the device's last ABSENT.
            if device.cooldown_remaining > 0:
                device.cooldown_remaining -= 1
            if device_id in seen:
                device.hits += 1
                device.misses = 0
                transition = self._maybe_present(device_id, device)
            else:
                device.misses += 1
                device.hits = 0
                transition = self._maybe_absent(device_id, device)
                if transition is None and self._should_prune(device):
                    pruned.append(device_id)
            if transition is not None:
                transitions.append(transition)

        for device_id in pruned:
            del self._devices[device_id]
        return transitions

    def _maybe_present(
        self, device_id: int, device: _DeviceState
    ) -> PresenceTransition | None:
        if device.state is PresenceState.PRESENT:
            return None
        if device.hits < self._appear:
            return None
        device.state = PresenceState.PRESENT
        device.hits = 0
        if device.cooldown_remaining == 0:
            device.present_emitted = True
            return PresenceTransition(device_id, PresenceState.PRESENT)
        # Returned within cooldown of leaving — same visit, not a new one.
        device.present_emitted = False
        return None

    def _maybe_absent(
        self, device_id: int, device: _DeviceState
    ) -> PresenceTransition | None:
        if device.state is not PresenceState.PRESENT:
            return None
        if device.misses < self._disappear:
            return None
        device.state = PresenceState.ABSENT
        # misses keeps counting while absent (until a hit resets it), so
        # pruning is a simple monotonic threshold for absent devices.
        # Start the cooldown: a return within this many windows is the
        # same visit. Re-armed on every departure, so a device pacing at
        # the boundary stays one visit while it keeps returning.
        device.cooldown_remaining = self._cooldown
        emitted = device.present_emitted
        device.present_emitted = False
        # Only pair an ABSENT with a PRESENT that was actually emitted.
        if emitted:
            return PresenceTransition(device_id, PresenceState.ABSENT)
        return None

    def _should_prune(self, device: _DeviceState) -> bool:
        # Never prune a device whose cooldown is still active — dropping
        # it would forget the cooldown and let a return re-emit PRESENT.
        return (
            device.state is PresenceState.ABSENT
            and device.misses >= self._prune_after
            and device.cooldown_remaining == 0
        )
