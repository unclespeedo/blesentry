# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Continuous scan loop (P1-8): scan window -> resolve -> persist.

Identity comes from the fusion resolver (#19, ``resolver.py``,
ADR-0005): weighted signal scoring with a stable-address mismatch
veto, conservative by design — see resolver.py and docs/risks.md for
the fusion limits (payload-variance under-joining, impersonation
surface, window bounds).

Error contract (ADR-0002): scanner failures propagate out of the loop
— the process exits non-zero and the supervisor (systemd, P3-1)
restarts it. A sentinel that cannot scan must never look like a quiet
site.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import NamedTuple

from blesentry.alerts import UnknownDeviceAlerter
from blesentry.detection.familiar import FamiliarSetRefresher
from blesentry.detection.features import band_counts
from blesentry.detection.models import DetectionEvent, DetectionWindow
from blesentry.detection.protocol import Detector
from blesentry.notifier.models import OutboundMessage
from blesentry.presence import PresenceTracker
from blesentry.resolver import DeviceResolver, fingerprint_key
from blesentry.scanner.protocol import Scanner
from blesentry.storage.database import transaction
from blesentry.storage.repository import (
    DeviceRepository,
    ObservationRepository,
    OutboxRepository,
    PresenceEventRepository,
    WindowBandCountRepository,
)

logger = logging.getLogger(__name__)

# Default INFO heartbeat: 60 cycles × (window + pause) ≈ 15 min at
# the shipped 10 s + 5 s cadence. Per-cycle stats stay at DEBUG so
# they do not fill the 64M journald cap (#100).
CYCLE_LOG_ROLLUP_EVERY = 60


class CycleStats(NamedTuple):
    """Summary of one scan cycle."""

    heard: int
    devices: int
    observations: int


def iso_utc(timestamp: float) -> str:
    """Epoch seconds -> the schema's fixed-width UTC format.

    ``docs/schema.md``: ``%Y-%m-%dT%H:%M:%fZ`` with millisecond
    precision, lexically sortable.
    """
    dt = datetime.fromtimestamp(timestamp, tz=UTC)
    millis = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{millis:03d}Z"


__all__ = [
    "CYCLE_LOG_ROLLUP_EVERY",
    "CycleStats",
    "fingerprint_key",
    "iso_utc",
    "run_cycle",
    "run_loop",
]


def _require_resolver_affinity(
    resolver: DeviceResolver, devices: DeviceRepository
) -> None:
    """Raise if a caller-supplied resolver is not the cycle repo (#149)."""
    if resolver.connection is not devices.connection:
        raise ValueError(
            "resolver must share the cycle connection for atomicity"
        )
    if resolver.site_id != devices.site_id:
        raise ValueError("resolver must share the cycle site")


def _detection_alert_text(event: DetectionEvent) -> str:
    """Format a detection event for the outbox (no raw address)."""
    from blesentry.detection.approach import (
        APPROACH_DETECTOR_ID,
        APPROACH_KIND,
    )

    if event.detector == APPROACH_DETECTOR_ID and event.kind == APPROACH_KIND:
        from blesentry.detection.approach_detector import (
            format_approach_alert,
        )

        return format_approach_alert(event)
    return (
        f"Detection {event.detector}/{event.kind} "
        f"at window {event.window_index}."
    )


async def run_cycle(
    scanner: Scanner,
    devices: DeviceRepository,
    observations: ObservationRepository,
    duration: float,
    resolver: DeviceResolver | None = None,
    *,
    window_index: int,
    presence: PresenceTracker | None = None,
    presence_events: PresenceEventRepository | None = None,
    alerter: UnknownDeviceAlerter | None = None,
    detector: Detector | None = None,
    outbox: OutboxRepository | None = None,
    window_band_counts: WindowBandCountRepository | None = None,
    familiar_refresher: FamiliarSetRefresher | None = None,
    now: Callable[[], float] = time.time,
) -> CycleStats:
    """Run one scan window and persist everything heard atomically.

    The whole cycle commits in ONE transaction (#84): on process
    death at most one cycle (~15s) of observations is lost — at or
    inside the durability trade already accepted by docs/schema.md
    (power loss can additionally lose commits since the last WAL
    checkpoint, as schema.md documents).

    Identity comes from the fusion resolver (#19). Its staged ids
    publish only after COMMIT and are discarded on failure — a
    rolled-back cycle can never poison resolver memory (the #84
    lesson). Pass one resolver across cycles (run_loop does) or
    rotation fusion resets every call. A caller-supplied resolver
    must share the cycle ``devices`` connection *and* ``site_id``;
    ``run_cycle`` raises if either differs (#149). Resolve writes
    have to sit inside the cycle transaction, and observation
    ``device_id`` is not site-qualified.

    Presence (P2-1): when a ``presence`` tracker and ``presence_events``
    repository are given (both or neither), each window's per-device
    best RSSI advances the tracker and its ABSENT/PRESENT transitions
    are written to ``presence_events`` *inside the cycle transaction* —
    a transition and the observations that caused it commit together.

    The tracker is in-memory and ``update`` mutates it before COMMIT
    (unlike the resolver, which stages). This is safe ONLY because the
    contract is fail-loud: a rolled-back cycle re-raises out of
    ``run_loop``, the process exits, and it restarts with a fresh
    tracker — it never continues from a half-applied window. A caller
    that catches ``run_cycle`` exceptions to keep scanning MUST discard
    the tracker, or a transition emitted in memory but rolled back in
    the DB would be lost. On a fresh start the tracker is empty (not
    seeded), so a device that was PRESENT before a restart re-emits
    PRESENT once it re-clears ``appear_windows`` — an at-least-once
    duplicate the alert layer (P2-6) must tolerate; seeding is a
    follow-up (#112).

    Detection (A3): when a ``detector`` is given, ``observe`` runs
    inside the same transaction on the in-memory advertisements and
    ``heard`` map (DC-1). Returned events are formatted and enqueued
    to ``outbox`` (required; same connection). The detector mutates
    before COMMIT under the same fail-loud contract as presence.

    ``window_index`` is required (no default). It must be strictly
    increasing across cycles when reusing a stateful detector (A3's
    ``TrajectoryTracker``). ``run_loop`` passes ``0, 1, …``.
    """
    advertisements = await scanner.scan(duration=duration)
    if observations.connection is not devices.connection:
        raise ValueError(
            "repositories must share one connection for cycle atomicity"
        )
    if (presence is None) != (presence_events is None):
        # Fail loud rather than silently tracking nothing (ADR-0002).
        raise ValueError("presence and presence_events must be given together")
    if alerter is not None and presence is None:
        raise ValueError("alerter requires presence to be given")
    if detector is not None and outbox is None:
        raise ValueError("detector requires outbox to be given")
    if (
        presence_events is not None
        and presence_events.connection is not devices.connection
    ):
        raise ValueError(
            "presence_events must share the cycle connection for atomicity"
        )
    if outbox is not None and outbox.connection is not devices.connection:
        raise ValueError(
            "outbox must share the cycle connection for atomicity"
        )
    if outbox is not None and outbox.site_id != devices.site_id:
        raise ValueError("outbox must share the cycle site")
    if (
        window_band_counts is not None
        and window_band_counts.connection is not devices.connection
    ):
        raise ValueError(
            "window_band_counts must share the cycle connection for atomicity"
        )
    if (
        window_band_counts is not None
        and window_band_counts.site_id != devices.site_id
    ):
        raise ValueError("window_band_counts must share the cycle site")
    r = resolver if resolver is not None else DeviceResolver(devices)
    _require_resolver_affinity(r, devices)
    device_ids: set[int] = set()
    heard: dict[int, int] = {}
    persisted = 0
    cycle_at = iso_utc(now())
    try:
        async with transaction(devices.connection):
            for ad in advertisements:
                device_id = await r.resolve(ad)
                await observations.append(
                    device_id=device_id,
                    rssi=ad.rssi,
                    observed_at=iso_utc(ad.timestamp),
                    adapter_id=ad.adapter_id,
                    address_type=ad.address_type,
                    adv_type=ad.adv_type,
                )
                device_ids.add(device_id)
                if device_id not in heard or ad.rssi > heard[device_id]:
                    heard[device_id] = ad.rssi
                persisted += 1
            if presence is not None and presence_events is not None:
                occurred_at = iso_utc(now())
                transitions = presence.update(heard)
                for transition in transitions:
                    await presence_events.append(
                        device_id=transition.device_id,
                        event_type=transition.state.value,
                        occurred_at=occurred_at,
                    )
                # Alert enqueue is atomic with the presence event that
                # triggered it (same cycle transaction).
                if alerter is not None:
                    await alerter.handle(transitions)
            if detector is not None and outbox is not None:
                events = detector.observe(
                    DetectionWindow(
                        index=window_index,
                        advertisements=advertisements,
                        heard=heard,
                    )
                )
                for event in events:
                    await outbox.enqueue(
                        payload=OutboundMessage(
                            text=_detection_alert_text(event)
                        ).model_dump_json()
                    )
                    logger.info(
                        "detection %s/%s window=%d",
                        event.detector,
                        event.kind,
                        event.window_index,
                    )
            if window_band_counts is not None:
                keyed = {
                    str(device_id): rssi for device_id, rssi in heard.items()
                }
                counts = band_counts(keyed)
                await window_band_counts.append(
                    window_index=window_index,
                    observed_at=cycle_at,
                    count_all=counts.count_all,
                    count_far=counts.count_far,
                    count_near=counts.count_near,
                    count_adjacent=counts.count_adjacent,
                )
    except BaseException:
        r.abort()
        raise
    r.commit()
    if window_band_counts is not None:
        await window_band_counts.run_retention_if_due(iso_utc(now()))
    if familiar_refresher is not None:
        await familiar_refresher.refresh_if_due(iso_utc(now()))
    return CycleStats(
        heard=len(advertisements),
        devices=len(device_ids),
        observations=persisted,
    )


def _log_cycle_rollup(
    first: int,
    last: int,
    heard: int,
    device_windows: int,
    observations: int,
) -> None:
    """INFO heartbeat covering cycles ``first`` through ``last`` inclusive.

    ``device_windows`` is the sum of per-cycle unique device counts,
    not a distinct-id union. ``heard`` and ``observations`` are sums.
    """
    logger.info(
        "cycles %d-%d: heard=%d devices=%d observations=%d",
        first,
        last,
        heard,
        device_windows,
        observations,
    )


async def run_loop(
    scanner: Scanner,
    devices: DeviceRepository,
    observations: ObservationRepository,
    *,
    duration: float,
    pause: float,
    max_cycles: int | None = None,
    resolver: DeviceResolver | None = None,
    presence: PresenceTracker | None = None,
    presence_events: PresenceEventRepository | None = None,
    alerter: UnknownDeviceAlerter | None = None,
    detector: Detector | None = None,
    outbox: OutboxRepository | None = None,
    window_band_counts: WindowBandCountRepository | None = None,
    familiar_refresher: FamiliarSetRefresher | None = None,
    now: Callable[[], float] = time.time,
    rollup_every: int = CYCLE_LOG_ROLLUP_EVERY,
) -> int:
    """Scan continuously: window, persist, pause, repeat.

    Args:
        scanner: Any Scanner implementation.
        devices: Device repository (site-scoped).
        observations: Observation repository (same site).
        duration: Scan window length in seconds.
        pause: Sleep between windows in seconds.
        max_cycles: Stop after this many cycles (None = forever).
            Bounded runs are for tests and timeboxed captures.
        resolver: Pre-built resolver — the seam through which config
            (P1-9) supplies tuned thresholds. Must be built over the
            same ``devices`` repo (same connection and ``site_id``);
            ``run_loop`` fail-fasts before ``seed()`` otherwise
            (#149). ``None`` builds a default resolver.
        presence: Pre-built presence tracker, carried across cycles
            (P2-1). ``None`` disables presence tracking.
        presence_events: Repository for presence transitions; required
            when ``presence`` is given, on the same connection.
        alerter: Unknown-device alerter (P2-6); enqueues an alert when
            an unlabeled device becomes PRESENT. ``None`` disables it.
        detector: Adaptive detector (A3). ``None`` skips ``observe``.
            The same instance must be carried across cycles (like
            presence) so fire-once / fade state survives. Mutates
            before COMMIT; a rolled-back cycle must not continue.
        outbox: Scan-connection outbox for detection enqueue (DC-1).
            Required when ``detector`` is given.
        window_band_counts: Optional band-count cache writer (C2).
            When given, appends one F3 band-count row per cycle inside
            the transaction and runs daily retention after COMMIT.
        familiar_refresher: F6 familiar-set rebuild (startup + daily
            after COMMIT). ``None`` skips refresh.
        now: Clock for stamping presence transitions (injectable).
        rollup_every: Emit one INFO heartbeat every this many completed
            cycles (#100). Per-cycle stats stay at DEBUG. Cycle 1 also
            emits one INFO liveness line so a restart is immediately
            visible at INFO. A leftover window is flushed on exit.
            Must be >= 1.

    Returns:
        Number of cycles completed.

    Raises:
        ValueError: If ``rollup_every`` is less than 1, or if a
            caller-supplied resolver does not share the cycle
            connection and ``site_id``.
    """
    if rollup_every < 1:
        raise ValueError("rollup_every must be >= 1")
    cycles = 0
    if resolver is None:
        resolver = DeviceResolver(devices)
    _require_resolver_affinity(resolver, devices)
    await resolver.seed()
    window_first = 1
    heard = 0
    device_windows = 0
    observation_count = 0
    try:
        while max_cycles is None or cycles < max_cycles:
            stats = await run_cycle(
                scanner,
                devices,
                observations,
                duration,
                resolver,
                presence=presence,
                presence_events=presence_events,
                alerter=alerter,
                detector=detector,
                outbox=outbox,
                window_band_counts=window_band_counts,
                familiar_refresher=familiar_refresher,
                window_index=cycles,
                now=now,
            )
            cycles += 1
            logger.debug(
                "cycle %d: heard=%d devices=%d observations=%d",
                cycles,
                stats.heard,
                stats.devices,
                stats.observations,
            )
            if cycles == 1:
                # One INFO per process so journalctl -f shows liveness
                # immediately; the 60-cycle rollup is the steady-state
                # heartbeat (#100). Do not use a "cycle N:" prefix —
                # that shape is DEBUG-only.
                logger.info(
                    "scanning; INFO rollup every %d cycles",
                    rollup_every,
                )
            heard += stats.heard
            device_windows += stats.devices
            observation_count += stats.observations
            if cycles % rollup_every == 0:
                _log_cycle_rollup(
                    window_first,
                    cycles,
                    heard,
                    device_windows,
                    observation_count,
                )
                window_first = cycles + 1
                heard = 0
                device_windows = 0
                observation_count = 0
            if max_cycles is not None and cycles >= max_cycles:
                break
            await asyncio.sleep(pause)
    finally:
        if cycles >= window_first:
            _log_cycle_rollup(
                window_first,
                cycles,
                heard,
                device_windows,
                observation_count,
            )
    return cycles
