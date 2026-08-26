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
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from blesentry.notifier.protocol import Notifier
    from blesentry.presence import PresenceTracker
    from blesentry.scanner.protocol import Scanner

__all__ = [
    "BleakScannerConfig",
    "Config",
    "ConfigError",
    "MockScannerConfig",
    "NoneNotifierConfig",
    "NotifierConfig",
    "PresenceConfig",
    "ResolverConfig",
    "ScanConfig",
    "StorageConfig",
    "TelegramNotifierConfig",
    "build_notifier",
    "build_presence",
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
    """Fusion-resolver thresholds (P1-7, ADR-0005).

    Defaults mirror :class:`~blesentry.resolver.DeviceResolver`.
    ``min_score`` must exceed the company-only fusion weight or a
    vendor's rotation cloud collapses into one device.
    """

    # Upper bound guards against a typo (e.g. 7) that would silently
    # exceed the max achievable fusion score (~1.6 with the current
    # provisional weights; the HAP path caps at 1.0) and disable all
    # fusion. 2.0 leaves headroom for weight retuning; a threshold no
    # advertisement can reach is a misconfig, not a valid setting.
    # Lower bound is the company-only weight (ADR-0005), applied in
    # ``_above_company_only`` — Field(ge=0) would still admit vendor
    # collapse.
    min_score: float = Field(default=0.55, ge=0, le=2.0)
    recent_window: int = Field(default=512, ge=1)

    @field_validator("min_score")
    @classmethod
    def _above_company_only(cls, value: float) -> float:
        """Reject a floor that lets company-id-only overlap fuse."""
        from blesentry.resolver import COMPANY_ONLY_WEIGHT

        if value <= COMPANY_ONLY_WEIGHT:
            raise ValueError(
                "must exceed the company-only fusion weight "
                f"({COMPANY_ONLY_WEIGHT}) or a vendor collapses "
                "to one device"
            )
        return value


class PresenceConfig(_Section):
    """Presence state-machine thresholds (P2-1 debounce + cooldown).

    These are the operator's dwell-and-proximity dials — the levers that
    turn a device-dense site from an alert firehose into signal. Counts
    are in *scan windows*, not seconds: one window is ``[scan] window +
    [scan] pause`` long, so at the defaults (10 s + 5 s) three windows is
    ~45 s. Defaults mirror
    :class:`~blesentry.presence.PresenceTracker`; see ``docs/tuning.md``.
    """

    # Consecutive above-gate windows before a device counts as PRESENT.
    # Higher = slower to fire, more resistant to a passer-by.
    appear_windows: int = Field(default=3, ge=1)
    # Consecutive missed windows before a PRESENT device counts as ABSENT.
    disappear_windows: int = Field(default=3, ge=1)
    # Proximity gate: the minimum RSSI (dBm) for a window to count as a
    # hit. RSSI is negative and rises toward 0 with proximity, so RAISING
    # this (e.g. -80 -> -65) admits only nearby devices — the primary
    # lever for a dense site. Bounded [-127, 0): a value of 0 or more is a
    # sign typo that would silence the sentinel — no real received signal
    # reaches 0 dBm, so nothing would ever count as a hit.
    rssi_threshold: int = Field(default=-80, ge=-127, lt=0)
    # Windows after a device goes ABSENT during which a return is treated
    # as the same visit (no re-emit). 0 = every return is a new visit; to
    # affect a device that returns immediately it must exceed
    # ``appear_windows`` (reconfirming PRESENT itself costs that many).
    cooldown_windows: int = Field(default=0, ge=0)
    # Drop an ABSENT device from tracker memory after this many missed
    # windows, bounding RAM on the 512 MB target. ``None`` -> the
    # tracker's default of ``4 * disappear_windows``.
    prune_after_windows: int | None = Field(default=None, ge=1)


class NoneNotifierConfig(_Section):
    """The disabled backend — a daemon that runs without alerting."""

    backend: Literal["none"] = "none"


class TelegramNotifierConfig(_Section):
    """Telegram backend (ADR-0003): long-poll ``getUpdates``, no webhook.

    ``bot_token`` is a secret — a :class:`~pydantic.SecretStr`, so it
    never lands in a repr, log line, or traceback — supplied via the
    environment (``BLESENTRY_NOTIFIER__BOT_TOKEN``) or a gitignored
    local file, never the committed config (SECURITY.md, ADR-0003).
    ``chat_id`` and ``user_id`` are the single authorized operator pair
    the ADR-0003 auth rule matches against.
    """

    backend: Literal["telegram"]
    bot_token: SecretStr = Field(min_length=1)
    chat_id: int
    user_id: int
    # Long-poll hold time for getUpdates; the outbound HTTP request the
    # daemon parks on behind CGNAT (ADR-0003).
    poll_timeout: float = Field(default=30.0, gt=0)


# Backend is the discriminator, mirroring ScannerConfig: the concrete
# options model — and thus which keys are legal — is chosen by the
# ``backend`` string (ADR-0002's "config maps a string key to a class").
NotifierConfig = Annotated[
    NoneNotifierConfig | TelegramNotifierConfig,
    Field(discriminator="backend"),
]


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
    presence: PresenceConfig = PresenceConfig()
    notifier: NotifierConfig = NoneNotifierConfig()


def _safe_message(err: Mapping[str, object]) -> str:
    """Return a pydantic error message with any echoed input scrubbed.

    Standard pydantic messages state the *expected* shape, never the
    input — safe to render verbatim. The exception is a discriminated
    union's ``union_tag_invalid``, which embeds the offending tag
    (``notifier.backend`` here) in its message. This seam carries the
    bot-token secret, so the tag is redacted while the (static, safe)
    expected-tags guidance is kept. The secret itself is a ``SecretStr``
    and never reaches an error message.
    """
    msg = str(err["msg"])
    ctx = err.get("ctx") or {}
    tag = ctx.get("tag") if isinstance(ctx, dict) else None
    if tag:
        msg = msg.replace(str(tag), "***")
    return msg


def _format_errors(exc: ValidationError) -> str:
    """Render pydantic errors as one clear ``loc: message`` line each."""
    lines = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"]) or "(root)"
        lines.append(f"  {loc}: {_safe_message(err)}")
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


def build_presence(presence: PresenceConfig) -> PresenceTracker:
    """Construct the presence state machine from its config section.

    A straight pass-through of the debounce and proximity thresholds into
    :class:`~blesentry.presence.PresenceTracker`. Unlike the resolver, the
    tracker needs no repository, so it is fully built here (the daemon
    just hands it to the scan loop). The ``PresenceTracker`` constructor
    re-validates the counts, so a bad value fails fast either way.

    Args:
        presence: The validated presence section from :class:`Config`.

    Returns:
        A ready-to-use :class:`~blesentry.presence.PresenceTracker`.
    """
    from blesentry.presence import PresenceTracker

    return PresenceTracker(
        appear_windows=presence.appear_windows,
        disappear_windows=presence.disappear_windows,
        rssi_threshold=presence.rssi_threshold,
        cooldown_windows=presence.cooldown_windows,
        prune_after_windows=presence.prune_after_windows,
    )


def build_notifier(notifier: NotifierConfig) -> Notifier:
    """Construct the configured Notifier backend (ADR-0002 selection).

    Backends are imported lazily so the ``none`` path never drags the
    Telegram HTTP stack into the import graph — important on the 512 MB
    target. The secret is unwrapped from its :class:`~pydantic.SecretStr`
    only here, at the boundary where the transport actually needs it.

    Args:
        notifier: The validated notifier section from :class:`Config`.

    Returns:
        A ready-to-use Notifier implementation.
    """
    if isinstance(notifier, TelegramNotifierConfig):
        from blesentry.notifier.telegram import TelegramNotifier

        return TelegramNotifier(
            bot_token=notifier.bot_token.get_secret_value(),
            chat_id=notifier.chat_id,
            user_id=notifier.user_id,
            poll_timeout=notifier.poll_timeout,
        )

    from blesentry.notifier.null import NullNotifier

    return NullNotifier()
