# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Bulk-label session (P2-7): /init and ``blesentry init``.

Walks a snapshot of currently-PRESENT unlabeled devices one at a time.
The snapshot and cursor live in ``init_sessions`` so a partial session
survives daemon restart and can finish on chat or CLI. Time-boxed to
:data:`DEFAULT_TIMEOUT_SECONDS` of wall clock.

This module has no SQL — storage goes through
:class:`~blesentry.storage.repository.InitSessionRepository` and
:class:`~blesentry.storage.repository.DeviceRepository`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import NamedTuple

from blesentry.loop import iso_utc
from blesentry.storage.database import transaction
from blesentry.storage.repository import (
    DeviceRepository,
    DeviceRow,
    InitSessionRepository,
    InitSessionRow,
)

DEFAULT_TIMEOUT_SECONDS = 1800.0
# Same sentinel as ``commands.IGNORED_LABEL`` — kept here so this
# module does not import the command router (commands imports us).
IGNORED_LABEL = "(ignored)"
_HINT = "send a name, or /skip /ignore /done"

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "IGNORED_LABEL",
    "InitDeps",
    "handle_inbound",
    "run_cli_session",
    "start_or_resume",
]


class InitDeps(NamedTuple):
    """Repositories and knobs for one init-session action."""

    devices: DeviceRepository
    sessions: InitSessionRepository
    actor: str
    now: Callable[[], float]
    timeout: float
    ignored_label: str = IGNORED_LABEL


def _with_cursor(session: InitSessionRow, cursor: int) -> InitSessionRow:
    return InitSessionRow(
        id=session["id"],
        site_id=session["site_id"],
        status=session["status"],
        cursor=cursor,
        device_ids=session["device_ids"],
        expires_at=session["expires_at"],
        created_at=session["created_at"],
        updated_at=session["updated_at"],
    )


def _safe_display(text: str) -> str:
    """Collapse whitespace and strip controls for prompt display (#85)."""
    collapsed = " ".join(text.split())
    return "".join(ch if ch.isprintable() else "�" for ch in collapsed)


def _prompt(index: int, total: int, device: DeviceRow) -> str:
    address = _safe_display(device["address"] or "?") or "?"
    return (
        f"init {index + 1}/{total}: device {device['id']} [{address}]\n{_HINT}"
    )


def _done(labeled: int) -> str:
    return f"init done: labeled {labeled}"


async def _expire_if_stale(deps: InitDeps, session: InitSessionRow) -> bool:
    """Mark EXPIRED if the time-box has passed. Return True if expired."""
    if session["expires_at"] > iso_utc(deps.now()):
        return False
    await deps.sessions.set_status(session["id"], "EXPIRED")
    return True


async def _labeled_in_snapshot(deps: InitDeps, session: InitSessionRow) -> int:
    count = 0
    for device_id in session["device_ids"]:
        row = await deps.devices.get(device_id)
        if row is not None and row["label"] is not None:
            count += 1
    return count


async def _advance_to_next(
    deps: InitDeps, session: InitSessionRow, *, from_index: int
) -> str:
    """Move cursor to the next unlabeled snapshot member and prompt.

    Completes the session when none remain.
    """
    ids = session["device_ids"]
    total = len(ids)
    for index in range(from_index, total):
        row = await deps.devices.get(ids[index])
        if row is None or row["label"] is not None:
            continue
        if index != session["cursor"]:
            await deps.sessions.set_cursor(session["id"], index)
        return _prompt(index, total, row)
    labeled = await _labeled_in_snapshot(deps, session)
    await deps.sessions.set_status(session["id"], "DONE")
    return _done(labeled)


async def start_or_resume(deps: InitDeps) -> str:
    """Start a new session or re-prompt the current snapshot member."""
    prefix = ""
    session = await deps.sessions.get_active()
    if session is not None:
        if await _expire_if_stale(deps, session):
            prefix = "init session expired.\n"
            session = None
        else:
            prefix = "resuming init.\n"
            return prefix + await _advance_to_next(
                deps, session, from_index=session["cursor"]
            )
    present = await deps.devices.list_present_unlabeled()
    if not present:
        return prefix + "no unlabeled devices currently present"
    created = await deps.sessions.create(
        device_ids=[row["id"] for row in present],
        expires_at=iso_utc(deps.now() + deps.timeout),
    )
    return prefix + await _advance_to_next(deps, created, from_index=0)


async def _require_active(deps: InitDeps) -> InitSessionRow | str:
    """Return the live session, or a 'no session' / expired reply."""
    session = await deps.sessions.get_active()
    if session is None:
        return "no init session in progress"
    if await _expire_if_stale(deps, session):
        return "init session expired; send /init to start over"
    return session


async def _apply_current(
    deps: InitDeps, session: InitSessionRow, *, label: str
) -> str:
    """Label the expected cursor, or reprompt if another consumer won.

    Chat and CLI share the row on different connections. ``BEGIN
    IMMEDIATE`` serializes them; we re-read the cursor inside the
    transaction so a stale prompt cannot overwrite a label the other
    surface just wrote.
    """
    async with transaction(deps.devices.connection):
        live = await deps.sessions.get_active()
        if live is None:
            return "no init session in progress"
        if await _expire_if_stale(deps, live):
            return "init session expired; send /init to start over"
        from_index = live["cursor"]
        ack = ""
        if live["cursor"] == session["cursor"]:
            ids = live["device_ids"]
            index = live["cursor"]
            if index >= len(ids):
                await deps.sessions.set_status(live["id"], "DONE")
                return _done(await _labeled_in_snapshot(deps, live))
            device_id = ids[index]
            current = await deps.devices.get(device_id)
            already = current is not None and current["label"] is not None
            if not already:
                ok = await deps.devices.set_label(
                    device_id, label=label, actor=deps.actor
                )
                if ok:
                    ack = f"labeled device {device_id}: {label}\n"
            await deps.sessions.set_cursor(live["id"], index + 1)
            live = _with_cursor(live, index + 1)
            from_index = index + 1
        return ack + await _advance_to_next(deps, live, from_index=from_index)


async def handle_inbound(raw: str, deps: InitDeps) -> str | None:
    """Consume init-session text; return None to fall through to commands.

    Slash commands ``/init``, ``/skip``, ``/done``, and bare ``/ignore``
    are handled here when they belong to a session. Free text (no
    leading ``/``) is the name for the current device while a session
    is active.
    """
    text = raw.strip()
    if not text:
        return None
    is_slash = text.startswith("/")
    if is_slash:
        stripped = text.removeprefix("/")
        head, _, rest = stripped.partition(" ")
        verb = head.split("@", 1)[0].lower()
        args = rest.strip()
        if verb == "init":
            if args.lower() == "cancel":
                return await _cancel(deps)
            return await start_or_resume(deps)
        if verb == "skip":
            return await _skip(deps)
        if verb == "done":
            return await _finish(deps)
        if verb == "ignore" and not args:
            active = await deps.sessions.get_active()
            if active is None:
                return None
            return await _ignore_current(deps)
        return None
    session = await _require_active(deps)
    if isinstance(session, str):
        # No session / expired: expired reply is ours; otherwise the
        # command router should treat this as an unknown command.
        if session.startswith("init session expired"):
            return session
        return None
    return await _apply_current(deps, session, label=text)


async def _cancel(deps: InitDeps) -> str:
    session = await deps.sessions.get_active()
    if session is None:
        return "no init session in progress"
    if await _expire_if_stale(deps, session):
        return "init session expired; send /init to start over"
    await deps.sessions.set_status(session["id"], "CANCELLED")
    return "init cancelled"


async def _skip(deps: InitDeps) -> str:
    session = await _require_active(deps)
    if isinstance(session, str):
        return session
    nxt = session["cursor"] + 1
    await deps.sessions.set_cursor(session["id"], nxt)
    return await _advance_to_next(
        deps, _with_cursor(session, nxt), from_index=nxt
    )


async def _finish(deps: InitDeps) -> str:
    session = await _require_active(deps)
    if isinstance(session, str):
        return session
    labeled = await _labeled_in_snapshot(deps, session)
    await deps.sessions.set_status(session["id"], "DONE")
    return _done(labeled)


async def _ignore_current(deps: InitDeps) -> str:
    session = await _require_active(deps)
    if isinstance(session, str):
        return session
    return await _apply_current(deps, session, label=deps.ignored_label)


async def run_cli_session(
    devices: DeviceRepository,
    sessions: InitSessionRepository,
    *,
    readline: Callable[[], str | None],
    write: Callable[[str], None],
    now: Callable[[], float] = time.time,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    actor: str = "cli",
) -> int:
    """Drive an init session over line-oriented I/O (the CLI).

    EOF (empty readline) pauses the session for later resume. Returns
    the process exit code (0 on success or pause).
    """
    deps = InitDeps(devices, sessions, actor, now, timeout, IGNORED_LABEL)
    write(await start_or_resume(deps))
    if await sessions.get_active() is None:
        return 0
    while True:
        line = readline()
        if line is None:
            write("paused; resume with blesentry init or /init")
            return 0
        reply = await handle_inbound(line, deps)
        if reply is None:
            write(_HINT)
            continue
        write(reply)
        if await sessions.get_active() is None:
            return 0
