# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Value objects crossing the Detector seam (ADR-0006).

``DetectionWindow`` is one scan window of in-memory observations;
``DetectionEvent`` is one would-be alert the detector returns. Both
are frozen and closed (``extra="forbid"``), mirroring
``Advertisement`` / ``OutboundMessage``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)

from blesentry.scanner.models import Advertisement

type ProximityBand = Literal["adjacent", "near", "far", "beyond-far"]


class DetectionWindow(BaseModel):
    """One scan window fed to :meth:`Detector.observe`.

    Clock-free: ``index`` is an ordinal, not a wall-clock timestamp
    (ADR-0006, DC-4 / DC-9). ``advertisements`` is the pre-fusion
    stream; ``heard`` is the post-resolve per-device best RSSI.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    advertisements: Sequence[Advertisement] = Field(default_factory=tuple)
    heard: Mapping[int, int] = Field(default_factory=dict)

    def model_post_init(self, __context: object, /) -> None:
        """Freeze containers so callers cannot mutate through us."""
        object.__setattr__(self, "advertisements", tuple(self.advertisements))
        object.__setattr__(self, "heard", MappingProxyType(dict(self.heard)))

    @field_serializer("advertisements")
    def _serialize_advertisements(
        self, value: Sequence[Advertisement]
    ) -> list[Advertisement]:
        """Dump the frozen tuple as a plain list."""
        return list(value)

    @field_serializer("heard")
    def _serialize_heard(self, value: Mapping[int, int]) -> dict[int, int]:
        """Dump MappingProxyType as a plain dict."""
        return dict(value)


class DetectionEvent(BaseModel):
    """One would-be alert produced from a window.

    ``kind`` is detector-defined; A1 / C1 / I1 freeze their tokens.
    ``window_index`` is the clock-free timestamp (the window's index).
    Additive A3 fields (``rssi`` / ``band`` / ``rising``) default to
    ``None`` so ``mock`` events stay three tokens (ADR-0006).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    detector: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    window_index: int = Field(ge=0)
    rssi: int | None = None
    band: ProximityBand | None = None
    rising: bool | None = None
    count: int | None = Field(default=None, ge=1)
    contributors: tuple[int, ...] | None = None

    @field_validator("contributors")
    @classmethod
    def _contributors_non_empty(
        cls, value: tuple[int, ...] | None
    ) -> tuple[int, ...] | None:
        if value is not None and len(value) == 0:
            raise ValueError("contributors must be non-empty when set")
        return value
