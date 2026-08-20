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
from typing import NamedTuple

from bleak.exc import BleakError

from blesentry.scanner.models import Advertisement
from blesentry.scanner.patterns import DEFAULT_OR_PATTERNS, parse_or_pattern
from blesentry.scanner.protocol import Scanner
from blesentry.storage.database import MigrationError


class _RunSettings(NamedTuple):
    """Effective ``run`` settings, from ``--config`` or explicit flags.

    Resolver thresholds are ``None`` in flag mode (the loop then builds
    a default resolver) and populated in config mode.
    """

    scanner: Scanner
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
        from blesentry.config import build_scanner, load_config

        cfg = load_config(args.config)
        return _RunSettings(
            scanner=build_scanner(cfg.scanner),
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
    return _RunSettings(
        scanner=_build_scanner(args),
        db=args.db,
        site_id=args.site_id,
        window=args.window,
        pause=args.pause,
        max_cycles=args.max_cycles,
        min_score=None,
        recent_window=None,
    )


async def _run_daemon(args: argparse.Namespace) -> int:
    """Connect, migrate, and scan-persist until stopped.

    SIGTERM (systemd's stop signal) cancels the task so the loop
    unwinds through ``finally`` and the connection closes cleanly —
    the issue #20 graceful-shutdown contract. A second SIGTERM during
    shutdown hits the default disposition and kills the process
    immediately (WAL keeps the database consistent) — deliberate.
    """
    from blesentry.loop import run_loop
    from blesentry.resolver import DeviceResolver
    from blesentry.storage.database import apply_migrations, connect
    from blesentry.storage.repository import (
        DeviceRepository,
        ObservationRepository,
    )

    settings = _resolve_run_settings(args)
    logger = logging.getLogger(__name__)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is not None:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    conn = None
    try:
        conn = await connect(settings.db)
        applied = await apply_migrations(conn)
        if applied:
            logger.info("applied migrations: %s", ", ".join(applied))
        devices = DeviceRepository(conn, settings.site_id)
        resolver = None
        if settings.min_score is not None and (
            settings.recent_window is not None
        ):
            resolver = DeviceResolver(
                devices,
                min_score=settings.min_score,
                recent_window=settings.recent_window,
            )
        cycles = await run_loop(
            settings.scanner,
            devices,
            ObservationRepository(conn, settings.site_id),
            duration=settings.window,
            pause=settings.pause,
            max_cycles=settings.max_cycles,
            resolver=resolver,
        )
        logger.info("stopped after %d cycles", cycles)
    except asyncio.CancelledError:
        if task is not None:
            task.uncancel()
        logger.info("terminated; shutting down cleanly")
    finally:
        if task is not None:
            loop.remove_signal_handler(signal.SIGTERM)
        if conn is not None:
            await conn.close()
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
