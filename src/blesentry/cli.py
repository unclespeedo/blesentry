# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""One-shot scan CLI (P1-4) — the project's primary remote debug tool.

``blesentry scan --duration N`` runs a single scan window on the real
adapter and prints a table (or ``--json`` for scripting). argparse, not
typer: zero extra dependencies on the 512MB target.

On Linux the BlueZ passive path needs advertisement-monitor patterns;
the built-in default set matches common flags values and is provisional
until validated on Pi hardware (#68). Override with ``--or-pattern``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import time
from collections.abc import Coroutine
from typing import Any, NamedTuple

from bleak.exc import BleakError

from blesentry.notifier.protocol import Notifier
from blesentry.scanner.models import Advertisement
from blesentry.scanner.patterns import DEFAULT_OR_PATTERNS, parse_or_pattern
from blesentry.scanner.protocol import Scanner
from blesentry.storage.database import MigrationError


class _RunSettings(NamedTuple):
    """Effective ``run`` settings, from ``--config`` or explicit flags.

    Resolver thresholds are ``None`` in flag mode (the loop then builds
    a default resolver) and populated in config mode. ``notifier`` is the
    config-selected backend (``NullNotifier`` in flag mode — alerting is
    a config-only concern).
    """

    scanner: Scanner
    notifier: Notifier
    db: str
    site_id: str
    window: float
    pause: float
    max_cycles: int | None
    min_score: float | None
    recent_window: int | None


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser with the scan subcommand."""
    parser = argparse.ArgumentParser(
        prog="blesentry",
        description="Local-first BLE presence sentinel.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser(
        "scan",
        help="run one scan window and print what was heard",
    )
    scan.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="scan window in seconds (default: 10)",
    )
    scan.add_argument(
        "--json",
        action="store_true",
        help="emit a JSON array instead of a table",
    )
    scan.add_argument(
        "--adapter",
        default=None,
        help="BlueZ adapter device name, e.g. hci0 (Linux only; "
        "default: system default adapter)",
    )
    scan.add_argument(
        "--or-pattern",
        action="append",
        metavar="START:ADTYPE:HEXBYTES",
        dest="or_patterns",
        help="BlueZ passive-scan pattern (repeatable; Linux only; "
        f"default: {' '.join(DEFAULT_OR_PATTERNS)} — provisional "
        "until #68 hardware validation)",
    )

    run = sub.add_parser(
        "run",
        help="scan continuously and persist to the database (P1-8)",
    )
    run.add_argument(
        "--config",
        default=None,
        help="TOML config file (P1-9); supplies every setting below. "
        "Cannot be combined with --db/--site-id.",
    )
    run.add_argument(
        "--db",
        default=None,
        help="SQLite database path (created/migrated on start); "
        "required unless --config is given",
    )
    run.add_argument(
        "--site-id",
        default=None,
        help="site identifier stamped on every row; "
        "required unless --config is given",
    )
    run.add_argument(
        "--window",
        type=float,
        default=10.0,
        help="scan window in seconds (default: 10)",
    )
    run.add_argument(
        "--pause",
        type=float,
        default=5.0,
        help="pause between windows in seconds (default: 5)",
    )
    run.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="stop after N cycles (default: run forever)",
    )
    run.add_argument(
        "--adapter",
        default=None,
        help="BlueZ adapter device name, e.g. hci0 (Linux only)",
    )
    run.add_argument(
        "--or-pattern",
        action="append",
        metavar="START:ADTYPE:HEXBYTES",
        dest="or_patterns",
        help="BlueZ passive-scan pattern (repeatable; Linux only)",
    )
    return parser


async def run_scan(
    scanner: Scanner,
    duration: float,
) -> list[Advertisement]:
    """Run one scan window on any Scanner implementation."""
    return await scanner.scan(duration=duration)


def format_json(advertisements: list[Advertisement]) -> str:
    """Render advertisements as a JSON array for scripting."""
    return json.dumps(
        [ad.model_dump(mode="json") for ad in advertisements],
        indent=2,
    )


def _display_text(value: str) -> str:
    """Sanitize untrusted text for terminal display (#85).

    Device names are radio-controlled: ANSI/OSC escapes and bidi
    control characters are replaced so a crafted name cannot inject
    terminal sequences or visually reorder the line. isprintable()
    is False for both C0/C1 controls and Unicode format characters
    (Cf), which covers ESC/BEL and the bidi overrides. Residual and
    accepted: combining-mark floods and homoglyphs are printable and
    pass through — cosmetic misalignment only, no injection.
    """
    return "".join(ch if ch.isprintable() else "�" for ch in value)


def format_table(advertisements: list[Advertisement]) -> str:
    """Render a human-readable table, strongest signal first."""
    lines = [
        f"{len(advertisements)} device(s) heard",
    ]
    if advertisements:
        lines.append(f"{'ADDRESS':<40} {'RSSI':>5}  NAME")
        for ad in sorted(advertisements, key=lambda a: -a.rssi):
            name = _display_text(ad.local_name or "-")
            lines.append(
                f"{_display_text(ad.address):<40} {ad.rssi:>5}  {name}"
            )
    return "\n".join(lines)


def _build_scanner(args: argparse.Namespace) -> Scanner:
    """Construct the platform BleakScanner from CLI arguments.

    Imported lazily so ``--help`` and argument errors never pay the
    bleak/backend import cost.
    """
    from blesentry.scanner.bleak import BleakScanner

    if sys.platform == "darwin":
        return BleakScanner(adapter_id="macos-corebluetooth")
    raw = args.or_patterns or list(DEFAULT_OR_PATTERNS)
    return BleakScanner(
        adapter_id="bluez-linux",
        adapter=args.adapter,
        or_patterns=[parse_or_pattern(p) for p in raw],
    )


def _resolve_run_settings(args: argparse.Namespace) -> _RunSettings:
    """Fold the two ``run`` invocation styles into one settings object.

    Either ``--config FILE`` (every setting from the file, including
    resolver thresholds) or the explicit ``--db``/``--site-id`` flags —
    never both. Raises ``ValueError`` so the CLI's fail-fast handler
    reports it with a non-zero exit.
    """
    if args.config is not None:
        if args.db is not None or args.site_id is not None:
            raise ValueError("--config cannot be combined with --db/--site-id")
        from blesentry.config import (
            build_notifier,
            build_scanner,
            load_config,
        )

        cfg = load_config(args.config)
        return _RunSettings(
            scanner=build_scanner(cfg.scanner),
            notifier=build_notifier(cfg.notifier),
            db=str(cfg.storage.db),
            site_id=cfg.site_id,
            window=cfg.scan.window,
            pause=cfg.scan.pause,
            max_cycles=cfg.scan.max_cycles,
            min_score=cfg.resolver.min_score,
            recent_window=cfg.resolver.recent_window,
        )
    if args.db is None or args.site_id is None:
        raise ValueError("run requires --config, or both --db and --site-id")
    from blesentry.notifier.null import NullNotifier

    return _RunSettings(
        scanner=_build_scanner(args),
        notifier=NullNotifier(),
        db=args.db,
        site_id=args.site_id,
        window=args.window,
        pause=args.pause,
        max_cycles=args.max_cycles,
        min_score=None,
        recent_window=None,
    )


async def _run_supervised(
    scan: Coroutine[Any, Any, Any],
    *others: Coroutine[Any, Any, Any],
) -> int:
    """Run the scan loop and its sibling tasks concurrently, fail-loud.

    ``others`` are the outbox drain and (when a real notifier is
    configured) the command loop. Whichever task finishes or fails first
    stops the rest: if any raises, the siblings are cancelled and the
    exception propagates — a dead scan, drain, or command loop takes the
    daemon down for the supervisor (systemd, P3-1) to restart, never a
    half-running sentinel. Cancelling this coroutine (SIGTERM) cancels
    all children; the ``finally`` always drains their cancellation so no
    task is orphaned. Returns the scan loop's cycle count.
    """
    scan_task = asyncio.ensure_future(scan)
    children = [scan_task, *(asyncio.ensure_future(c) for c in others)]
    try:
        await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for child in children:
            child.cancel()
        outcomes = await asyncio.gather(*children, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(
            outcome, asyncio.CancelledError
        ):
            raise outcome
    return scan_task.result() if not scan_task.cancelled() else 0


async def _run_daemon(args: argparse.Namespace) -> int:
    """Connect, migrate, and scan-persist + drain until stopped.

    The scan loop, the outbox drain, and (when a real notifier is
    configured) the inbound command loop run concurrently
    (:func:`_run_supervised`). Each task that writes gets its OWN
    connection: ``transaction()`` nesting is connection-global, so
    sharing one connection across tasks would corrupt their units of
    work (#91). The command loop is skipped for the ``none`` backend,
    which has no inbound side.

    SIGTERM (systemd's stop signal) cancels the task so both loops
    unwind through ``finally`` and connections close cleanly — the issue
    #20 graceful-shutdown contract. The handler removes itself on the
    first signal, so a second SIGTERM during shutdown hits the default
    disposition and kills the process immediately (WAL keeps the
    database consistent) — deliberate, and it never re-interrupts the
    in-flight cleanup that would otherwise orphan the child tasks.
    """
    from blesentry.commands import run_command_loop
    from blesentry.drain import run_drain
    from blesentry.loop import run_loop
    from blesentry.notifier.null import NullNotifier
    from blesentry.presence import PresenceTracker
    from blesentry.resolver import DeviceResolver
    from blesentry.storage.database import apply_migrations, connect
    from blesentry.storage.repository import (
        DeviceRepository,
        ObservationRepository,
        OutboxRepository,
        PresenceEventRepository,
    )

    settings = _resolve_run_settings(args)
    logger = logging.getLogger(__name__)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    scan_conn = None
    drain_conn = None
    cmd_conn = None
    try:
        if task is not None:

            def _on_sigterm() -> None:
                # Unwind gracefully on the first SIGTERM; unregister so a
                # second hits the default disposition (abrupt kill) and
                # never re-cancels mid-cleanup (which would orphan the
                # child tasks). Inside the try so aclose still runs if
                # add_signal_handler ever raises.
                loop.remove_signal_handler(signal.SIGTERM)
                task.cancel()

            loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
        scan_conn = await connect(settings.db)
        applied = await apply_migrations(scan_conn)
        if applied:
            logger.info("applied migrations: %s", ", ".join(applied))
        # Dedicated connection for the drain task (#91). Same WAL
        # database; a second connection is a supported reader/writer.
        drain_conn = await connect(settings.db)
        devices = DeviceRepository(scan_conn, settings.site_id)
        resolver = None
        if settings.min_score is not None and (
            settings.recent_window is not None
        ):
            resolver = DeviceResolver(
                devices,
                min_score=settings.min_score,
                recent_window=settings.recent_window,
            )
        coros = [
            run_loop(
                settings.scanner,
                devices,
                ObservationRepository(scan_conn, settings.site_id),
                duration=settings.window,
                pause=settings.pause,
                max_cycles=settings.max_cycles,
                resolver=resolver,
                # Presence runs on the scan connection so transitions
                # commit atomically with their observations (#84).
                # Thresholds are defaults; config wiring is P2-2 (#23).
                presence=PresenceTracker(),
                presence_events=PresenceEventRepository(
                    scan_conn, settings.site_id
                ),
            ),
            run_drain(
                OutboxRepository(drain_conn, settings.site_id),
                settings.notifier,
            ),
        ]
        # The command loop consumes the notifier's inbound side; the
        # null backend has none, so skip it. Its own connection (#91).
        if not isinstance(settings.notifier, NullNotifier):
            cmd_conn = await connect(settings.db)
            coros.append(
                run_command_loop(
                    settings.notifier,
                    DeviceRepository(cmd_conn, settings.site_id),
                    OutboxRepository(cmd_conn, settings.site_id),
                    db_path=settings.db,
                    started_at=time.monotonic(),
                )
            )
        cycles = await _run_supervised(*coros)
        logger.info("stopped after %d cycles", cycles)
    except asyncio.CancelledError:
        if task is not None:
            task.uncancel()
        logger.info("terminated; shutting down cleanly")
    finally:
        if task is not None:
            loop.remove_signal_handler(signal.SIGTERM)
        await settings.notifier.aclose()
        if scan_conn is not None:
            await scan_conn.close()
        if drain_conn is not None:
            await drain_conn.close()
        if cmd_conn is not None:
            await cmd_conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            )
            return asyncio.run(_run_daemon(args))
        scanner = _build_scanner(args)
        advertisements = asyncio.run(run_scan(scanner, duration=args.duration))
    except KeyboardInterrupt:
        # Graceful SIGINT/SIGTERM-adjacent stop: asyncio.run cancels
        # the loop task and closes the connection via finally.
        print("interrupted; shutting down", file=sys.stderr)
        return 0
    except (ValueError, OSError, BleakError, MigrationError) as exc:
        # Fail fast and loud (ADR-0002): a sentinel that cannot scan
        # must say so, with a non-zero exit for scripts.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(format_json(advertisements))
    else:
        print(format_table(advertisements))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
