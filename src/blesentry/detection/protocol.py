# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Detector protocol — the adaptive-detection seam.

The ``Detector`` protocol is the single interface later approach /
crowd / inside backends (and the F1 replay harness) implement.
Concrete backends are selected via config
(:func:`blesentry.config.build_detector`) and never imported by name
outside the detection package.

ADR-0002 records Detector as a config-selected extension point;
ADR-0006 freezes this surface.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from blesentry.detection.models import DetectionEvent, DetectionWindow


@runtime_checkable
class Detector(Protocol):
    """Adaptive-detection seam: one window in, zero or more events out.

    ``observe`` is synchronous and must not perform I/O, touch a
    repository, or enqueue to the outbox. The scan-cycle consumer
    (later issues) enqueues from the returned events inside the cycle
    transaction.
    """

    def observe(self, window: DetectionWindow) -> Sequence[DetectionEvent]:
        """Inspect one window and return any would-be alerts.

        An empty window or a quiet site returns an empty sequence —
        that is success, not an error.

        Args:
            window: The clock-free scan window (advertisements + heard).

        Returns:
            Detection events for this window; empty if nothing fired.
        """
        ...
