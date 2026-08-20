# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Notifier seam tests (P2-5): protocol, value objects, auth, mock, config.

The library-independent core of the Notifier seam (ADR-0002 / ADR-0003).
These pin the contract the drain loop (P2-4) and bot flow (P2-6/P2-8)
build on, and the single-operator auth rule stated verbatim in
ADR-0003 — all without a live bot token (that round-trip is the issue's
HUMAN VERIFY box).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from blesentry.config import (
    Config,
    NoneNotifierConfig,
    TelegramNotifierConfig,
    build_notifier,
    load_config,
)
from blesentry.notifier.auth import is_authorized
from blesentry.notifier.mock import MockNotifier
from blesentry.notifier.models import (
    DeliveryResult,
    InboundCommand,
    OutboundMessage,
)
from blesentry.notifier.null import NullNotifier
from blesentry.notifier.protocol import Notifier

# --- value objects ---------------------------------------------------


def test_outbound_message_is_frozen() -> None:
    msg = OutboundMessage(text="hi")
    assert msg.text == "hi"
    frozen_field = "text"  # variable dodges B010 on a constant setattr
    with pytest.raises(ValidationError):
        setattr(msg, frozen_field, "no")


def test_outbound_message_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        OutboundMessage(text="hi", parse_mode="HTML")


def test_delivery_result_defaults() -> None:
    result = DeliveryResult(ok=True, message_id=7)
    assert result.ok
    assert result.message_id == 7
    assert result.error is None
    assert result.retriable is True


def test_inbound_command_fields() -> None:
    cmd = InboundCommand(chat_id=1, user_id=2, message_id=3, text="/status")
    assert cmd.text == "/status"
    assert cmd.chat_id == 1
    assert cmd.user_id == 2


# --- single-operator auth (ADR-0003, stated verbatim) ---------------


def test_authorized_when_both_ids_match() -> None:
    assert is_authorized(
        origin_chat_id=10,
        origin_user_id=20,
        allowed_chat_id=10,
        allowed_user_id=20,
    )


@pytest.mark.parametrize(
    ("chat", "user"),
    [(99, 20), (10, 99), (99, 99)],
)
def test_rejected_when_either_id_mismatches(chat: int, user: int) -> None:
    assert not is_authorized(
        origin_chat_id=chat,
        origin_user_id=user,
        allowed_chat_id=10,
        allowed_user_id=20,
    )


# --- protocol conformance -------------------------------------------


def test_mock_notifier_satisfies_protocol() -> None:
    assert isinstance(MockNotifier(), Notifier)


def test_null_notifier_satisfies_protocol() -> None:
    assert isinstance(NullNotifier(), Notifier)


# --- MockNotifier (the CI double) -----------------------------------


async def test_mock_send_records_and_autonumbers() -> None:
    notifier = MockNotifier()
    first = await notifier.send(OutboundMessage(text="a"))
    second = await notifier.send(OutboundMessage(text="b"))
    assert [m.text for m in notifier.sent] == ["a", "b"]
    assert first.ok and second.ok
    assert first.message_id != second.message_id


async def test_mock_send_replays_scripted_results() -> None:
    fail = DeliveryResult(ok=False, error="boom", retriable=True)
    notifier = MockNotifier(results=[fail])
    assert await notifier.send(OutboundMessage(text="a")) is fail
    # Script exhausted → falls back to an auto-ok result.
    assert (await notifier.send(OutboundMessage(text="b"))).ok


async def test_mock_commands_yields_scripted_inbound() -> None:
    cmds = [
        InboundCommand(chat_id=1, user_id=2, message_id=i, text=f"/{i}")
        for i in range(3)
    ]
    notifier = MockNotifier(inbound=cmds)
    got = [c async for c in notifier.commands()]
    assert got == cmds


async def test_mock_aclose_marks_closed() -> None:
    notifier = MockNotifier()
    await notifier.aclose()
    assert notifier.closed


# --- NullNotifier (the "none" backend) ------------------------------


async def test_null_notifier_discards_and_yields_nothing() -> None:
    notifier = NullNotifier()
    assert (await notifier.send(OutboundMessage(text="a"))).ok
    assert [c async for c in notifier.commands()] == []
    await notifier.aclose()


# --- config widening ------------------------------------------------


def test_none_is_the_default_backend() -> None:
    cfg = Config(site_id="s", storage={"db": "x.db"})
    assert isinstance(cfg.notifier, NoneNotifierConfig)


def test_telegram_config_parses() -> None:
    cfg = Config(
        site_id="s",
        storage={"db": "x.db"},
        notifier={
            "backend": "telegram",
            "bot_token": "secret-token",
            "chat_id": 100,
            "user_id": 200,
        },
    )
    assert isinstance(cfg.notifier, TelegramNotifierConfig)
    assert cfg.notifier.chat_id == 100
    assert cfg.notifier.user_id == 200


def test_telegram_token_is_secret_in_repr() -> None:
    cfg = TelegramNotifierConfig(
        backend="telegram", bot_token="super-secret", chat_id=1, user_id=2
    )
    assert "super-secret" not in repr(cfg)
    assert cfg.bot_token.get_secret_value() == "super-secret"


def test_telegram_backend_requires_token() -> None:
    with pytest.raises(ValidationError):
        TelegramNotifierConfig.model_validate(
            {"backend": "telegram", "chat_id": 1, "user_id": 2}
        )


def test_notifier_secret_loads_from_env_not_file(
    monkeypatch, tmp_path
) -> None:
    """The token fills from env — never written to the TOML on disk."""
    monkeypatch.setenv("BLESENTRY_NOTIFIER__BOT_TOKEN", "env-token")
    path = tmp_path / "c.toml"
    path.write_text(
        'site_id = "s"\n'
        "[storage]\n"
        'db = "x.db"\n'
        "[notifier]\n"
        'backend = "telegram"\n'
        "chat_id = 1\n"
        "user_id = 2\n",
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert isinstance(cfg.notifier, TelegramNotifierConfig)
    assert cfg.notifier.bot_token.get_secret_value() == "env-token"


def test_build_notifier_none_returns_null() -> None:
    assert isinstance(build_notifier(NoneNotifierConfig()), NullNotifier)
