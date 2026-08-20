# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Single-operator authorization — the ADR-0003 rule in one place.

ADR-0003, stated verbatim:

    An inbound message is authorized if and only if the configured
    ``chat_id`` AND the configured ``user_id`` both match the message's
    origin. Either mismatch → the message is ignored and logged.

Kept as a pure predicate so the rule has exactly one definition, shared
by every backend and directly unit-testable without a transport.
"""

from __future__ import annotations


def is_authorized(
    *,
    origin_chat_id: int,
    origin_user_id: int,
    allowed_chat_id: int,
    allowed_user_id: int,
) -> bool:
    """Return ``True`` iff both the chat id and the user id match.

    Args:
        origin_chat_id: Chat the inbound message came from.
        origin_user_id: User who sent the inbound message.
        allowed_chat_id: The single configured operator chat.
        allowed_user_id: The single configured operator user.

    Returns:
        ``True`` only when both ids match; any mismatch is ``False``.
    """
    return (
        origin_chat_id == allowed_chat_id and origin_user_id == allowed_user_id
    )
