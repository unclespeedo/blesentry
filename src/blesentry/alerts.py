# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Unknown-device alerting (P2-6): PRESENT of an unlabeled device → alert.

When the presence state machine (P2-1) reports a device going PRESENT and
that device has no operator label, an interactive alert is enqueued to
the outbox (delivered by the drain through the Notifier). The operator
replies with ``/label <id> <name>`` (P2-8) to name it — or ``/ignore
<id>`` to acknowledge it without a name; either sets a label, which
closes the alert gate, so a labeled device never re-alerts.

Gating on ``label IS NULL`` (not an in-memory set) makes this
restart-safe and self-explaining: the presence machine emits one PRESENT
per visit, a labeled device is silent, and a still-unknown device that
genuinely leaves and returns re-nags — the right behaviour for a
sentinel. Alerts flow through the outbox (ADR-0003), never
fire-and-forget, and are sent as plain text (the device address is the
only untrusted field and cannot inject markup).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from blesentry.notifier.models import OutboundMessage
from blesentry.presence import PresenceState, PresenceTransition
from blesentry.storage.repository import (
    DeviceRepository,
    DeviceRow,
    OutboxRepository,
)

logger = logging.getLogger(__name__)


def _safe_address(address: str) -> str:
    """Neutralise an untrusted, radio-sourced address for the alert.

    The address is length-bounded but not charset-bounded upstream. Even
    sent as plain text, a newline could forge a second (tappable)
    command line in the operator's alert, so control/format chars are
    replaced and whitespace collapsed — the posture ``cli._display_text``
    uses for the same field.
    """
    cleaned = "".join(ch if ch.isprintable() else " " for ch in address)
    return " ".join(cleaned.split()) or "unknown address"


def _alert_text(device: DeviceRow) -> str:
    device_id = device["id"]
    address = device["address"]
    shown = _safe_address(address) if address else "unknown address"
    return (
        f"Unknown device #{device_id} is present [{shown}].\n"
        f"Name it:  /label {device_id} <name>\n"
        f"Ignore it:  /ignore {device_id}"
    )


class UnknownDeviceAlerter:
    """Enqueue an interactive alert when an unlabeled device goes PRESENT.

    Built on the scan connection so an alert enqueue is atomic with the
    presence transition that triggered it (run_cycle calls ``handle``
    inside the cycle transaction).
    """

    def __init__(
        self, devices: DeviceRepository, outbox: OutboxRepository
    ) -> None:
        """Initialise with the device and outbox repositories."""
        self._devices = devices
        self._outbox = outbox

    async def handle(self, transitions: Iterable[PresenceTransition]) -> int:
        """Alert for each unlabeled device that just became PRESENT.

        ABSENT transitions, already-labeled devices, and devices that
        vanished before this runs are skipped. Returns how many alerts
        were enqueued.
        """
        alerted = 0
        for transition in transitions:
            if transition.state is not PresenceState.PRESENT:
                continue
            device = await self._devices.get(transition.device_id)
            if device is None or device["label"] is not None:
                # Vanished, or already known — nothing to ask about.
                continue
            await self._outbox.enqueue(
                payload=OutboundMessage(
                    text=_alert_text(device)
                ).model_dump_json()
            )
            logger.info("alerted unknown device %d", transition.device_id)
            alerted += 1
        return alerted
