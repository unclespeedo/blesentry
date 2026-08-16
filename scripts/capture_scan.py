# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""One-shot BLE advertisement capture for the fixture corpus (P0-3).

Runs a bleak scan on the local adapter and writes every advertisement seen
to a normalized JSON array. The output is the seed of ``tests/fixtures/``
that Phase-1 tests (P1-1 ``Advertisement`` model, P1-2 ``MockScanner``)
parse.

Usage:
    uv run scripts/capture_scan.py --duration 900 --out capture.json

Normalized record schema (each element of the JSON array):

    mac                source address; on macOS/CoreBluetooth this is the
                       peripheral identifier, not a real MAC (docs/risks.md)
    rssi               last reported signal strength, dBm
    local_name         advertised name or null
    service_uuids      list of service UUID strings
    manufacturer_data  {company_id: hex} as strings, hex values are lower-case
    service_data       {uuid: hex}
    tx_power           advertised TX power or null
    timestamp          wall-clock seconds (float) at receipt
    adapter_id         constant label of the capturing backend

Only the most recent advertisement per address is retained (backends already
deduplicate by address; this mirrors ``BleakScanner`` behaviour).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

from bleak import BleakScanner


def _hex(value: bytes) -> str:
    return value.hex()


def _normalize(
    address: str, adv: Any, adapter_id: str, timestamp: float
) -> dict[str, Any]:
    return {
        "mac": address,
        "rssi": adv.rssi,
        "local_name": adv.local_name,
        "service_uuids": list(adv.service_uuids),
        "manufacturer_data": {
            str(company): _hex(data)
            for company, data in adv.manufacturer_data.items()
        },
        "service_data": {
            uuid: _hex(data) for uuid, data in adv.service_data.items()
        },
        "tx_power": adv.tx_power,
        "timestamp": timestamp,
        "adapter_id": adapter_id,
    }


def _write(out_path: str, records: list[dict[str, Any]]) -> None:
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2)
        fh.write("\n")


async def main() -> int:
    """Run the capture; returns process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duration",
        type=float,
        default=900.0,
        help="scan duration in seconds (default: 900)",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="output JSON path (default: corpus-<unix-ts>.json)",
    )
    parser.add_argument(
        "--adapter",
        default="macos-corebluetooth",
        help="adapter label recorded on every record "
        "(default: macos-corebluetooth)",
    )
    args = parser.parse_args()

    latest: dict[str, dict[str, Any]] = {}

    def on_detection(device: Any, adv: Any) -> None:
        record = _normalize(device.address, adv, args.adapter, time.time())
        latest[device.address] = record

    out_path = args.out or f"corpus-{int(time.time())}.json"
    print(f"scanning for {args.duration:.0f}s ...")
    scanner = BleakScanner(detection_callback=on_detection)
    try:
        async with scanner:
            await asyncio.sleep(args.duration)
    except KeyboardInterrupt:
        print("interrupted early; writing what was captured")

    records = list(latest.values())
    records.sort(key=lambda r: r["timestamp"])
    _write(out_path, records)
    print(f"wrote {len(records)} unique addresses to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
