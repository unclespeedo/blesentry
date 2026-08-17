# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""MockScanner — scripted fixture replay for CI and unit tests.

``MockScanner`` consumes a list of advertisement batches (one per
``scan()`` call) so tests can script device appear / disappear / MAC
rotation scenarios without touching real hardware.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

from blesentry.scanner.models import Advertisement


class MockScanner:
    """Fixture-replay scanner for test scenarios.

    Constructed with a list of *scenarios* — each element is a batch
    of ``Advertisement`` objects that will be returned by one
    ``scan()`` call.  Batches are consumed in FIFO order; once
    exhausted, ``scan()`` returns an empty list.

    Example::

        scanner = MockScanner(scenarios=[
            [ad_1, ad_2],   # first scan returns two devices
            [ad_1],         # second scan: one dropped out
            [],             # third scan: all gone
        ])
    """

    def __init__(
        self,
        scenarios: list[list[Advertisement]],
    ) -> None:
        """Initialise with scripted scan-result batches.

        Args:
            scenarios: One ``list[Advertisement]`` per ``scan()``
                call, consumed in FIFO order.
        """
        self._queue: deque[list[Advertisement]] = deque(scenarios)

    async def scan(
        self,
        duration: float,
    ) -> list[Advertisement]:
        """Return the next scripted batch of advertisements.

        Args:
            duration: Ignored by MockScanner (accepted for protocol
                conformance).

        Returns:
            The next batch of advertisements, or an empty list if all
            scenarios have been consumed.
        """
        if self._queue:
            return list(self._queue.popleft())
        return []

    @classmethod
    def from_corpus(
        cls,
        path: Path,
    ) -> MockScanner:
        """Load a JSON fixture corpus as a single-scan replay.

        The entire corpus is replayed in one ``scan()`` call — useful
        for tests that need a realistic batch of advertisements
        without scripting per-window behaviour.

        Args:
            path: Path to a JSON file containing a list of
                advertisement records (the P0-3 capture corpus
                format).

        Returns:
            A ``MockScanner`` that returns all advertisements on the
            first ``scan()`` call, then empty on subsequent calls.
        """
        import json

        raw = json.loads(path.read_text(encoding="utf-8"))
        ads = [Advertisement.model_validate(r) for r in raw]
        return cls(scenarios=[ads])
