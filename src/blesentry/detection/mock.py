# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""MockDetector — the CI/replay double for the Detector seam.

Records every window it observes and replays scripted event batches
so F1 and unit tests can drive detectors without a live radio or a
real backend.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence

from blesentry.detection.models import DetectionEvent, DetectionWindow


class MockDetector:
    """In-memory Detector for tests and offline replay.

    Args:
        events: Per-``observe`` batches to return, in order. Once
            exhausted, further calls return an empty tuple (the window
            is still recorded).
    """

    def __init__(
        self,
        *,
        events: Sequence[Sequence[DetectionEvent]] | None = None,
    ) -> None:
        """Initialise with optional scripted event batches."""
        self.observed: list[DetectionWindow] = []
        self._events: deque[tuple[DetectionEvent, ...]] = deque(
            tuple(batch) for batch in (events or ())
        )

    def observe(self, window: DetectionWindow) -> tuple[DetectionEvent, ...]:
        """Record the window and return the next scripted batch.

        Args:
            window: The window under inspection.

        Returns:
            The next scripted batch, or empty once the script is spent.
        """
        self.observed.append(window)
        if self._events:
            return self._events.popleft()
        return ()
