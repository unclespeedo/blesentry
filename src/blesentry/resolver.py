# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Fingerprint fusion resolver (P1-7, #19).

Resolves an ``Advertisement`` to an existing device or creates one.
Replaces the loop's provisional exact-fingerprint identity with
weighted scoring across the signal hierarchy the overnight baseline
validated: HAP device ids are near-authoritative; manufacturer-data
payload continuity is the strongest weighted join signal; any exact
address match within the temporally-local window is strong (two
radios cannot share an address concurrently — 2026-08-18 policy);
address *provenance* powers the stable-address mismatch veto rather
than gating the match weight.

Transaction discipline (the #84 lesson): identities created inside a
cycle transaction are staged; the caller invokes :meth:`commit` after
the transaction commits or :meth:`abort` on failure, so a rolled-back
cycle can never poison the resolver's memory with phantom ids.
"""

from __future__ import annotations

import json
from collections import OrderedDict

import aiosqlite

from blesentry.scanner.models import Advertisement, Fingerprint
from blesentry.storage.repository import DeviceRepository

# Signal weights. Sum of non-address signals (0.5 + 0.25 + 0.25) = 1.0;
# the default threshold 0.55 means: full manufacturer-payload equality
# alone is not enough — it needs a name or service-set corroboration.
# Company-id-only overlap (0.3) can never fuse on its own: the
# rotation cloud must not collapse into one device per vendor.
# Weights and the 0.55 default threshold are ACCEPTED-PROVISIONAL
# pending P0-11 labeled walk-test tuning (2026-08-18 policy decision).
_W_MFR_PAYLOAD = 0.5
_W_MFR_COMPANY = 0.3
_W_UUIDS = 0.25
_W_NAME = 0.25
_W_STABLE_ADDRESS = 0.6

_STABLE_TYPES = frozenset({"public", "random_static"})

_APPLE_COMPANY = "76"
_HAP_TYPE = 0x06


def hap_device_id(fingerprint: Fingerprint) -> str | None:
    """Extract the stable HomeKit (HAP) device id, if advertised.

    Apple manufacturer data of Continuity type 0x06 is a HAP pairing
    advertisement: type (1), subtype/length (1), status flags (1),
    then a 6-byte device id that is stable per accessory —
    near-authoritative identity for HomeKit accessories (the
    stable-address mismatch veto runs first in scoring), unaffected
    by the state counter that changes across sightings.
    """
    for company, payload_hex in fingerprint.manufacturer_data:
        if company != _APPLE_COMPANY:
            continue
        try:
            payload = bytes.fromhex(payload_hex)
        except ValueError:
            continue
        if len(payload) >= 9 and payload[0] == _HAP_TYPE:
            return payload[3:9].hex()
    return None


# Exact-key cache bound (matches the #84 cache sizing rationale).
_MAX_KEY_CACHE = 100_000


def fingerprint_key(fingerprint: Fingerprint) -> str:
    """Canonical, deterministic string form of a Fingerprint.

    Sorted containers and sorted JSON keys so equal fingerprints from
    different capture passes serialize identically; versioned so a
    future algorithm change is self-describing in the devices table.
    """
    return json.dumps(
        {
            "v": 2,
            "address": fingerprint.address,
            "service_uuids": sorted(fingerprint.service_uuids),
            "manufacturer_data": sorted(fingerprint.manufacturer_data),
            "local_name": fingerprint.local_name,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def fusion_score(
    candidate: Fingerprint,
    other: Fingerprint,
    *,
    address_type: str | None = None,
    other_address_type: str | None = None,
) -> float:
    """Score two fingerprints' likelihood of being one device.

    ``address_type`` / ``other_address_type`` are each side's address
    provenance. STABLE-ADDRESS MISMATCH VETO: two fingerprints whose
    addresses are both known-stable and different can never be one
    device — identical twin products (two factory-default sensors
    with the same name/payload/uuids; ground-truth case at the test
    site) must stay distinct, or an intruder's identical product
    disappears inside a known identity.
    """
    if (
        address_type in _STABLE_TYPES
        and other_address_type in _STABLE_TYPES
        and candidate.address != other.address
    ):
        return 0.0
    hap_a = hap_device_id(candidate)
    hap_b = hap_device_id(other)
    if hap_a is not None and hap_b is not None:
        # Near-authoritative per-accessory identity (the stable-
        # address veto has already run): equal ids fuse,
        # differing ids veto (covers provenance-null captures too).
        return 1.0 if hap_a == hap_b else 0.0
    score = 0.0
    if candidate.manufacturer_data and other.manufacturer_data:
        if candidate.manufacturer_data == other.manufacturer_data:
            score += _W_MFR_PAYLOAD
        else:
            companies_a = {k for k, _ in candidate.manufacturer_data}
            companies_b = {k for k, _ in other.manufacturer_data}
            if companies_a == companies_b:
                score += _W_MFR_COMPANY
    if (
        candidate.service_uuids
        and candidate.service_uuids == other.service_uuids
    ):
        score += _W_UUIDS
    if candidate.local_name and candidate.local_name == other.local_name:
        score += _W_NAME
    if candidate.address and candidate.address == other.address:
        # Same address within the resolver's temporally-local window
        # is near-certain same-device regardless of provenance: two
        # radios cannot share an address concurrently (2026-08-18
        # fusion-policy decision).
        score += _W_STABLE_ADDRESS
    return score


class DeviceResolver:
    """Stateful resolver over a site's :class:`DeviceRepository`.

    Keeps a bounded window of recently resolved fingerprints —
    rotation joins are temporally local (median rotated-row lifetime
    measured at ~0.3 min) — plus an exact-key cache for the steady
    state. All memory is committed-state only; in-flight identities
    stage in pending maps until :meth:`commit`.
    """

    def __init__(
        self,
        devices: DeviceRepository,
        *,
        min_score: float = 0.55,
        recent_window: int = 512,
    ) -> None:
        """Configure thresholds (constructor params until #21 lands)."""
        self._devices = devices
        self._min_score = min_score
        self._recent_window = recent_window
        self._key_cache: dict[str, int] = {}
        self._recent: OrderedDict[str, tuple[Fingerprint, str | None, int]] = (
            OrderedDict()
        )
        self._pending_keys: dict[str, int] = {}
        self._pending_recent: list[
            tuple[str, Fingerprint, str | None, int]
        ] = []

    @property
    def connection(self) -> aiosqlite.Connection:
        """The repository connection, for the cycle transaction."""
        return self._devices.connection

    async def resolve(self, advertisement: Advertisement) -> int:
        """Resolve to an existing device id or create a new device.

        Must run inside the caller's cycle transaction; pair with
        :meth:`commit` / :meth:`abort`.
        """
        fingerprint = Fingerprint.from_advertisement(advertisement)
        key = fingerprint_key(fingerprint)

        device_id = self._key_cache.get(key)
        if device_id is None:
            device_id = self._pending_keys.get(key)
        if device_id is not None:
            if key in self._recent:
                self._recent.move_to_end(key)
            return device_id

        a_type = advertisement.address_type
        stored = await self._devices.get_by_fingerprint(key)
        if stored is not None:
            self._pending_keys[key] = stored["id"]
            self._pending_recent.append(
                (key, fingerprint, a_type, stored["id"])
            )
            return stored["id"]

        fused = self._best_match(fingerprint, a_type)
        if fused is not None:
            if advertisement.address:
                await self._devices.touch_address(fused, advertisement.address)
            self._pending_keys[key] = fused
            self._pending_recent.append((key, fingerprint, a_type, fused))
            return fused

        device_id = await self._devices.upsert(
            fingerprint=key, address=advertisement.address
        )
        self._pending_keys[key] = device_id
        self._pending_recent.append((key, fingerprint, a_type, device_id))
        return device_id

    def _best_match(
        self, fingerprint: Fingerprint, address_type: str | None
    ) -> int | None:
        best_id: int | None = None
        best_score = 0.0
        pending_view = (
            (fp, at, did) for _, fp, at, did in self._pending_recent
        )
        for candidate_fp, candidate_type, candidate_id in (
            *self._recent.values(),
            *pending_view,
        ):
            score = fusion_score(
                fingerprint,
                candidate_fp,
                address_type=address_type,
                other_address_type=candidate_type,
            )
            if score > best_score:
                best_score = score
                best_id = candidate_id
        if best_score >= self._min_score:
            return best_id
        return None

    def commit(self) -> None:
        """Publish staged identities after the transaction commits."""
        if len(self._key_cache) + len(self._pending_keys) > _MAX_KEY_CACHE:
            self._key_cache.clear()
        self._key_cache.update(self._pending_keys)
        for key, fingerprint, a_type, device_id in self._pending_recent:
            self._recent[key] = (fingerprint, a_type, device_id)
            self._recent.move_to_end(key)
        while len(self._recent) > self._recent_window:
            self._recent.popitem(last=False)
        self._pending_keys.clear()
        self._pending_recent.clear()

    def abort(self) -> None:
        """Discard staged identities after a rolled-back transaction."""
        self._pending_keys.clear()
        self._pending_recent.clear()

    async def seed(self) -> None:
        """Warm fusion memory from the newest stored devices.

        Restores rotation-join continuity across process restarts
        (2026-08-18 fusion-policy decision): the founding fingerprints
        of the most recently updated devices become window candidates.
        Stored keys carry no live provenance; address_type seeds None,
        which the same-address and HAP rules do not need.
        """
        rows = await self._devices.list_recent(self._recent_window)
        # list_recent is newest-first; insert oldest-first so FIFO
        # window eviction discards the oldest seeds, not the newest.
        for row in reversed(rows):
            try:
                data = json.loads(row["fingerprint"])
                if not isinstance(data, dict) or data.get("v") != 2:
                    continue
                fingerprint = Fingerprint(
                    # prefer the touched current address over the
                    # founding-key address: the same-address signal
                    # must survive a restart within an RPA lifetime
                    address=row["address"] or data.get("address"),
                    service_uuids=frozenset(data.get("service_uuids") or []),
                    manufacturer_data=frozenset(
                        (k, v)
                        for k, v in (data.get("manufacturer_data") or [])
                    ),
                    local_name=data.get("local_name"),
                )
            except (ValueError, TypeError):
                # A tampered or corrupt row must never take the
                # sentinel down at boot: skip it (fail-fast is for
                # scanning, not for warming an optional cache).
                continue
            key = row["fingerprint"]
            self._key_cache[key] = row["id"]
            self._recent[key] = (fingerprint, None, row["id"])
