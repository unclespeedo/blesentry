# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Fixture corpus integrity (P0-3 output, P1-1/P1-2 input).

Every JSON file under ``tests/fixtures/`` must match the normalized
advertisement schema documented in ``scripts/capture_scan.py``. Phase-1
models and the ``MockScanner`` replay depend on this shape; break it here
first.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = FIXTURES / "corpus-macos-corebluetooth.json"
REQUIRED_FIELDS = {
    "address",
    "rssi",
    "local_name",
    "service_uuids",
    "manufacturer_data",
    "service_data",
    "tx_power",
    "timestamp",
    "adapter_id",
}


def _corpus_files() -> list[Path]:
    if not FIXTURES.is_dir():
        return []
    return sorted(FIXTURES.glob("*.json"))


def test_fixture_corpus_present() -> None:
    """At least one corpus file exists (the P0-3 live capture)."""
    assert _corpus_files(), (
        "no corpus files under tests/fixtures/ — run "
        "scripts/capture_scan.py first (P0-3)"
    )


@pytest.mark.parametrize("path", _corpus_files(), ids=lambda p: p.name)
def test_corpus_file_is_valid_json_array(path: Path) -> None:
    """Each corpus file parses as a JSON array of records."""
    records = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(records, list)
    assert records, f"{path.name}: empty corpus"


@pytest.mark.parametrize("path", _corpus_files(), ids=lambda p: p.name)
def test_corpus_records_match_schema(path: Path) -> None:
    """Every record carries the required fields with the right types."""
    records = json.loads(path.read_text(encoding="utf-8"))
    for record in records:
        assert isinstance(record, dict)
        missing = REQUIRED_FIELDS - set(record)
        assert not missing, f"{path.name}: missing {sorted(missing)}"
        assert isinstance(record["address"], str) and record["address"]
        assert isinstance(record["rssi"], int)
        assert record["local_name"] is None or isinstance(
            record["local_name"], str
        )
        assert isinstance(record["service_uuids"], list)
        assert all(isinstance(u, str) for u in record["service_uuids"])
        assert isinstance(record["manufacturer_data"], dict)
        assert isinstance(record["service_data"], dict)
        assert record["tx_power"] is None or isinstance(
            record["tx_power"], int
        )
        assert isinstance(record["timestamp"], (int, float))
        assert isinstance(record["adapter_id"], str)
        assert record["adapter_id"]


# ---------------------------------------------------------------------------
# Hygiene (#82): committed corpora must be sanitized per
# tests/fixtures/README.md — no embedded site addresses, synthetic epoch
# ---------------------------------------------------------------------------


def _apple_tlvs(hex_payload: str):
    data = bytes.fromhex(hex_payload)
    i = 0
    while i + 2 <= len(data):
        t, ln = data[i], data[i + 1]
        yield t, data[i + 2 : i + 2 + ln]
        i += 2 + ln


def test_corpus_airplay_tlvs_carry_no_site_addresses() -> None:
    records = json.loads(CORPUS.read_text(encoding="utf-8"))
    checked = 0
    for record in records:
        payload = record.get("manufacturer_data", {}).get("76")
        if not payload:
            continue
        for tlv_type, body in _apple_tlvs(payload):
            if tlv_type == 0x09 and len(body) >= 8:
                ip = tuple(body[2:6])
                port = int.from_bytes(body[6:8], "big")
                assert ip[:3] == (192, 0, 2), (
                    "AirPlay TLV must use TEST-NET-1 per fixtures README"
                )
                assert port == 0
                checked += 1
    assert checked > 0, "corpus should contain AirPlay TLVs to check"


def test_corpus_timestamps_are_epoch_shifted() -> None:
    records = json.loads(CORPUS.read_text(encoding="utf-8"))
    stamps = [r["timestamp"] for r in records]
    assert stamps == sorted(stamps)
    assert stamps[0] == 1600000000.0, (
        "corpus epoch must be the synthetic base per fixtures README"
    )
