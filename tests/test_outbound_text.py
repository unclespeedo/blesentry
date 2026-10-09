# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Tests for outbound plain-text sanitization."""

from __future__ import annotations

import pytest

from blesentry.outbound_text import (
    OPERATOR_NAME_MAX_LEN,
    sanitize_operator_name,
)


def test_sanitize_operator_name_collapses_whitespace() -> None:
    assert sanitize_operator_name("  Guest\n BLE  ") == "Guest BLE"


def test_sanitize_operator_name_rejects_blank() -> None:
    assert sanitize_operator_name("   ") is None
    assert sanitize_operator_name("") is None


def test_sanitize_operator_name_strips_controls() -> None:
    assert sanitize_operator_name("Evil\x00name") == "Evil name"


def test_sanitize_operator_name_truncates() -> None:
    long_name = "x" * (OPERATOR_NAME_MAX_LEN + 20)
    result = sanitize_operator_name(long_name)
    assert result is not None
    assert len(result) == OPERATOR_NAME_MAX_LEN


def test_sanitize_operator_name_max_len_validation() -> None:
    with pytest.raises(ValueError, match="max_len"):
        sanitize_operator_name("ok", max_len=0)
