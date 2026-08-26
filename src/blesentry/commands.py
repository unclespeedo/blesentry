# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Admin command router (P2-8): inbound commands → replies.

The daemon runs a command loop consuming the notifier's ``commands()``
seam. Auth is already enforced there — only messages from the configured
``chat_id`` **and** ``user_id`` are yielded (ADR-0003, implemented in
P2-5), so a command reaching this module is authorized by construction.

Each command is parsed, dispatched to a handler, and its reply enqueued
to the outbox (ADR-0003: outbound flows through the outbox, never
fire-and-forget), so the drain delivers it with the same
durability/backoff as an alert. Replies are plain text.

Commands: help, status, list, label, unlabel, describe, init (P2-7).
The scan-loop-coupled ones (force-scan, and status's presence-aware
fields) are a follow-up (#110).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import NamedTuple

from blesentry.init import (
    DEFAULT_TIMEOUT_SECONDS,
    IGNORED_LABEL,
    InitDeps,
    handle_inbound,
)
from blesentry.notifier.models import InboundCommand, OutboundMessage
from blesentry.notifier.protocol import Notifier
from blesentry.storage.repository import (
    DeviceRepository,
    InitSessionRepository,
    OutboxRepository,
)

logger = logging.getLogger(__name__)

# Re-exported: the P2-6 /ignore sentinel. Defined in init.py so the
# session layer can apply it without importing this router.

HELP_TEXT = (
    "commands:\n"
    "  /status — uptime, device count, outbox depth, db size\n"
    "  /list — list known devices\n"
    "  /label <id> <name> — name a device\n"
    "  /unlabel <id> — clear a device's name\n"
    "  /ignore <id> — acknowledge a device, no more alerts\n"
    "  /describe <id> <text> — set a device note\n"
    "  /init — bulk-label present devices (then a name, /skip /done)\n"
    "  /help — this message"
)

__all__ = ["HELP_TEXT", "dispatch", "run_command_loop"]

# Max devices shown in one /list reply — keeps it under Telegram's
# 4096-char message cap. Pagination is a follow-up.
_LIST_CAP = 50


class CommandContext(NamedTuple):
    """Everything a command handler may need for one inbound command."""

    command: InboundCommand
    devices: DeviceRepository
    outbox: OutboxRepository
    db_path: str
    clock: Callable[[], float]
    started_at: float
    sessions: InitSessionRepository | None = None
    now: Callable[[], float] = time.time
    init_timeout: float = DEFAULT_TIMEOUT_SECONDS


def _parse(text: str) -> tuple[str, str]:
    """Split raw text into a lowercased verb and its argument string.

    Tolerates a leading ``/`` and a Telegram ``@botname`` suffix.
    """
    stripped = text.strip().removeprefix("/")
    head, _, rest = stripped.partition(" ")
    verb = head.split("@", 1)[0].lower()
    return verb, rest.strip()


def _device_id(token: str) -> int | None:
    try:
        value = int(token)
    except ValueError:
        return None
    # SQLite binds ids as int64; an out-of-range value would raise
    # OverflowError at the SQL layer, so reject it here as not-an-id.
    if not -(2**63) <= value <= 2**63 - 1:
        return None
    return value


def _one_line(text: str) -> str:
    """Collapse whitespace so a label/description can't forge list rows."""
    return " ".join(text.split())


async def _help(args: str, ctx: CommandContext) -> str:
    return HELP_TEXT


async def _status(args: str, ctx: CommandContext) -> str:
    uptime = _format_duration(ctx.clock() - ctx.started_at)
    devices = len(await ctx.devices.list_devices())
    depth = await ctx.outbox.count_pending()
    return (
        "status:\n"
        f"  uptime: {uptime}\n"
        f"  devices: {devices}\n"
        f"  outbox depth: {depth}\n"
        f"  db size: {_format_size(ctx.db_path)}"
    )


async def _list(args: str, ctx: CommandContext) -> str:
    rows = await ctx.devices.list_devices()
    if not rows:
        return "no devices yet"
    # Cap the reply: Telegram rejects messages over 4096 chars, and a
    # rejected reply would be silently dead-lettered by the drain.
    lines = ["devices:"]
    for row in rows[:_LIST_CAP]:
        label = _one_line(row["label"]) if row["label"] else "(unlabeled)"
        line = f"  {row['id']}: {label} [{row['address'] or '?'}]"
        if row["description"]:
            line += f" — {_one_line(row['description'])}"
        lines.append(line)
    if len(rows) > _LIST_CAP:
        lines.append(
            f"  …and {len(rows) - _LIST_CAP} more ({len(rows)} total)"
        )
    return "\n".join(lines)


async def _label(args: str, ctx: CommandContext) -> str:
    token, _, label = args.partition(" ")
    label = label.strip()
    device_id = _device_id(token)
    if device_id is None or not label:
        return "usage: /label <device-id> <name>"
    ok = await ctx.devices.set_label(
        device_id, label=label, actor=f"tg:{ctx.command.user_id}"
    )
    return (
        f"labeled device {device_id}: {label}"
        if ok
        else f"no device {device_id}"
    )


async def _unlabel(args: str, ctx: CommandContext) -> str:
    device_id = _device_id(args.strip())
    if device_id is None:
        return "usage: /unlabel <device-id>"
    ok = await ctx.devices.set_label(
        device_id, label=None, actor=f"tg:{ctx.command.user_id}"
    )
    return (
        f"cleared label on device {device_id}"
        if ok
        else f"no device {device_id}"
    )


async def _ignore(args: str, ctx: CommandContext) -> str:
    device_id = _device_id(args.strip())
    if device_id is None:
        return "usage: /ignore <device-id>"
    ok = await ctx.devices.set_label(
        device_id, label=IGNORED_LABEL, actor=f"tg:{ctx.command.user_id}"
    )
    return (
        f"ignoring device {device_id} — no more alerts"
        if ok
        else f"no device {device_id}"
    )


async def _describe(args: str, ctx: CommandContext) -> str:
    token, _, description = args.partition(" ")
    description = description.strip()
    device_id = _device_id(token)
    if device_id is None or not description:
        return "usage: /describe <device-id> <text>"
    ok = await ctx.devices.set_description(device_id, description=description)
    return f"described device {device_id}" if ok else f"no device {device_id}"


_HANDLERS: dict[str, Callable[[str, CommandContext], Awaitable[str]]] = {
    "help": _help,
    "start": _help,
    "status": _status,
    "list": _list,
    "list_devices": _list,
    "devices": _list,
    "label": _label,
    "unlabel": _unlabel,
    "ignore": _ignore,
    "describe": _describe,
    "set_description": _describe,
}


def _init_deps(ctx: CommandContext) -> InitDeps | None:
    if ctx.sessions is None:
        return None
    return InitDeps(
        ctx.devices,
        ctx.sessions,
        actor=f"tg:{ctx.command.user_id}",
        now=ctx.now,
        timeout=ctx.init_timeout,
        ignored_label=IGNORED_LABEL,
        message_id=ctx.command.message_id,
    )


async def dispatch(ctx: CommandContext) -> str:
    """Route one authorized command to its handler; return the reply."""
    deps = _init_deps(ctx)
    if deps is not None:
        handled = await handle_inbound(ctx.command.text, deps)
        if handled is not None:
            return handled
    verb, args = _parse(ctx.command.text)
    handler = _HANDLERS.get(verb)
    if handler is None:
        prefix = f"unknown command: /{verb}\n\n" if verb else ""
        return f"{prefix}{HELP_TEXT}"
    return await handler(args, ctx)


async def run_command_loop(
    notifier: Notifier,
    devices: DeviceRepository,
    outbox: OutboxRepository,
    *,
    db_path: str,
    started_at: float,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], float] = time.time,
    init_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_commands: int | None = None,
) -> int:
    """Consume authorized commands and reply through the outbox.

    Runs until ``commands()`` ends (or ``max_commands`` for tests). In
    the daemon it runs on its OWN connection (#91) — ``devices`` and
    ``outbox`` here are built on a connection dedicated to this task,
    never the scan loop's. A handler error is logged and answered, never
    fatal to the loop. Returns the number of commands processed.
    """
    sessions = InitSessionRepository(devices.connection, devices.site_id)
    processed = 0
    async for command in notifier.commands():
        ctx = CommandContext(
            command,
            devices,
            outbox,
            db_path,
            clock,
            started_at,
            sessions,
            now,
            init_timeout,
        )
        try:
            reply = await dispatch(ctx)
        except Exception:
            logger.exception("command handler failed")
            reply = "sorry — that command failed"
        # The reply is enqueued (ADR-0003), not sent directly; it need
        # not be atomic with the command's own write — the notifier's
        # at-least-once redelivery covers a crash between the two. A
        # failed enqueue is logged, never fatal to the loop.
        try:
            await outbox.enqueue(
                payload=OutboundMessage(text=reply).model_dump_json()
            )
        except Exception:
            logger.exception("failed to enqueue command reply")
        processed += 1
        if max_commands is not None and processed >= max_commands:
            break
    return processed


def _format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _format_size(path: str) -> str:
    # Include the WAL sidecars — under journal_mode=WAL an un-checkpointed
    # -wal file can hold significant data, so the main file alone
    # understates real on-disk usage.
    total = 0.0
    found = False
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(path + suffix)
            found = True
        except OSError:
            continue
    if not found:
        return "unknown"
    for unit in ("B", "KB", "MB"):
        if total < 1024:
            precision = 0 if unit == "B" else 1
            return f"{total:.{precision}f} {unit}"
        total /= 1024
    return f"{total:.1f} GB"
