# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Advertisement + Fingerprint models (P1-1).

TDD against the P0-3 captured corpus: every fixture record must
deserialize into an ``Advertisement``, and derived ``Fingerprint``
instances must be stable, hashable identity keys.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from blesentry.scanner import Advertisement, Fingerprint

FIXTURES = Path(__file__).parent / "fixtures"
CORPUS = FIXTURES / "corpus-macos-corebluetooth.json"


def _corpus() -> list[dict[str, object]]:
    return json.loads(CORPUS.read_text(encoding="utf-8"))


def _sample_ad(**overrides: object) -> Advertisement:
    return Advertisement.model_validate({**_corpus()[0], **overrides})


@pytest.mark.parametrize(
    "record",
    _corpus(),
    ids=lambda r: f"mac={r['mac'][:8]}",
)
def test_corpus_record_parses_as_advertisement(
    record: dict[str, object],
) -> None:
    """Every fixture-corpus record deserializes into an Advertisement."""
    adv = Advertisement.model_validate(record)
    assert adv.mac == record["mac"]
    assert adv.rssi == record["rssi"]
    assert adv.timestamp == record["timestamp"]
    assert adv.adapter_id == record["adapter_id"]


def test_absent_local_name_defaults_to_none() -> None:
    """An advertisement without an advertised name parses with null name."""
    adv = _sample_ad(local_name=None)
    assert adv.local_name is None


def test_empty_local_name_is_accepted() -> None:
    """An empty advertised-name string is a legal (if unhelpful) value."""
    adv = _sample_ad(local_name="")
    assert adv.local_name == ""


def test_no_manufacturer_data_defaults_to_empty() -> None:
    """An advertisement without manufacturer data or service UUIDs parses."""
    adv = _sample_ad(manufacturer_data={}, service_uuids=[])
    assert adv.manufacturer_data == {}
    assert adv.service_uuids == ()
    assert adv.service_data == {}
    assert adv.tx_power is None


def test_missing_optional_fields_default() -> None:
    """Only identity + observation fields are required; the rest default."""
    adv = Advertisement(
        mac="00:11:22:33:44:55",
        rssi=-70,
        timestamp=1.0,
        adapter_id="bluez-linux",
    )
    assert adv.local_name is None
    assert adv.service_uuids == ()
    assert adv.manufacturer_data == {}
    assert adv.service_data == {}
    assert adv.tx_power is None


def test_empty_mac_rejected() -> None:
    """A blank source address is never a legal advertisement."""
    with pytest.raises(ValidationError):
        Advertisement(
            mac="",
            rssi=-70,
            timestamp=1.0,
            adapter_id="bluez-linux",
        )


def test_extra_fields_rejected() -> None:
    """Corpus schema drift fails loudly instead of being silently ignored."""
    record: dict[str, object] = {
        **_corpus()[0],
        "surprise_field": "nope",
    }
    with pytest.raises(ValidationError):
        Advertisement.model_validate(record)


def test_advertisement_is_immutable() -> None:
    """Frozen is real: containers are immutable, assignment is rejected."""
    adv = _sample_ad()
    assert isinstance(adv.service_uuids, tuple)
    assert isinstance(adv.manufacturer_data, MappingProxyType)
    assert isinstance(adv.service_data, MappingProxyType)
    attr: str = "rssi"
    with pytest.raises(ValidationError):
        setattr(adv, attr, -40)


def test_fingerprint_derived_from_advertisement() -> None:
    """Fingerprint carries the stable identity components of a scan."""
    adv = _sample_ad()
    fp = Fingerprint.from_advertisement(adv)
    assert fp.mac == adv.mac
    assert set(fp.service_uuids) == set(adv.service_uuids)
    assert set(fp.manufacturer_data) == set(adv.manufacturer_data.items())
    assert fp.local_name == adv.local_name


def test_fingerprint_ignores_observation_artifacts() -> None:
    """RSSI, timestamp and adapter must not change the identity key."""
    base = _corpus()[0]
    fp1 = Fingerprint.from_advertisement(_sample_ad(rssi=-62, timestamp=1.0))
    fp2 = Fingerprint.from_advertisement(
        _sample_ad(rssi=-80, timestamp=999.0, adapter_id="other")
    )
    assert fp1 == fp2
    assert hash(fp1) == hash(fp2)
    assert base["rssi"] != 999.0


def test_fingerprint_is_hashable() -> None:
    """Fingerprints are usable as dict keys and set members."""
    fp = Fingerprint.from_advertisement(_sample_ad())
    assert {fp} == {Fingerprint.from_advertisement(_sample_ad())}


def test_fingerprint_distinguishes_different_devices() -> None:
    """Different identity components yield different fingerprints."""
    fp_a = Fingerprint.from_advertisement(_sample_ad(mac="00:11:22:33:44:55"))
    fp_b = Fingerprint.from_advertisement(_sample_ad(mac="66:77:88:99:AA:BB"))
    assert fp_a != fp_b


def test_fingerprint_derived_for_entire_corpus() -> None:
    """Deriving fingerprints never fails on the real capture corpus."""
    records = _corpus()
    fingerprints = {
        Fingerprint.from_advertisement(Advertisement.model_validate(record))
        for record in records
    }
    assert len(fingerprints) == len(records)


# ---------------------------------------------------------------------------
# Serialization (#74): frozen containers must dump as plain list/dict
# with zero pydantic warnings
# ---------------------------------------------------------------------------


def test_advertisement_json_dump_plain_and_warning_free() -> None:
    ad = _sample_ad(
        service_uuids=["180d"],
        manufacturer_data={"76": "010203"},
        service_data={"180f": "0a"},
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        dumped = ad.model_dump(mode="json")
    assert dumped["service_uuids"] == ["180d"]
    assert type(dumped["service_uuids"]) is list
    assert dumped["manufacturer_data"] == {"76": "010203"}
    assert type(dumped["manufacturer_data"]) is dict
    assert type(dumped["service_data"]) is dict


def test_advertisement_python_dump_warning_free() -> None:
    ad = _sample_ad()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        dumped = ad.model_dump(mode="python")
    assert type(dumped["manufacturer_data"]) is dict


def test_advertisement_model_dump_json_round_trips() -> None:
    ad = _sample_ad(
        service_uuids=["180d", "180f"],
        manufacturer_data={"76": "0102"},
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        raw = ad.model_dump_json()
    revived = Advertisement.model_validate(json.loads(raw))
    assert revived == ad


# ---------------------------------------------------------------------------
# Input hardening (#85): radio-controlled fields are bounded; the model
# must not assume the radio is honest
# ---------------------------------------------------------------------------


def test_local_name_over_gap_maximum_rejected() -> None:
    with pytest.raises(ValidationError):
        _sample_ad(local_name="A" * 249)


def test_local_name_at_gap_maximum_accepted() -> None:
    ad = _sample_ad(local_name="A" * 248)
    assert ad.local_name is not None and len(ad.local_name) == 248


def test_lone_surrogate_local_name_rejected() -> None:
    with pytest.raises(ValidationError):
        _sample_ad(local_name="evil\ud800name")


def test_service_uuid_cardinality_bounded() -> None:
    with pytest.raises(ValidationError):
        _sample_ad(service_uuids=[f"{i:04x}" for i in range(65)])


def test_manufacturer_data_cardinality_bounded() -> None:
    with pytest.raises(ValidationError):
        _sample_ad(manufacturer_data={str(i): "00" for i in range(65)})


def test_service_data_cardinality_bounded() -> None:
    with pytest.raises(ValidationError):
        _sample_ad(service_data={f"{i:04x}": "00" for i in range(65)})
