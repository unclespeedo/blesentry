# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""NtfyNotifier — HTTP push backend (P4-7).

Publishes alerts with ``POST /{alert_topic}`` and receives operator
commands by polling ``GET /{command_topic}/json?poll=1`` with a
``since`` cursor (Unix time at subscribe, then message ids). Works
entirely over outbound HTTPS, like Telegram long-poll (ADR-0003
connectivity model) without a Bot API.

Authorization is topic-level: a Bearer token (when configured) must
match on publish and subscribe. Any ``message`` event on the command
topic is treated as an authorized operator command; configure separate
topics and server ACLs so only the operator can publish commands.

See ``docs/notifier.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping

import httpx

from blesentry.notifier.models import (
    DeliveryResult,
    InboundCommand,
    OutboundMessage,
)

logger = logging.getLogger(__name__)

_SEND_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_POLL_READ_HEADROOM = 10.0


def _ntfy_id_to_message_id(nid: str) -> int:
    """Map ntfy's string ids into SQLite-safe positive integers."""
    digest = hashlib.blake2b(nid.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


class NtfyNotifier:
    """ntfy.sh (or self-hosted) HTTP notifier."""

    def __init__(
        self,
        *,
        server: str,
        alert_topic: str,
        command_topic: str,
        operator_user_id: int,
        token: str | None = None,
        poll_timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
        error_backoff: float = 3.0,
    ) -> None:
        """Wire server base URL, topics, and operator audit id."""
        self._base = server.rstrip("/")
        self._alert_topic = alert_topic
        self._command_topic = command_topic
        self._operator_user_id = operator_user_id
        self._token = token
        self._poll_timeout = poll_timeout
        self._error_backoff = error_backoff
        self._since: str | None = None
        read = poll_timeout + _POLL_READ_HEADROOM
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(read, connect=5.0)
        )

    @property
    def operator_user_id(self) -> int:
        """Synthetic user id carried on inbound commands for audit."""
        return self._operator_user_id

    def _auth_headers(self) -> Mapping[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    async def send(self, message: OutboundMessage) -> DeliveryResult:
        """POST plain-text alert body; never raise on delivery failure."""
        url = f"{self._base}/{self._alert_topic}"
        try:
            response = await self._client.post(
                url,
                content=message.text.encode(),
                headers=self._auth_headers(),
                timeout=_SEND_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "ntfy publish transport error: %s", type(exc).__name__
            )
            return DeliveryResult(
                ok=False, error="transport-error", retriable=True
            )

        status = response.status_code
        if status == 200:
            message_id, ok = _parse_publish_id(response)
            if not ok:
                logger.warning("ntfy publish: non-JSON or malformed 200 body")
                return DeliveryResult(
                    ok=False, error="bad-response", retriable=True
                )
            return DeliveryResult(ok=True, message_id=message_id)

        retriable = status == 429 or status >= 500
        logger.warning("ntfy publish http %s", status)
        return DeliveryResult(
            ok=False, error=f"http-{status}", retriable=retriable
        )

    async def commands(self) -> AsyncIterator[InboundCommand]:
        """Poll the command topic JSON stream until cancelled."""
        if self._since is None:
            self._since = str(int(time.time()))
        url = f"{self._base}/{self._command_topic}/json"
        while True:
            try:
                response = await self._client.get(
                    url,
                    params={"poll": "1", "since": self._since},
                    headers=self._auth_headers(),
                    timeout=httpx.Timeout(
                        self._poll_timeout + _POLL_READ_HEADROOM,
                        connect=5.0,
                    ),
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "ntfy subscribe transport error: %s",
                    type(exc).__name__,
                )
                await asyncio.sleep(self._error_backoff)
                continue

            if response.status_code != 200:
                logger.warning("ntfy subscribe http %s", response.status_code)
                await asyncio.sleep(self._error_backoff)
                continue

            body = response.text
            if not body.strip():
                await asyncio.sleep(self._error_backoff)
                continue

            saw_command = False
            for line in body.splitlines():
                parsed = self._parse_command_line(line)
                if parsed is None:
                    continue
                command, raw_id = parsed
                yield command
                saw_command = True
                if raw_id is not None:
                    self._since = raw_id

            if not saw_command and body.strip():
                await asyncio.sleep(self._error_backoff)

    async def aclose(self) -> None:
        """Close the underlying HTTP client (idempotent)."""
        await self._client.aclose()

    def _parse_command_line(
        self, line: str
    ) -> tuple[InboundCommand, str | None] | None:
        line = line.strip()
        if not line:
            return None
        try:
            payload = json.loads(line)
        except ValueError:
            logger.warning("ntfy subscribe: non-JSON line")
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("event") != "message":
            return None
        text = payload.get("message")
        if not isinstance(text, str) or not text:
            return None
        raw_id = payload.get("id")
        message_id = (
            _ntfy_id_to_message_id(raw_id) if isinstance(raw_id, str) else 0
        )
        uid = self._operator_user_id
        ack = raw_id if isinstance(raw_id, str) else None
        return (
            InboundCommand(
                chat_id=uid,
                user_id=uid,
                message_id=message_id,
                text=text,
            ),
            ack,
        )


def _parse_publish_id(response: httpx.Response) -> tuple[int | None, bool]:
    try:
        data = response.json()
    except ValueError:
        return None, False
    if not isinstance(data, dict):
        return None, False
    raw_id = data.get("id")
    if isinstance(raw_id, str):
        return _ntfy_id_to_message_id(raw_id), True
    return None, True
