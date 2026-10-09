# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Sanitize operator-facing plain text (Telegram / outbox).

Radio-sourced names and operator labels can embed controls or exceed
Telegram's message cap. One-line, printable, length-bounded cleaning
for strings embedded in outbound alerts and digests.
"""

from __future__ import annotations

# Telegram Bot API hard limit; see commands.py list pagination notes.
TELEGRAM_MESSAGE_MAX_LEN = 4096

# Max length for a device name embedded inside a longer alert line.
OPERATOR_NAME_MAX_LEN = 40


def sanitize_operator_name(
    name: str,
    *,
    max_len: int = OPERATOR_NAME_MAX_LEN,
) -> str | None:
    """Return a one-line printable name, or ``None`` if nothing remains.

    Collapses whitespace, strips non-printable code points, and truncates
    so a long ``/label`` or ``local_name`` cannot reject the whole alert.
    """
    if max_len < 1:
        raise ValueError("max_len must be >= 1")
    cleaned = "".join(ch if ch.isprintable() else " " for ch in name)
    collapsed = " ".join(cleaned.split())
    if not collapsed:
        return None
    return collapsed[:max_len]
