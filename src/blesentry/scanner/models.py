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

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

# Radio-controlled fields are bounded (#85): the model must not assume
# the radio is honest. GAP's device-name maximum is 248 octets (the
# code-point Field cap is looser; the validator enforces the octet
# cap). 64 entries / 64-char keys / 4096-char values are far beyond
# any legitimate advertisement.
MAX_NAME_LENGTH = 248
MAX_CONTAINER_ENTRIES = 64
MAX_KEY_LENGTH = 64
MAX_VALUE_LENGTH = 4096


class Advertisement(BaseModel):
    """One observed BLE advertisement, normalized by a capture backend.

    Frozen and closed: an observation is an immutable fact, and new fields
    must land in the capture script and corpus before they appear here.
    Container fields are converted to ``tuple`` / ``MappingProxyType`` at
    construction so in-place mutation is impossible and a derived
    ``Fingerprint`` can never diverge from its source.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    address: str = Field(min_length=1, max_length=64)
    address_type: str | None = Field(default=None, max_length=32)
    adv_type: str | None = Field(default=None, max_length=32)
    rssi: int
    local_name: str | None = Field(default=None, max_length=MAX_NAME_LENGTH)
    service_uuids: Sequence[str] = Field(
        default_factory=tuple, max_length=MAX_CONTAINER_ENTRIES
    )
    manufacturer_data: Mapping[str, str] = Field(default_factory=dict)
    service_data: Mapping[str, str] = Field(default_factory=dict)
    tx_power: int | None = None
    timestamp: float
    adapter_id: str = Field(min_length=1)

    @field_validator("local_name")
    @classmethod
    def _bound_name(cls, value: str | None) -> str | None:
        """Enforce the GAP 248-octet cap (UTF-8 bytes, not code points).

        pydantic-core already rejects lone surrogates during str
        validation; the encode here is a backstop for older 2.x cores
        and makes the octet cap enforceable in one place.
        """
        if value is not None and len(value.encode("utf-8")) > MAX_NAME_LENGTH:
            raise ValueError(f"local_name exceeds {MAX_NAME_LENGTH} bytes")
        return value

    @field_validator("service_uuids")
    @classmethod
    def _bound_uuids(cls, value: Sequence[str]) -> Sequence[str]:
        """Cap per-entry UUID string size; require UTF-8-clean text."""
        for item in value:
            if len(item) > MAX_KEY_LENGTH:
                raise ValueError(
                    f"service uuid exceeds {MAX_KEY_LENGTH} chars"
                )
            item.encode("utf-8")
        return value

    @field_validator("manufacturer_data", "service_data")
    @classmethod
    def _bound_mapping(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        """Cap cardinality and per-entry sizes (radio-controlled input)."""
        if len(value) > MAX_CONTAINER_ENTRIES:
            raise ValueError(
                f"mapping exceeds {MAX_CONTAINER_ENTRIES} entries"
            )
        for key, item in value.items():
            if len(key) > MAX_KEY_LENGTH:
                raise ValueError(f"key exceeds {MAX_KEY_LENGTH} chars")
            if len(item) > MAX_VALUE_LENGTH:
                raise ValueError(f"value exceeds {MAX_VALUE_LENGTH} chars")
            key.encode("utf-8")
            item.encode("utf-8")
        return value

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
    """Hashable sighting key derived from a single advertisement.

    Carries the stable-looking signal components of that one observation
    (source address, service UUIDs, manufacturer data, advertised name)
    — never the observation artifacts (RSSI, timestamp, adapter).

    Equality is not an identity test. BLE MAC randomization (especially
    Apple rotating public addresses) means two observations of the same
    physical device produce unequal fingerprints when the address
    rotates. Use ``==`` and hashing only as an exact-sighting cache
    key. Joining rotated sightings is the resolver's job via fuzzy
    scoring (``fusion_score``, P1-7 / ADR-0005), not ``Fingerprint``
    equality.
    """

    model_config = ConfigDict(frozen=True)

    address: str | None = None
    service_uuids: frozenset[str] = Field(default_factory=frozenset)
    manufacturer_data: frozenset[tuple[str, str]] = Field(
        default_factory=lambda: frozenset[tuple[str, str]]()
    )
    local_name: str | None = None

    @classmethod
    def from_advertisement(cls, advertisement: Advertisement) -> Fingerprint:
        """Derive the sighting key from a single advertisement.

        Set-typed fields are order-independent so equivalent advertisements
        from different capture passes produce equal fingerprints. Equal
        fingerprints are the same *sighting shape*, not proof of the same
        device: a rotated MAC yields a different fingerprint (see the
        class docstring).
        """
        return cls(
            address=advertisement.address,
            service_uuids=frozenset(advertisement.service_uuids),
            manufacturer_data=frozenset(
                advertisement.manufacturer_data.items()
            ),
            local_name=advertisement.local_name,
        )
