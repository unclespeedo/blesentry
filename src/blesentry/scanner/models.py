# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""BLE advertisement value objects and the derived identity fingerprint.

Schema is the normalized record shape documented in
``scripts/capture_scan.py`` and enforced by ``tests/test_fixtures.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class Advertisement(BaseModel):
    """One observed BLE advertisement, normalized by a capture backend.

    Frozen and closed: an observation is an immutable fact, and new fields
    must land in the capture script and corpus before they appear here.
    Container fields are converted to ``tuple`` / ``MappingProxyType`` at
    construction so in-place mutation is impossible and a derived
    ``Fingerprint`` can never diverge from its source.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mac: str = Field(min_length=1)
    rssi: int
    local_name: str | None = None
    service_uuids: Sequence[str] = Field(default_factory=tuple)
    manufacturer_data: Mapping[str, str] = Field(default_factory=dict)
    service_data: Mapping[str, str] = Field(default_factory=dict)
    tx_power: int | None = None
    timestamp: float
    adapter_id: str = Field(min_length=1)

    def model_post_init(self, __context: object, /) -> None:
        """Replace mutable containers with immutable equivalents."""
        object.__setattr__(self, "service_uuids", tuple(self.service_uuids))
        object.__setattr__(
            self,
            "manufacturer_data",
            MappingProxyType(dict(self.manufacturer_data)),
        )
        object.__setattr__(
            self, "service_data", MappingProxyType(dict(self.service_data))
        )

    @field_serializer("service_uuids")
    def _serialize_service_uuids(self, value: Sequence[str]) -> list[str]:
        """Dump the frozen tuple as a plain list (#74).

        pydantic's serializer warns on the post-init container types;
        explicit serializers keep dumps warning-free and JSON-clean.
        """
        return list(value)

    @field_serializer("manufacturer_data", "service_data")
    def _serialize_mapping(self, value: Mapping[str, str]) -> dict[str, str]:
        """Dump MappingProxyType as a plain dict (#74)."""
        return dict(value)


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
