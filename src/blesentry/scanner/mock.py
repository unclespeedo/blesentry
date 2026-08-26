# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""MockScanner — scripted fixture replay for CI and unit tests.

``MockScanner`` consumes a list of advertisement batches (one per
``scan()`` call) so tests can script device appear / disappear / MAC
rotation scenarios without touching real hardware.

For presence and detection tests that need a *signal profile* rather
than a static RSSI, ``from_rssi_sequences`` expands one prototype
``Advertisement`` per address into those batches: each sequence is
one RSSI (dBm) per scan window, and ``None`` means the device is
absent that window. That is how tests model near-threshold flicker,
a gradual approach/departure, or a brief spike (the car-pass-by
case) without cloning advertisements by hand.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
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

    To vary RSSI across windows without rebuilding each batch, use
    ``from_rssi_sequences``: pass a prototype advertisement per
    address and a sequence of RSSI values (or ``None`` for absent).
    Sequences may differ in length; the scenario list is as long as
    the longest sequence, and a device whose sequence has ended is
    omitted from later windows.
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

    @classmethod
    def from_rssi_sequences(
        cls,
        templates: Mapping[str, Advertisement],
        sequences: Mapping[str, Sequence[int | None]],
        *,
        window_dt: float = 0.0,
    ) -> MockScanner:
        """Expand per-device RSSI sequences into scan-window batches.

        Each sequence is one RSSI (dBm) per ``scan()`` window.
        ``None`` means the device is absent that window. Sequences
        may differ in length; the scenario list is as long as the
        longest sequence, and a device whose sequence has ended is
        omitted from later windows.

        Args:
            templates: Address → prototype ``Advertisement``. The
                dict key must equal ``Advertisement.address``.
            sequences: Address → RSSI-per-window (or ``None``).
                Addresses missing from *templates* raise
                ``ValueError``. Extra templates with no sequence
                are ignored.
            window_dt: Seconds added to the prototype timestamp per
                window index. ``0`` (the default) leaves timestamps
                unchanged.

        Returns:
            A ``MockScanner`` whose successive ``scan()`` calls
            replay the expanded batches.

        Raises:
            ValueError: A sequence address has no prototype, or a
                prototype's address does not match its dict key.
        """
        for address, advertisement in templates.items():
            if advertisement.address != address:
                raise ValueError(
                    f"templates key {address!r} does not match "
                    f"Advertisement.address {advertisement.address!r}"
                )
        for address in sequences:
            if address not in templates:
                raise ValueError(f"no prototype advertisement for {address!r}")

        window_count = max(
            (len(seq) for seq in sequences.values()),
            default=0,
        )
        scenarios: list[list[Advertisement]] = []
        for index in range(window_count):
            batch: list[Advertisement] = []
            for address, seq in sequences.items():
                if index >= len(seq):
                    continue
                rssi = seq[index]
                if rssi is None:
                    continue
                template = templates[address]
                update: dict[str, int | float] = {"rssi": rssi}
                if window_dt != 0.0:
                    update["timestamp"] = (
                        template.timestamp + index * window_dt
                    )
                batch.append(template.model_copy(update=update))
            scenarios.append(batch)
        return cls(scenarios=scenarios)

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
