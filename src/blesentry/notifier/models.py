# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Value objects crossing the Notifier seam.

All three are frozen and closed (``extra="forbid"``): a message, its
delivery outcome, and an authorized inbound command are immutable facts
handed across the seam, mirroring the Scanner seam's ``Advertisement``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OutboundMessage(BaseModel):
    """An alert the system wants delivered to the operator.

    Kept to plain ``text`` for v1. The interactive label flow (P2-6)
    extends this with reply markup — additive, so the ``send`` signature
    never changes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)


class DeliveryResult(BaseModel):
    """The outcome of one :meth:`Notifier.send` attempt.

    ``send`` never raises on a delivery failure — it returns
    ``ok=False`` so the outbox drain loop (P2-4) can drive backoff
    without a try/except around every send. ``retriable`` is only
    meaningful when ``ok`` is ``False``: ``True`` for transient faults
    (network, 429, 5xx) the drain loop should retry, ``False`` for
    permanent ones (bot blocked, chat gone) it should dead-letter.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    message_id: int | None = None
    error: str | None = None
    retriable: bool = True


class InboundCommand(BaseModel):
    """One authorized inbound message from the operator.

    Only messages that pass the ADR-0003 auth rule (``chat_id`` **and**
    ``user_id`` both match) are ever materialized as an
    ``InboundCommand``; the origin ids are carried so downstream
    handlers (P2-6/P2-8) can reply and audit without re-checking auth.
    ``text`` is verbatim operator input — never radio-controlled — but
    handlers still validate it as command input.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    chat_id: int
    user_id: int
    message_id: int
    text: str
