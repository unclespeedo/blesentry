# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""TelegramNotifier — the v1 production chat backend (ADR-0003).

A deliberately thin adapter over the Telegram Bot API: two endpoints
(``sendMessage`` and ``getUpdates``) reached with ``httpx``. **Long-poll
``getUpdates`` only, never a webhook** — an outbound HTTPS request works
behind the deployment's CGNAT/firewall with zero inbound surface.

Two security properties are structural here, not incidental:

* **Single-operator auth** (ADR-0003): every inbound update is checked
  against the configured ``chat_id`` **and** ``user_id`` via the shared
  :func:`blesentry.notifier.auth.is_authorized` predicate; a mismatch is
  dropped and logged, never surfaced as a command.
* **Untrusted rendering** (#85): messages are sent as **plain text**
  (no ``parse_mode``), so an adversarial advertised device name rendered
  into an alert cannot inject Telegram markup.

The bot token lives only in the API base URL held on the client; the
Bot API requires it in the request path, so it cannot be moved out. It
is never placed in a ``DeliveryResult``, and httpx's request logger —
which would otherwise log the full tokened URL at INFO — is muted to
WARNING so the secret never reaches a log line.

Resilience: the daemon runs unattended for months behind CGNAT, where
proxies and captive portals can return a 200 with a non-JSON body and
updates can be malformed. Both ``send`` and ``commands`` treat a bad
body or a malformed update as a handled, non-fatal event (one skipped
update, one retriable delivery) — never an exception that stops the
sentinel from alerting or listening.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import httpx

from blesentry.notifier.auth import is_authorized
from blesentry.notifier.models import (
    DeliveryResult,
    InboundCommand,
    OutboundMessage,
)

logger = logging.getLogger(__name__)

# Headroom over the server-side long-poll hold so the client read
# timeout never fires before getUpdates returns.
_POLL_READ_HEADROOM = 10.0

# An outbound alert wants a tight deadline of its own, not the long
# poll's generous read window: a stuck send should fail fast so the
# drain loop (P2-4) can back off and retry.
_SEND_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class TelegramNotifier:
    """Telegram Bot API backend (long-poll, single-operator)."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: int,
        user_id: int,
        poll_timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        error_backoff: float = 3.0,
    ) -> None:
        """Initialise with the operator pair and long-poll settings.

        Args:
            bot_token: Bot API token (secret; stays in the base URL).
            chat_id: The one authorized operator chat.
            user_id: The one authorized operator user.
            poll_timeout: Server-side ``getUpdates`` hold, in seconds.
            client: Injected HTTP client (tests supply a mock
                transport); a default async client is built otherwise.
            error_backoff: Seconds to wait before re-polling after a
                transient ``getUpdates`` failure.
        """
        self._chat_id = chat_id
        self._user_id = user_id
        self._poll_timeout = poll_timeout
        self._error_backoff = error_backoff
        self._api = f"https://api.telegram.org/bot{bot_token}"
        self._offset = 0
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(poll_timeout + _POLL_READ_HEADROOM)
        )
        # httpx logs every request URL at INFO, and the URL carries the
        # bot token; the daemon runs at INFO, so mute httpx's logger to
        # keep the secret out of the journal.
        logging.getLogger("httpx").setLevel(logging.WARNING)

    @property
    def chat_id(self) -> int:
        """The configured authorized operator chat id."""
        return self._chat_id

    @property
    def user_id(self) -> int:
        """The configured authorized operator user id."""
        return self._user_id

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        """Deliver one message as plain text; never raise on failure."""
        try:
            response = await self._client.post(
                f"{self._api}/sendMessage",
                json={"chat_id": self._chat_id, "text": message.text},
                timeout=_SEND_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "telegram sendMessage transport error: %s",
                type(exc).__name__,
            )
            return DeliveryResult(
                ok=False, error="transport-error", retriable=True
            )

        status = response.status_code
        if status == 200:
            try:
                data = response.json()
            except ValueError:
                # A proxy/captive portal returned 200 with a non-JSON
                # body; treat as a transient delivery failure, not a
                # crash (send must never raise — protocol contract).
                logger.warning("telegram sendMessage: non-JSON 200 body")
                return DeliveryResult(
                    ok=False, error="bad-response", retriable=True
                )
            if data.get("ok"):
                result = data.get("result") or {}
                return DeliveryResult(
                    ok=True, message_id=result.get("message_id")
                )
            logger.warning(
                "telegram sendMessage rejected: %s", data.get("description")
            )
            return DeliveryResult(ok=False, error="api-error", retriable=False)

        retriable = status == 429 or status >= 500
        logger.warning(
            "telegram sendMessage http %s: %s",
            status,
            _description(response),
        )
        return DeliveryResult(
            ok=False, error=f"http-{status}", retriable=retriable
        )

    async def commands(self) -> AsyncIterator[InboundCommand]:
        """Long-poll ``getUpdates`` and yield authorized commands.

        Runs until the consuming task is cancelled. Transient poll
        failures — transport errors, non-200 status, ``ok=false``, a
        non-JSON body, or a malformed result — are logged and retried
        after ``error_backoff`` rather than ending the stream; a
        sentinel that stops listening is as bad as one that stops
        scanning. A single malformed update is skipped, not fatal.

        Delivery is **at-least-once**: an update's offset is only
        advanced (acked to Telegram on the next poll) *after* the
        consumer resumes past the ``yield``. If the process dies while a
        handler is mid-work, the command is redelivered on restart
        rather than lost — the guarantee P2-6's own-task/own-connection
        handler needs. The offset is in-memory only (reset to 0 on
        start), consistent with at-least-once.
        """
        while True:
            try:
                response = await self._client.post(
                    f"{self._api}/getUpdates",
                    json={
                        "offset": self._offset,
                        "timeout": self._poll_timeout,
                        "allowed_updates": ["message"],
                    },
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "telegram getUpdates transport error: %s",
                    type(exc).__name__,
                )
                await asyncio.sleep(self._error_backoff)
                continue

            if response.status_code != 200:
                logger.warning(
                    "telegram getUpdates http %s", response.status_code
                )
                await asyncio.sleep(self._error_backoff)
                continue

            try:
                data = response.json()
            except ValueError:
                logger.warning("telegram getUpdates: non-JSON 200 body")
                await asyncio.sleep(self._error_backoff)
                continue

            if not data.get("ok"):
                logger.warning(
                    "telegram getUpdates api error: %s",
                    data.get("description"),
                )
                await asyncio.sleep(self._error_backoff)
                continue

            result = data.get("result")
            if not isinstance(result, list):
                logger.warning("telegram getUpdates: malformed result")
                await asyncio.sleep(self._error_backoff)
                continue

            for update in result:
                if not isinstance(update, dict):
                    continue
                uid = update.get("update_id")
                command = self._authorize(update)
                if command is None:
                    # Skipped/unauthorized: ack now so it never
                    # redelivers (poison-message guard).
                    if isinstance(uid, int):
                        self._offset = uid + 1
                    continue
                yield command
                # Authorized: ack only after the consumer resumes, so a
                # crash mid-handling redelivers rather than drops.
                if isinstance(uid, int):
                    self._offset = uid + 1

    async def aclose(self) -> None:
        """Close the underlying HTTP client (idempotent)."""
        await self._client.aclose()

    def _authorize(self, update: dict) -> InboundCommand | None:
        """Return the command for an authorized text message, else None.

        Unauthorized senders (either id mismatches) are dropped and
        logged; non-text and malformed updates are silently skipped.
        """
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        text = message.get("text")
        if not isinstance(text, str):
            return None
        message_id = message.get("message_id")
        if not isinstance(message_id, int):
            return None
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        origin_chat = chat.get("id")
        origin_user = sender.get("id")
        if origin_chat is None or origin_user is None:
            return None
        if not is_authorized(
            origin_chat_id=origin_chat,
            origin_user_id=origin_user,
            allowed_chat_id=self._chat_id,
            allowed_user_id=self._user_id,
        ):
            logger.warning(
                "ignored unauthorized inbound message (chat=%s user=%s)",
                origin_chat,
                origin_user,
            )
            return None
        return InboundCommand(
            chat_id=origin_chat,
            user_id=origin_user,
            message_id=message_id,
            text=text,
        )


def _description(response: httpx.Response) -> str:
    """Best-effort Telegram error description for local logs only.

    Telegram's own error text is token-free; kept out of
    ``DeliveryResult`` regardless (which carries only a status code).
    """
    try:
        return str(response.json().get("description", ""))
    except (ValueError, httpx.HTTPError):
        return ""
