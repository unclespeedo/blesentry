# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""NtfyNotifier tests (P4-7): publish, poll subscribe, delivery contract."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from blesentry.config import NtfyNotifierConfig, build_notifier
from blesentry.notifier.models import OutboundMessage
from blesentry.notifier.ntfy import NtfyNotifier
from blesentry.notifier.protocol import Notifier

OPERATOR = 42
Handler = Callable[[httpx.Request], httpx.Response]


def _make(
    handler: Handler,
    *,
    server: str = "https://ntfy.example",
    alert_topic: str = "blesentry-alerts",
    command_topic: str = "blesentry-cmd",
    token: str | None = "sekrit",
) -> NtfyNotifier:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return NtfyNotifier(
        server=server,
        alert_topic=alert_topic,
        command_topic=command_topic,
        token=token,
        operator_user_id=OPERATOR,
        client=client,
        error_backoff=0.0,
        poll_timeout=5.0,
    )


def _publish_ok(msg_id: str = "abc123") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": msg_id,
            "time": 1,
            "event": "message",
            "topic": "blesentry-alerts",
            "message": "hi",
        },
    )


def _command_line(
    *,
    msg_id: str = "cmd1",
    body: str = "/status",
    event: str = "message",
) -> str:
    return json.dumps(
        {
            "id": msg_id,
            "time": 2,
            "event": event,
            "topic": "blesentry-cmd",
            "message": body,
        }
    )


def _open_line() -> str:
    return json.dumps(
        {
            "id": "open1",
            "time": 1,
            "event": "open",
            "topic": "blesentry-cmd",
        }
    )


async def test_ntfy_notifier_satisfies_protocol() -> None:
    notifier = _make(lambda req: httpx.Response(200, json={}))
    assert isinstance(notifier, Notifier)
    await notifier.aclose()


async def test_send_posts_plain_body_to_alert_topic() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _publish_ok("out99")

    notifier = _make(handler)
    result = await notifier.send(OutboundMessage(text="alert text"))
    await notifier.aclose()

    assert result.ok and result.message_id is not None
    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert req.url.path.endswith("/blesentry-alerts")
    assert req.content == b"alert text"
    assert req.headers.get("authorization") == "Bearer sekrit"


async def test_send_without_token_omits_auth_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _publish_ok()

    notifier = _make(handler, token=None)
    assert (await notifier.send(OutboundMessage(text="x"))).ok
    await notifier.aclose()
    assert "authorization" not in seen[0].headers


async def test_send_transport_error_is_retriable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x")

    notifier = _make(handler)
    result = await notifier.send(OutboundMessage(text="x"))
    await notifier.aclose()
    assert not result.ok and result.retriable


async def test_commands_yields_message_event_bodies() -> None:
    poll_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls
        assert request.url.params.get("poll") == "1"
        assert request.url.params.get("since") is not None
        poll_calls += 1
        if poll_calls == 1:
            body = _command_line(body="/status") + "\n"
            return httpx.Response(200, text=body)
        return httpx.Response(200, text="")

    notifier = _make(handler)
    cmd = None
    async for item in notifier.commands():
        cmd = item
        break
    await notifier.aclose()

    assert cmd is not None
    assert cmd.text == "/status"
    assert cmd.user_id == OPERATOR
    assert cmd.chat_id == OPERATOR


async def test_commands_empty_poll_then_later_message() -> None:
    """Greptile P1: first poll may return no command; a later poll must."""
    poll_calls = 0
    first_since: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls, first_since
        assert request.url.params.get("poll") == "1"
        poll_calls += 1
        if poll_calls == 1:
            first_since = request.url.params.get("since")
            return httpx.Response(200, text=_open_line() + "\n")
        assert request.url.params.get("since") == first_since
        return httpx.Response(200, text=_command_line(body="/status") + "\n")

    notifier = _make(handler)
    cmd = None
    async for item in notifier.commands():
        cmd = item
        break
    await notifier.aclose()

    assert poll_calls >= 2
    assert first_since is not None and first_since.isdigit()
    assert cmd is not None and cmd.text == "/status"


async def test_commands_skips_non_message_events() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            _command_line(event="keepalive", body="")
            + "\n"
            + _command_line(body="/help")
            + "\n"
        )
        return httpx.Response(200, text=body)

    notifier = _make(handler)
    cmd = None
    async for item in notifier.commands():
        cmd = item
        break
    await notifier.aclose()
    assert cmd is not None and cmd.text == "/help"


async def test_commands_advances_since_only_after_yield() -> None:
    seen_since: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_since.append(request.url.params.get("since"))
        n = len(seen_since)
        if n == 1:
            return httpx.Response(
                200, text=_command_line(msg_id="first") + "\n"
            )
        assert request.url.params.get("since") == "first"
        return httpx.Response(
            200, text=_command_line(msg_id="second", body="/help") + "\n"
        )

    notifier = _make(handler)
    got: list = []
    async for cmd in notifier.commands():
        got.append(cmd)
        if len(got) >= 2:
            break
    await notifier.aclose()
    assert len(seen_since) >= 2
    assert seen_since[0] is not None and seen_since[0].isdigit()
    assert seen_since[1] == "first"
    assert got[0].text == "/status"
    assert got[1].text == "/help"


async def test_build_notifier_ntfy_wires_config() -> None:
    cfg = NtfyNotifierConfig(
        backend="ntfy",
        server="https://ntfy.example",
        alert_topic="a",
        command_topic="c",
        token="t",
        operator_user_id=7,
    )
    notifier = build_notifier(cfg)
    assert isinstance(notifier, NtfyNotifier)
    assert notifier.operator_user_id == 7
