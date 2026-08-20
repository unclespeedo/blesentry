# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""NullNotifier — the ``none`` backend's do-nothing implementation.

The default config selects ``backend = "none"``: a daemon that runs
without alerting. This null object satisfies the seam so the loop never
needs to branch on "is a notifier configured" — sends are discarded
(reported successful, so nothing queues forever) and no commands arrive.

Consequence to be aware of: the daemon always runs the drain, so with
``backend = "none"`` an enqueued alert is *discarded* and its outbox row
is marked DELIVERED. That is the deliberate default (the alternative —
letting messages accumulate forever — is worse on the SD-card target,
and retention is out of scope until #107). Configure a real backend to
actually deliver.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from blesentry.notifier.models import (
    DeliveryResult,
    InboundCommand,
    OutboundMessage,
)


class NullNotifier:
    """Discards outbound messages; never yields inbound commands."""

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        """Discard the message and report success."""
        return DeliveryResult(ok=True)

    async def commands(self) -> AsyncIterator[InboundCommand]:
        """Yield nothing — the disabled backend has no inbound side."""
        return
        yield  # pragma: no cover - makes this an async generator

    async def aclose(self) -> None:
        """No resources to release."""
