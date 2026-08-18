# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""CLI tests (P1-4): one-shot scan command.

The CLI is factored so tests never touch hardware: ``run_scan`` takes
any Scanner (MockScanner here), and the formatters are pure functions
over Advertisement lists.
"""

from __future__ import annotations

import json

import pytest

from blesentry.cli import (
    build_parser,
    format_json,
    format_table,
    parse_or_pattern,
    run_scan,
)
from blesentry.scanner import Advertisement
from blesentry.scanner.mock import MockScanner


def _ad(
    address: str = "AA:BB:CC:DD:EE:FF",
    rssi: int = -65,
    local_name: str | None = "Test Device",
) -> Advertisement:
    return Advertisement(
        address=address,
        rssi=rssi,
        local_name=local_name,
        service_uuids=["180d"],
        manufacturer_data={"76": "010203"},
        timestamp=1755400000.0,
        adapter_id="test-adapter",
    )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_scan_subcommand_parses_duration() -> None:
    args = build_parser().parse_args(["scan", "--duration", "5"])
    assert args.command == "scan"
    assert args.duration == 5.0


def test_scan_duration_has_default() -> None:
    args = build_parser().parse_args(["scan"])
    assert args.duration > 0


def test_scan_json_flag_defaults_off() -> None:
    args = build_parser().parse_args(["scan"])
    assert args.json is False
    args = build_parser().parse_args(["scan", "--json"])
    assert args.json is True


def test_scan_adapter_flag() -> None:
    args = build_parser().parse_args(["scan", "--adapter", "hci0"])
    assert args.adapter == "hci0"


def test_missing_subcommand_errors() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# ---------------------------------------------------------------------------
# or_pattern parsing (START:ADTYPE:HEXBYTES)
# ---------------------------------------------------------------------------


def test_parse_or_pattern() -> None:
    pattern = parse_or_pattern("0:01:06")
    assert pattern.start_position == 0
    assert pattern.ad_data_type == 0x01
    assert pattern.content_of_pattern == b"\x06"


def test_parse_or_pattern_multibyte_content() -> None:
    pattern = parse_or_pattern("2:ff:4c00")
    assert pattern.start_position == 2
    assert pattern.ad_data_type == 0xFF
    assert pattern.content_of_pattern == b"\x4c\x00"


@pytest.mark.parametrize(
    "raw", ["", "0:01", "0:01:06:07", "x:01:06", "0:zz:06", "0:01:0"]
)
def test_parse_or_pattern_rejects_malformed(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_or_pattern(raw)


# ---------------------------------------------------------------------------
# Scan execution (MockScanner — no hardware)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_scan_returns_advertisements() -> None:
    scanner = MockScanner(
        scenarios=[[_ad(), _ad(address="11:22:33:44:55:66")]]
    )
    ads = await run_scan(scanner, duration=1.0)
    assert len(ads) == 2


@pytest.mark.asyncio
async def test_run_scan_empty_window() -> None:
    scanner = MockScanner(scenarios=[])
    assert await run_scan(scanner, duration=1.0) == []


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def test_format_json_round_trips() -> None:
    ads = [_ad(), _ad(address="11:22:33:44:55:66", local_name=None)]
    parsed = json.loads(format_json(ads))
    assert len(parsed) == 2
    assert parsed[0]["address"] == "AA:BB:CC:DD:EE:FF"
    assert parsed[0]["rssi"] == -65
    assert parsed[1]["local_name"] is None


def test_format_json_empty_is_valid_json() -> None:
    assert json.loads(format_json([])) == []


def test_format_table_contains_devices() -> None:
    out = format_table([_ad(rssi=-42, local_name="Kitchen Sensor")])
    assert "AA:BB:CC:DD:EE:FF" in out
    assert "-42" in out
    assert "Kitchen Sensor" in out


def test_format_table_sorts_by_rssi_strongest_first() -> None:
    ads = [
        _ad(address="11:11:11:11:11:11", rssi=-90),
        _ad(address="22:22:22:22:22:22", rssi=-40),
    ]
    out = format_table(ads)
    assert out.index("22:22:22:22:22:22") < out.index("11:11:11:11:11:11")


def test_format_table_handles_no_results() -> None:
    out = format_table([])
    assert "0 device" in out


# ---------------------------------------------------------------------------
# Display-boundary sanitization (#85): device names are untrusted
# ---------------------------------------------------------------------------


def test_format_table_strips_terminal_escapes() -> None:
    out = format_table([_ad(local_name="\x1b]0;pwned\x07\x1b[2Jbad")])
    assert "\x1b" not in out
    assert "\x07" not in out
    assert "bad" in out


def test_format_table_strips_bidi_overrides() -> None:
    out = format_table([_ad(local_name="ab\u202ecd")])
    assert "\u202e" not in out
    assert "ab" in out and "cd" in out


def test_format_json_keeps_names_escaped_not_raw() -> None:
    raw = format_json([_ad(local_name="x\x1b[2Jy")])
    assert "\x1b" not in raw
    assert "\\u001b" in raw
