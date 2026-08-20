# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Notifier protocol — the chat-platform seam.

The ``Notifier`` protocol is the single interface the rest of blesentry
uses to alert the operator and receive commands back. Concrete backends
(Telegram for production, mock/null for tests and the disabled state)
are selected via config (:func:`blesentry.config.build_notifier`) and
never imported by name outside the notifier package.

ADR-0002 (extension-point architecture) records this seam; ADR-0003
locks Telegram as the v1 platform behind it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from blesentry.notifier.models import (
    DeliveryResult,
    InboundCommand,
    OutboundMessage,
)


@runtime_checkable
class Notifier(Protocol):
    """Chat-platform seam: outbound alerts and inbound commands.

    Every backend implements this protocol; swapping platforms
    (Telegram → Discord/ntfy, P4-7) is a config change, not a code
    change. The seam is transport-agnostic: nothing here mentions
    long polling, tokens, or HTTP.
    """

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        """Deliver one message to the operator.

        Must not raise on a delivery failure: transport and API errors
        are reported as ``DeliveryResult(ok=False, ...)`` so the outbox
        drain loop (P2-4) drives retry/backoff from the return value.
        Programmer errors and task cancellation still propagate.

        Args:
            message: The alert to deliver.

        Returns:
            The delivery outcome (success carries the platform message
            id; failure carries a redacted error and retriability).
        """
        ...

    def commands(self) -> AsyncIterator[InboundCommand]:
        """Yield authorized inbound commands until cancelled.

        Only messages passing the ADR-0003 single-operator auth rule
        are yielded; unauthorized senders are dropped and logged, never
        surfaced. Runs until the consuming task is cancelled (SIGTERM);
        implementations release transport resources on cancellation.

        Returns:
            An async iterator of authorized :class:`InboundCommand`.
        """
        ...

    async def aclose(self) -> None:
        """Release transport resources (HTTP client, sockets).

        Idempotent. Called on daemon shutdown after the ``commands``
        consumer and any in-flight ``send`` have been cancelled.
        """
        ...
