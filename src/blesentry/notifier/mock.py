# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""MockNotifier — the CI/test double for the Notifier seam.

Records outbound messages, replays scripted delivery results (so the
drain loop's backoff can be exercised), and yields scripted inbound
commands (so the bot flow can be exercised) — all without a network or
a bot token.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Sequence

from blesentry.notifier.models import (
    DeliveryResult,
    InboundCommand,
    OutboundMessage,
)


class MockNotifier:
    """In-memory Notifier for tests.

    Args:
        inbound: Commands :meth:`commands` will yield, in order.
        results: Delivery results :meth:`send` returns, in order; once
            exhausted, ``send`` returns an auto-numbered ``ok`` result.
    """

    def __init__(
        self,
        *,
        inbound: Sequence[InboundCommand] | None = None,
        results: Sequence[DeliveryResult] | None = None,
    ) -> None:
        """Initialise with optional scripted inbound and send results."""
        self.sent: list[OutboundMessage] = []
        self.closed = False
        self._inbound: list[InboundCommand] = list(inbound or [])
        self._results: deque[DeliveryResult] = deque(results or [])
        self._next_id = 1

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        """Record the message and return the next scripted/auto result."""
        self.sent.append(message)
        if self._results:
            return self._results.popleft()
        result = DeliveryResult(ok=True, message_id=self._next_id)
        self._next_id += 1
        return result

    async def commands(self) -> AsyncIterator[InboundCommand]:
        """Yield each scripted inbound command in order."""
        for command in self._inbound:
            yield command

    async def aclose(self) -> None:
        """Mark the notifier closed (idempotent)."""
        self.closed = True
