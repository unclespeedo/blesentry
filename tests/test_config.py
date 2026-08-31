# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Config system tests (P1-9): TOML + pydantic-settings.

The config is the point where ADR-0002's modularity contract becomes
real: swapping a scanner backend is a config edit. These tests pin the
three DoD guarantees — an example config that loads, fail-fast on every
class of invalid input, and every Phase-1 knob wired through to the
component that consumes it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from blesentry.config import (
    BleakScannerConfig,
    Config,
    ConfigError,
    MockDetectionConfig,
    MockScannerConfig,
    NoneDetectionConfig,
    PresenceConfig,
    SummaryConfig,
    build_detector,
    build_presence,
    build_scanner,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "config.example.toml"


@pytest.fixture(autouse=True)
def _clear_blesentry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip inherited BLESENTRY_* so the env overlay never leaks in.

    ``load_config`` intentionally lets environment variables fill fields
    the file omits; a stray var in CI/dev would otherwise flip an
    assertion. Tests that want env overlay set their own var after this.
    """
    import os

    for key in list(os.environ):
        if key.startswith("BLESENTRY_"):
            monkeypatch.delenv(key, raising=False)


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


MINIMAL = """
site_id = "example-site"

[storage]
db = "/var/lib/blesentry/blesentry.db"
"""

FULL = """
site_id = "example-site"

[storage]
db = "/var/lib/blesentry/blesentry.db"

[scan]
window = 12.0
pause = 3.0
max_cycles = 100

[scanner]
backend = "bleak"
adapter_id = "bluez-linux"
adapter = "hci0"
or_patterns = ["0:01:06", "0:01:1a"]

[resolver]
min_score = 0.7
recent_window = 256

[presence]
appear_windows = 5
disappear_windows = 4
rssi_threshold = -70
cooldown_windows = 6
prune_after_windows = 20

[notifier]
backend = "none"

[detection]
backend = "mock"

[summary]
enabled = false
hour_utc = 8
"""


# ---------------------------------------------------------------------------
# Happy path: defaults and full wiring
# ---------------------------------------------------------------------------


def test_minimal_config_applies_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, MINIMAL))
    assert cfg.site_id == "example-site"
    assert cfg.storage.db == Path("/var/lib/blesentry/blesentry.db")
    # Every optional section falls back to a sane default.
    assert cfg.scan.window > 0
    assert cfg.scan.pause >= 0
    assert cfg.scan.max_cycles is None
    assert isinstance(cfg.scanner, BleakScannerConfig)
    assert cfg.scanner.backend == "bleak"
    assert cfg.resolver.min_score == 0.55
    assert cfg.resolver.recent_window == 512
    # Presence debounce falls back to the P2-1 tracker defaults.
    assert cfg.presence.appear_windows == 3
    assert cfg.presence.disappear_windows == 3
    assert cfg.presence.rssi_threshold == -80
    assert cfg.presence.cooldown_windows == 0
    assert cfg.presence.prune_after_windows is None
    assert cfg.notifier.backend == "none"
    assert isinstance(cfg.detection, NoneDetectionConfig)
    assert cfg.detection.backend == "none"
    assert cfg.summary.enabled is True
    assert cfg.summary.hour_utc == 12
    assert isinstance(cfg.summary, SummaryConfig)


def test_full_config_wires_every_knob(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, FULL))
    assert cfg.scan.window == 12.0
    assert cfg.scan.pause == 3.0
    assert cfg.scan.max_cycles == 100
    assert isinstance(cfg.scanner, BleakScannerConfig)
    assert cfg.scanner.adapter_id == "bluez-linux"
    assert cfg.scanner.adapter == "hci0"
    assert cfg.scanner.or_patterns == ["0:01:06", "0:01:1a"]
    assert cfg.resolver.min_score == 0.7
    assert cfg.resolver.recent_window == 256
    assert cfg.presence.appear_windows == 5
    assert cfg.presence.disappear_windows == 4
    assert cfg.presence.rssi_threshold == -70
    assert cfg.presence.cooldown_windows == 6
    assert cfg.presence.prune_after_windows == 20
    assert cfg.summary.enabled is False
    assert cfg.summary.hour_utc == 8
    assert isinstance(cfg.detection, MockDetectionConfig)
    assert cfg.detection.backend == "mock"


def test_summary_hour_utc_boundaries_accepted(tmp_path: Path) -> None:
    for hour in (0, 23):
        body = MINIMAL + f"\n[summary]\nhour_utc = {hour}\n"
        cfg = load_config(_write(tmp_path, body))
        assert cfg.summary.hour_utc == hour


def test_config_type_is_base_settings(tmp_path: Path) -> None:
    # pydantic-settings gives env overlay for omitted fields for free.
    from pydantic_settings import BaseSettings

    assert issubclass(Config, BaseSettings)


def test_env_fills_omitted_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = '[storage]\ndb = "/tmp/x.db"\n'
    monkeypatch.setenv("BLESENTRY_SITE_ID", "env-site")
    cfg = load_config(_write(tmp_path, body))
    assert cfg.site_id == "env-site"


# ---------------------------------------------------------------------------
# Scanner backend selection (ADR-0002 discriminated union)
# ---------------------------------------------------------------------------


def test_mock_backend_selected(tmp_path: Path) -> None:
    body = (
        'site_id = "s"\n[storage]\ndb = "/tmp/x.db"\n'
        '[scanner]\nbackend = "mock"\ncorpus = "/tmp/corpus.json"\n'
    )
    cfg = load_config(_write(tmp_path, body))
    assert isinstance(cfg.scanner, MockScannerConfig)
    assert cfg.scanner.corpus == Path("/tmp/corpus.json")


def test_unknown_scanner_backend_rejected(tmp_path: Path) -> None:
    body = (
        'site_id = "s"\n[storage]\ndb = "/tmp/x.db"\n'
        '[scanner]\nbackend = "carrier-pigeon"\n'
    )
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, body))


def test_unknown_detection_backend_rejected(tmp_path: Path) -> None:
    body = (
        'site_id = "s"\n[storage]\ndb = "/tmp/x.db"\n'
        '[detection]\nbackend = "crowd"\n'
    )
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, body))


# ---------------------------------------------------------------------------
# Fail-fast on every class of invalid input (DoD: clear errors)
# ---------------------------------------------------------------------------


def test_missing_file_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "nope.toml")
    assert "not found" in str(exc.value).lower()


def test_malformed_toml_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, "site_id = \nthis is not toml"))
    assert "toml" in str(exc.value).lower()


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    body = MINIMAL + '\ntypo_knob = "oops"\n'
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, body))
    assert "typo_knob" in str(exc.value)


def test_unknown_nested_key_rejected(tmp_path: Path) -> None:
    body = MINIMAL + "\n[resolver]\nmin_scor = 0.5\n"
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, body))


def test_unknown_detection_key_rejected(tmp_path: Path) -> None:
    body = MINIMAL + '\n[detection]\nbackend = "none"\ntypo_knob = "oops"\n'
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, body))
    assert "typo_knob" in str(exc.value)


def test_missing_required_site_id(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, '[storage]\ndb = "/tmp/x.db"\n'))
    assert "site_id" in str(exc.value)


def test_missing_required_db(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, 'site_id = "s"\n'))
    assert "db" in str(exc.value)


def test_wrong_type_rejected(tmp_path: Path) -> None:
    body = (
        'site_id = "s"\n[storage]\ndb = "/tmp/x.db"\n[scan]\nwindow = "soon"\n'
    )
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, body))


@pytest.mark.parametrize(
    "section",
    [
        "[scan]\nwindow = 0\n",
        "[scan]\npause = -1\n",
        "[resolver]\nmin_score = -0.1\n",
        # At or below the company-only weight: vendor-collapse.
        "[resolver]\nmin_score = 0.3\n",
        "[resolver]\nmin_score = 0.15\n",
        "[resolver]\nmin_score = 0.0\n",
        # Above the max achievable fusion score: unreachable, not valid.
        "[resolver]\nmin_score = 7.0\n",
        "[resolver]\nmin_score = 2.01\n",
        "[resolver]\nrecent_window = 0\n",
        "[presence]\nappear_windows = 0\n",
        "[presence]\ndisappear_windows = 0\n",
        "[presence]\ncooldown_windows = -1\n",
        "[presence]\nprune_after_windows = 0\n",
        # A zero-or-positive RSSI is a sign typo that no real signal can
        # reach — it would silence the sentinel (rssi >= 0 never holds),
        # so both 0 and a positive value are rejected at load time.
        "[presence]\nrssi_threshold = 80\n",
        "[presence]\nrssi_threshold = 0\n",
        "[summary]\nhour_utc = 24\n",
        "[summary]\nhour_utc = -1\n",
    ],
)
def test_out_of_range_values_rejected(tmp_path: Path, section: str) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, MINIMAL + "\n" + section))


def test_min_score_floor_tracks_company_only_weight(
    tmp_path: Path,
) -> None:
    """ADR-0005: min_score must exceed COMPANY_ONLY_WEIGHT (0.3)."""
    from blesentry.resolver import COMPANY_ONLY_WEIGHT

    at_floor = MINIMAL + f"\n[resolver]\nmin_score = {COMPANY_ONLY_WEIGHT}\n"
    with pytest.raises(ConfigError, match="min_score"):
        load_config(_write(tmp_path, at_floor))
    above = MINIMAL + (
        f"\n[resolver]\nmin_score = {COMPANY_ONLY_WEIGHT + 0.01}\n"
    )
    cfg = load_config(_write(tmp_path, above))
    assert cfg.resolver.min_score == pytest.approx(COMPANY_ONLY_WEIGHT + 0.01)


def test_min_score_upper_bound_accepted(tmp_path: Path) -> None:
    cfg = load_config(
        _write(tmp_path, MINIMAL + "\n[resolver]\nmin_score = 2.0\n")
    )
    assert cfg.resolver.min_score == 2.0


def test_validation_error_does_not_leak_input_value(tmp_path: Path) -> None:
    # The seam carries P2's notifier secret via env; a validation
    # failure must not preserve the offending value in the raised
    # error or its cause chain (which a traceback would print).
    secret = "s3cr3t-token-value"
    body = MINIMAL + f'\n[notifier]\nbackend = "{secret}"\n'
    with pytest.raises(ConfigError) as exc:
        load_config(_write(tmp_path, body))
    assert secret not in str(exc.value)
    assert exc.value.__cause__ is None


def test_invalid_notifier_backend_rejected(tmp_path: Path) -> None:
    body = MINIMAL + '\n[notifier]\nbackend = "telegram"\n'
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, body))


# ---------------------------------------------------------------------------
# build_scanner: config -> concrete Scanner (ADR-0002 lazy registry)
# ---------------------------------------------------------------------------


async def test_build_mock_scanner_empty() -> None:
    scanner = build_scanner(MockScannerConfig(backend="mock"))
    assert await scanner.scan(duration=0.0) == []


async def test_build_mock_scanner_from_corpus(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus.json"
    corpus.write_text(
        '[{"address": "AA:BB:CC:DD:EE:FF", "rssi": -50, '
        '"timestamp": 1755400000.0, "adapter_id": "mock"}]',
        encoding="utf-8",
    )
    scanner = build_scanner(MockScannerConfig(backend="mock", corpus=corpus))
    ads = await scanner.scan(duration=0.0)
    assert len(ads) == 1
    assert ads[0].rssi == -50


def test_build_bleak_scanner_carries_adapter_id() -> None:
    from blesentry.scanner.bleak import BleakScanner

    scanner = build_scanner(
        BleakScannerConfig(adapter_id="bluez-linux", adapter="hci0")
    )
    assert isinstance(scanner, BleakScanner)
    assert scanner.adapter_id == "bluez-linux"


def test_build_bleak_scanner_rejects_bad_or_pattern() -> None:
    cfg = BleakScannerConfig(or_patterns=["not-a-pattern"])
    with pytest.raises(ValueError):
        build_scanner(cfg)


# ---------------------------------------------------------------------------
# build_presence: config -> PresenceTracker (P2-2 threshold wiring)
# ---------------------------------------------------------------------------


def test_build_presence_returns_tracker() -> None:
    from blesentry.presence import PresenceTracker

    assert isinstance(build_presence(PresenceConfig()), PresenceTracker)


def test_build_presence_wires_rssi_gate_and_appear() -> None:
    # A behavioural check that the two headline knobs actually reach the
    # tracker: an at-appear_windows=1 device below the RSSI gate stays
    # silent, the same device above the gate reaches PRESENT immediately.
    from blesentry.presence import PresenceState

    tracker = build_presence(
        PresenceConfig(appear_windows=1, rssi_threshold=-70)
    )
    assert tracker.update({1: -75}) == []  # below the -70 gate → a miss
    [transition] = tracker.update({1: -65})  # above the gate → PRESENT
    assert transition.device_id == 1
    assert transition.state is PresenceState.PRESENT


def test_build_presence_wires_disappear_and_cooldown() -> None:
    # disappear_windows=1 makes a single miss drop the device to ABSENT;
    # cooldown/prune are pass-through and covered by the tracker's own
    # tests — here we just prove the disappear count reaches the machine.
    from blesentry.presence import PresenceState

    tracker = build_presence(
        PresenceConfig(appear_windows=1, disappear_windows=1)
    )
    tracker.update({1: -50})  # → PRESENT
    [transition] = tracker.update({})  # one miss → ABSENT
    assert transition.state is PresenceState.ABSENT


# ---------------------------------------------------------------------------
# build_detector: config -> concrete Detector (ADR-0006 lazy registry)
# ---------------------------------------------------------------------------


def test_build_detector_none_is_null() -> None:
    from blesentry.detection.null import NullDetector

    assert isinstance(build_detector(NoneDetectionConfig()), NullDetector)


def test_build_detector_mock_is_mock() -> None:
    from blesentry.detection.mock import MockDetector

    assert isinstance(
        build_detector(MockDetectionConfig(backend="mock")),
        MockDetector,
    )


def test_build_detector_none_does_not_import_mock() -> None:
    """The none path must not load mock (ADR-0006 lazy import)."""
    import subprocess
    import sys

    script = (
        "from blesentry.config import NoneDetectionConfig, "
        "build_detector\n"
        "build_detector(NoneDetectionConfig())\n"
        "import sys\n"
        "raise SystemExit(0 if 'blesentry.detection.mock' "
        "not in sys.modules else 1)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_build_detector_none_does_not_import_approach() -> None:
    """The none path must not load the A3 backend."""
    import subprocess
    import sys

    script = (
        "from blesentry.config import NoneDetectionConfig, "
        "build_detector\n"
        "build_detector(NoneDetectionConfig())\n"
        "import sys\n"
        "mods = sys.modules\n"
        "raise SystemExit(0 if "
        "'blesentry.detection.approach_detector' not in mods "
        "else 1)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# The committed example config (DoD: example config committed)
# ---------------------------------------------------------------------------


def test_example_config_exists() -> None:
    assert EXAMPLE_CONFIG.is_file(), "config.example.toml must be committed"


def test_example_config_loads_clean() -> None:
    cfg = load_config(EXAMPLE_CONFIG)
    assert cfg.site_id
    assert cfg.storage.db
    assert cfg.scanner.backend in {"bleak", "mock"}
    assert cfg.detection.backend in {"none", "mock", "approach", "inside"}
