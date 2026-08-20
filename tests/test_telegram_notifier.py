# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""TelegramNotifier tests (P2-5): long-poll, auth filter, delivery.

The live round-trip is the issue's HUMAN VERIFY box; everything here
runs offline against an ``httpx.MockTransport`` standing in for the Bot
API — no token, no network. These pin the ADR-0003 single-operator auth
rule on the inbound path and the delivery/retriability contract the
drain loop (P2-4) consumes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
import pytest

from blesentry.config import TelegramNotifierConfig, build_notifier
from blesentry.notifier.models import OutboundMessage
from blesentry.notifier.protocol import Notifier
from blesentry.notifier.telegram import TelegramNotifier

CHAT = 100
USER = 200
Handler = Callable[[httpx.Request], httpx.Response]


def _make(
    handler: Handler,
    *,
    bot_token: str = "T",
    chat_id: int = CHAT,
    user_id: int = USER,
) -> TelegramNotifier:
    """Build a notifier whose HTTP client is the mock transport."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TelegramNotifier(
        bot_token=bot_token,
        chat_id=chat_id,
        user_id=user_id,
        client=client,
        error_backoff=0.0,
    )


def _msg_update(
    *,
    update_id: int,
    message_id: int,
    chat: int = CHAT,
    user: int = USER,
    text: str = "/status",
) -> dict[str, object]:
    """One Telegram ``message`` update as the Bot API would send it."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": user},
            "chat": {"id": chat},
            "text": text,
        },
    }


def _updates(*updates: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": list(updates)})


def _sent_ok(message_id: int = 1) -> httpx.Response:
    """A successful ``sendMessage`` response carrying a message id."""
    return httpx.Response(
        200, json={"ok": True, "result": {"message_id": message_id}}
    )


# --- protocol conformance -------------------------------------------


async def test_telegram_notifier_satisfies_protocol() -> None:
    notifier = _make(lambda req: _updates())
    assert isinstance(notifier, Notifier)
    await notifier.aclose()


# --- send: success --------------------------------------------------


async def test_send_success_returns_message_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _sent_ok(42)

    notifier = _make(handler)
    result = await notifier.send(OutboundMessage(text="hello"))
    await notifier.aclose()

    assert result.ok
    assert result.message_id == 42
    assert seen[0].url.path == "/botT/sendMessage"
    body = json.loads(seen[0].content)
    assert body["chat_id"] == CHAT
    assert body["text"] == "hello"


async def test_send_uses_plain_text_no_parse_mode() -> None:
    """Untrusted device names (#85) must not be parsed as markup."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _sent_ok()

    notifier = _make(handler)
    await notifier.send(OutboundMessage(text="*not* _markup_ [x](y)"))
    await notifier.aclose()

    body = json.loads(seen[0].content)
    assert "parse_mode" not in body


# --- send: failure classification -----------------------------------


@pytest.mark.parametrize(
    ("status", "retriable"),
    [(403, False), (400, False), (429, True), (500, True), (503, True)],
)
async def test_send_http_error_classifies_retriability(
    status: int, retriable: bool
) -> None:
    notifier = _make(
        lambda req: httpx.Response(
            status, json={"ok": False, "description": "x"}
        )
    )
    result = await notifier.send(OutboundMessage(text="hi"))
    await notifier.aclose()

    assert not result.ok
    assert result.retriable is retriable


async def test_send_transport_error_is_retriable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    notifier = _make(handler)
    result = await notifier.send(OutboundMessage(text="hi"))
    await notifier.aclose()

    assert not result.ok
    assert result.retriable is True


async def test_send_error_never_contains_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    notifier = _make(handler, bot_token="SUPERSECRETTOKEN")
    result = await notifier.send(OutboundMessage(text="hi"))
    await notifier.aclose()

    assert "SUPERSECRETTOKEN" not in str(result.error)


async def test_send_non_json_200_is_retriable_failure() -> None:
    """A proxy 200 with an HTML body must not raise (protocol contract)."""
    notifier = _make(
        lambda req: httpx.Response(200, text="<html>captive portal</html>")
    )
    result = await notifier.send(OutboundMessage(text="hi"))
    await notifier.aclose()

    assert not result.ok
    assert result.retriable is True


async def test_httpx_request_logger_is_muted() -> None:
    """The httpx logger prints the tokened URL at INFO; mute it."""
    notifier = _make(lambda req: _sent_ok())
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    await notifier.aclose()


# --- commands: auth filter (ADR-0003) -------------------------------


async def test_commands_yields_authorized_message() -> None:
    notifier = _make(
        lambda req: _updates(_msg_update(update_id=5, message_id=10))
    )
    got = []
    async for cmd in notifier.commands():
        got.append(cmd)
        break
    await notifier.aclose()

    assert len(got) == 1
    assert got[0].message_id == 10
    assert got[0].text == "/status"


async def test_commands_rejects_wrong_chat_id(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _updates(
            _msg_update(update_id=1, message_id=1, chat=999),
            _msg_update(update_id=2, message_id=2),
        )

    notifier = _make(handler)
    with caplog.at_level("WARNING"):
        got = []
        async for cmd in notifier.commands():
            got.append(cmd)
            break
    await notifier.aclose()

    assert [c.message_id for c in got] == [2]  # the wrong-chat one skipped
    assert "unauthorized" in caplog.text.lower()


async def test_commands_rejects_wrong_user_id(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _updates(
            _msg_update(update_id=1, message_id=1, user=999),
            _msg_update(update_id=2, message_id=2),
        )

    notifier = _make(handler)
    with caplog.at_level("WARNING"):
        got = []
        async for cmd in notifier.commands():
            got.append(cmd)
            break
    await notifier.aclose()

    assert [c.message_id for c in got] == [2]  # wrong-user one skipped
    assert "unauthorized" in caplog.text.lower()


async def test_commands_skips_non_text_updates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        no_text = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "from": {"id": USER},
                "chat": {"id": CHAT},
            },
        }
        return _updates(no_text, _msg_update(update_id=2, message_id=2))

    notifier = _make(handler)
    got = []
    async for cmd in notifier.commands():
        got.append(cmd)
        break
    await notifier.aclose()

    assert [c.message_id for c in got] == [2]


# --- commands: long-poll mechanics ----------------------------------


async def test_commands_acks_offset_and_scopes_updates() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return _updates(_msg_update(update_id=5, message_id=10))
        return _updates(_msg_update(update_id=8, message_id=11))

    notifier = _make(handler)
    got = []
    async for cmd in notifier.commands():
        got.append(cmd)
        if len(got) == 2:
            break
    await notifier.aclose()

    assert [c.message_id for c in got] == [10, 11]
    assert bodies[0]["offset"] == 0
    assert bodies[1]["offset"] == 6  # 5 + 1: the batch is acked
    assert bodies[0]["allowed_updates"] == ["message"]
    assert "timeout" in bodies[0]


async def test_commands_survives_transient_error() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500, json={"ok": False})
        return _updates(_msg_update(update_id=5, message_id=10))

    notifier = _make(handler)
    got = []
    async for cmd in notifier.commands():
        got.append(cmd)
        break
    await notifier.aclose()

    assert len(got) == 1
    assert len(calls) == 2  # retried after the 500


async def test_commands_survives_non_json_200() -> None:
    """A 200 non-JSON body is retried, not fatal to the stream."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(200, text="<html>proxy</html>")
        return _updates(_msg_update(update_id=5, message_id=10))

    notifier = _make(handler)
    got = []
    async for cmd in notifier.commands():
        got.append(cmd)
        break
    await notifier.aclose()

    assert len(got) == 1
    assert len(calls) == 2  # retried after the non-JSON body


async def test_commands_skips_malformed_update() -> None:
    """One malformed update (no message_id) is skipped, not fatal."""

    def handler(request: httpx.Request) -> httpx.Response:
        malformed = {
            "update_id": 1,
            "message": {
                "from": {"id": USER},
                "chat": {"id": CHAT},
                "text": "/status",
            },
        }
        return _updates(malformed, _msg_update(update_id=2, message_id=11))

    notifier = _make(handler)
    got = []
    async for cmd in notifier.commands():
        got.append(cmd)
        break
    await notifier.aclose()

    assert [c.message_id for c in got] == [11]


# --- config selection -----------------------------------------------


async def test_build_notifier_telegram_wires_the_auth_pair() -> None:
    cfg = TelegramNotifierConfig(
        backend="telegram", bot_token="T", chat_id=1, user_id=2
    )
    notifier = build_notifier(cfg)
    assert isinstance(notifier, TelegramNotifier)
    assert notifier.chat_id == 1
    assert notifier.user_id == 2
    await notifier.aclose()
