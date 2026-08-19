# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Config system (P1-9): one TOML file → a validated settings object.

This is where ADR-0002's modularity contract becomes real: selecting a
scanner backend, tuning the resolver, or pointing at a different site is
a config edit, not a code change. ``site_id`` and the database path are
required; everything else has a deployment-sane default.

The file is parsed with stdlib ``tomllib`` (precise, per-file error
messages) and validated by a ``pydantic-settings`` ``BaseSettings``
model, so environment variables (``BLESENTRY_…``, ``__`` for nesting)
fill any field the file omits — the seam through which P2 injects the
notifier secret without writing it to disk.

Fail-fast (ADR-0002 posture): every class of bad input — missing file,
malformed TOML, unknown key, missing required value, wrong type,
out-of-range threshold — raises a single :class:`ConfigError` with a
path-qualified message. A sentinel that cannot start must say why.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from blesentry.scanner.protocol import Scanner

__all__ = [
    "BleakScannerConfig",
    "Config",
    "ConfigError",
    "MockScannerConfig",
    "NotifierConfig",
    "ResolverConfig",
    "ScanConfig",
    "StorageConfig",
    "build_scanner",
    "load_config",
]


class ConfigError(ValueError):
    """Configuration could not be loaded or is invalid.

    Subclasses ``ValueError`` so the CLI's fail-fast handler surfaces it
    with a non-zero exit alongside the other startup value errors.
    """


class _Section(BaseModel):
    """Base for config sections: unknown keys are a hard error.

    ``extra="forbid"`` turns a typo'd knob into a load-time failure
    instead of a silently-ignored setting — the DoD's "clear errors".
    """

    model_config = ConfigDict(extra="forbid")


class StorageConfig(_Section):
    """SQLite storage location (the storage seam's only knob for v1)."""

    db: Path


class ScanConfig(_Section):
    """Scan-loop cadence (P1-8 ``run_loop`` parameters)."""

    window: float = Field(default=10.0, gt=0)
    pause: float = Field(default=5.0, ge=0)
    # Bounded runs are for tests and timeboxed captures; a deployed
    # daemon omits this and scans until SIGTERM.
    max_cycles: int | None = Field(default=None, ge=1)


class ResolverConfig(_Section):
    """Fusion-resolver thresholds (P1-7).

    Defaults mirror :class:`~blesentry.resolver.DeviceResolver` — this
    section retires that class's "constructor params until #21 lands".
    """

    # Upper bound guards against a typo (e.g. 7) that would silently
    # exceed the max achievable fusion score (~1.6 with the current
    # provisional weights; the HAP path caps at 1.0) and disable all
    # fusion. 2.0 leaves headroom for weight retuning; a threshold no
    # advertisement can reach is a misconfig, not a valid setting.
    min_score: float = Field(default=0.55, ge=0, le=2.0)
    recent_window: int = Field(default=512, ge=1)


class NotifierConfig(_Section):
    """Notifier backend selector — a validated stub for Phase 1.

    The Notifier seam has no implementation yet (deferred to P2-5), so
    ``"none"`` is the only accepted backend; any other value fails fast.
    P2-5 widens this to the Telegram backend.
    """

    backend: Literal["none"] = "none"


class BleakScannerConfig(_Section):
    """Options for the production bleak backend (BlueZ / CoreBluetooth).

    ``or_patterns`` are BlueZ advertisement-monitor patterns in the
    ``START:ADTYPE:HEXBYTES`` CLI form; ``None`` means "use the
    provisional default set" (resolved in :func:`build_scanner`). They
    are parsed — and validated — when the scanner is built, so a
    malformed pattern still fails at daemon start.
    """

    backend: Literal["bleak"] = "bleak"
    adapter_id: str = "bluez-linux"
    adapter: str | None = None
    or_patterns: list[str] | None = None


class MockScannerConfig(_Section):
    """Options for the fixture-replay backend (tests, corpus replay)."""

    backend: Literal["mock"]
    corpus: Path | None = None


# Backend is the discriminator: the concrete options model — and thus
# which keys are legal — is chosen by the ``backend`` string. This is
# ADR-0002's "config maps a string key to the class", type-checked.
ScannerConfig = Annotated[
    BleakScannerConfig | MockScannerConfig,
    Field(discriminator="backend"),
]


class Config(BaseSettings):
    """The whole of blesentry's runtime configuration.

    ``site_id`` and ``storage.db`` are required; every other section
    defaults to deployment-sane values so a minimal file is two lines.
    """

    model_config = SettingsConfigDict(
        env_prefix="BLESENTRY_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    site_id: str = Field(min_length=1)
    storage: StorageConfig
    scan: ScanConfig = ScanConfig()
    scanner: ScannerConfig = BleakScannerConfig()
    resolver: ResolverConfig = ResolverConfig()
    notifier: NotifierConfig = NotifierConfig()


def _format_errors(exc: ValidationError) -> str:
    """Render pydantic errors as one clear ``loc: message`` line each."""
    lines = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "(root)"
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config(path: str | Path) -> Config:
    """Load and validate a TOML config file.

    Args:
        path: Path to the TOML config file.

    Returns:
        The validated :class:`Config`. Fields absent from the file fall
        back to environment variables (``BLESENTRY_…``), then defaults.

    Raises:
        ConfigError: the file is missing or unreadable, is not valid
            TOML, or fails validation (unknown key, missing required
            value, wrong type, out-of-range threshold). The message is
            path-qualified and names the offending field.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    try:
        return Config(**data)
    except ValidationError as exc:
        # `from None`, not `from exc`: pydantic's ValidationError repr
        # embeds each field's input value, and this seam is built to
        # carry P2's notifier secret (env overlay). Severing the cause
        # chain keeps a mistyped-secret value out of any downstream
        # traceback. `_format_errors` emits only loc + message.
        raise ConfigError(
            f"invalid config in {path}:\n{_format_errors(exc)}"
        ) from None


def build_scanner(scanner: ScannerConfig) -> Scanner:
    """Construct the configured Scanner backend (ADR-0002 selection).

    Backends are imported lazily so an unused one never adds to the
    import-time footprint — important on the 512 MB target. ``bleak``
    parses its ``or_patterns`` here (falling back to the provisional
    default set), which is where a malformed pattern fails fast.

    Args:
        scanner: The validated scanner section from :class:`Config`.

    Returns:
        A ready-to-use Scanner implementation.

    Raises:
        ValueError: a bleak ``or_pattern`` string is malformed, or the
            bleak backend rejects the platform configuration.
    """
    if isinstance(scanner, MockScannerConfig):
        from blesentry.scanner.mock import MockScanner

        if scanner.corpus is not None:
            return MockScanner.from_corpus(scanner.corpus)
        return MockScanner(scenarios=[])

    from blesentry.scanner.bleak import BleakScanner
    from blesentry.scanner.patterns import (
        DEFAULT_OR_PATTERNS,
        parse_or_pattern,
    )

    raw = (
        scanner.or_patterns
        if scanner.or_patterns is not None
        else list(DEFAULT_OR_PATTERNS)
    )
    return BleakScanner(
        adapter_id=scanner.adapter_id,
        adapter=scanner.adapter,
        or_patterns=[parse_or_pattern(p) for p in raw],
    )
