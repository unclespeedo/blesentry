# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""NullDetector — the ``none`` backend's do-nothing implementation.

The default config selects ``backend = "none"``: a daemon that runs
without adaptive detection. This null object satisfies the seam so
callers never branch on "is a detector configured" — every window
yields no events.
"""

from __future__ import annotations

from blesentry.detection.models import DetectionEvent, DetectionWindow


class NullDetector:
    """Emits no events for any window."""

    def observe(self, window: DetectionWindow) -> tuple[DetectionEvent, ...]:
        """Return no events.

        Args:
            window: Ignored; present to satisfy :class:`Detector`.

        Returns:
            An empty tuple.
        """
        del window
        return ()
