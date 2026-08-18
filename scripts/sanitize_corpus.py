# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Sanitize a capture corpus for public commit (#82).

Implements the mechanical parts of ``tests/fixtures/README.md``:

- Apple Continuity type-0x09 (AirPlay target) TLVs: the embedded IPv4
  is remapped length-preserving into TEST-NET-1 (192.0.2.x, one
  address per distinct original, preserving per-device consistency)
  and the port is zeroed.
- Absolute timestamps are shifted by one per-corpus offset so the
  first record lands on the synthetic base epoch 1600000000.0;
  inter-record deltas are preserved exactly.

Everything else passes through byte-identical. Keyed MAC pseudonyms
(Linux corpora) and auth-tag scrambling are applied here as those
capture types arrive — see the README protocol.

Usage:
    uv run scripts/sanitize_corpus.py in.json out.json
"""

from __future__ import annotations

import argparse
import json
from typing import Any

SYNTHETIC_EPOCH = 1600000000.0
APPLE_COMPANY = "76"
AIRPLAY_TARGET = 0x09


def _remap_airplay(payload: bytes, ip_map: dict[bytes, bytes]) -> bytes:
    """Rewrite IPv4+port inside type-0x09 TLVs, walking the TLV chain."""
    out = bytearray()
    i = 0
    while i + 2 <= len(payload):
        tlv_type, length = payload[i], payload[i + 1]
        body = bytearray(payload[i + 2 : i + 2 + length])
        if tlv_type == AIRPLAY_TARGET and length >= 8:
            original = bytes(body[2:6])
            if original not in ip_map:
                ip_map[original] = bytes([192, 0, 2, len(ip_map) + 1])
            body[2:6] = ip_map[original]
            body[6:8] = b"\x00\x00"
        out += bytes([tlv_type, length]) + body
        i += 2 + length
    out += payload[i:]
    return bytes(out)


def sanitize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return sanitized copies of *records* per the fixtures protocol."""
    if not records:
        return records
    offset = records[0]["timestamp"] - SYNTHETIC_EPOCH
    ip_map: dict[bytes, bytes] = {}
    out = []
    for record in records:
        record = dict(record)
        record["timestamp"] = round(record["timestamp"] - offset, 6)
        mfr = dict(record.get("manufacturer_data", {}))
        if APPLE_COMPANY in mfr:
            mfr[APPLE_COMPANY] = _remap_airplay(
                bytes.fromhex(mfr[APPLE_COMPANY]), ip_map
            ).hex()
        record["manufacturer_data"] = mfr
        out.append(record)
    return out


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("infile")
    parser.add_argument("outfile")
    args = parser.parse_args()
    with open(args.infile, encoding="utf-8") as fh:
        records = json.load(fh)
    sanitized = sanitize(records)
    with open(args.outfile, "w", encoding="utf-8") as fh:
        json.dump(sanitized, fh, indent=2)
        fh.write("\n")
    print(f"sanitized {len(sanitized)} records -> {args.outfile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
