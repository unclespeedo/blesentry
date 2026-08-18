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

from bleak.args.bluez import OrPattern
from bleak.assigned_numbers import AdvertisementDataType
from bleak.exc import BleakError

from blesentry.scanner.models import Advertisement
from blesentry.scanner.protocol import Scanner
from blesentry.storage.database import MigrationError

# Provisional BlueZ passive-scan patterns: common AD FLAGS values
# (0x01 = flags AD type). Misses flag-less nonconnectable beacons —
# a BlueZ limitation to be characterized on hardware (#68).
DEFAULT_OR_PATTERNS = (
    "0:01:06",
    "0:01:1a",
    "0:01:05",
    "0:01:02",
    "0:01:04",
)


def parse_or_pattern(raw: str) -> OrPattern:
    """Parse ``START:ADTYPE:HEXBYTES`` (e.g. ``0:01:06``) into an OrPattern.

    Raises:
        ValueError: if the string is not three colon-separated fields
            with an integer start, hex AD type, and non-empty even-length
            hex content.
    """
    parts = raw.split(":")
    if len(parts) != 3:
        raise ValueError(f"or-pattern {raw!r} must be START:ADTYPE:HEXBYTES")
    start_raw, ad_type_raw, content_raw = parts
    try:
        start = int(start_raw, 10)
        ad_type = AdvertisementDataType(int(ad_type_raw, 16))
        content = bytes.fromhex(content_raw)
    except ValueError as exc:
        raise ValueError(f"or-pattern {raw!r}: {exc}") from exc
    if not content:
        raise ValueError(f"or-pattern {raw!r} has empty content")
    return OrPattern(start, ad_type, content)


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
        "--db",
        required=True,
        help="SQLite database path (created/migrated on start)",
    )
    run.add_argument(
        "--site-id",
        required=True,
        help="site identifier stamped on every row",
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


def format_table(advertisements: list[Advertisement]) -> str:
    """Render a human-readable table, strongest signal first."""
    lines = [
        f"{len(advertisements)} device(s) heard",
    ]
    if advertisements:
        lines.append(f"{'ADDRESS':<40} {'RSSI':>5}  NAME")
        for ad in sorted(advertisements, key=lambda a: -a.rssi):
            name = ad.local_name or "-"
            lines.append(f"{ad.mac:<40} {ad.rssi:>5}  {name}")
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


async def _run_daemon(args: argparse.Namespace) -> int:
    """Connect, migrate, and scan-persist until stopped.

    SIGTERM (systemd's stop signal) cancels the task so the loop
    unwinds through ``finally`` and the connection closes cleanly —
    the issue #20 graceful-shutdown contract.
    """
    from blesentry.loop import run_loop
    from blesentry.storage.database import apply_migrations, connect
    from blesentry.storage.repository import (
        DeviceRepository,
        ObservationRepository,
    )

    logger = logging.getLogger(__name__)
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is not None:
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    conn = None
    try:
        conn = await connect(args.db)
        applied = await apply_migrations(conn)
        if applied:
            logger.info("applied migrations: %s", ", ".join(applied))
        cycles = await run_loop(
            _build_scanner(args),
            DeviceRepository(conn, args.site_id),
            ObservationRepository(conn, args.site_id),
            duration=args.window,
            pause=args.pause,
            max_cycles=args.max_cycles,
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
