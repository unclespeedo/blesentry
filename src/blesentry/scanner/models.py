# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""BLE advertisement value objects and the derived identity fingerprint.

Schema is the normalized record shape documented in
``scripts/capture_scan.py`` and enforced by ``tests/test_fixtures.py``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Advertisement(BaseModel):
    """One observed BLE advertisement, normalized by a capture backend.

    Frozen and closed: an observation is an immutable fact, and new fields
    must land in the capture script and corpus before they appear here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mac: str = Field(min_length=1)
    rssi: int
    local_name: str | None = None
    service_uuids: list[str] = Field(default_factory=list)
    manufacturer_data: dict[str, str] = Field(default_factory=dict)
    service_data: dict[str, str] = Field(default_factory=dict)
    tx_power: int | None = None
    timestamp: float
    adapter_id: str = Field(min_length=1)


class Fingerprint(BaseModel):
    """Hashable identity key derived from a single advertisement.

    Carries the stable signal components only (source address, service
    UUIDs, manufacturer data, advertised name) — never the observation
    artifacts (RSSI, timestamp, adapter). Candidate matching and scoring
    across fingerprints is the resolver's job (P1-7).
    """

    model_config = ConfigDict(frozen=True)

    mac: str | None = None
    service_uuids: frozenset[str] = Field(default_factory=frozenset)
    manufacturer_data: frozenset[tuple[str, str]] = Field(
        default_factory=lambda: frozenset[tuple[str, str]]()
    )
    local_name: str | None = None

    @classmethod
    def from_advertisement(cls, advertisement: Advertisement) -> Fingerprint:
        """Derive the identity key from a single advertisement.

        Set-typed fields are order-independent so equivalent advertisements
        from different capture passes produce equal fingerprints.
        """
        return cls(
            mac=advertisement.mac,
            service_uuids=frozenset(advertisement.service_uuids),
            manufacturer_data=frozenset(
                advertisement.manufacturer_data.items()
            ),
            local_name=advertisement.local_name,
        )
