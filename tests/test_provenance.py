"""Provenance contract. If these pass, "every number is sourced" is enforced, not claimed."""

from __future__ import annotations

import pytest

from analyzer.core import provenance as pv


class TestValidation:
    def test_measured_requires_a_source(self):
        with pytest.raises(ValueError):
            pv.Value(42, "measured")

    def test_estimated_requires_a_source(self):
        with pytest.raises(ValueError):
            pv.Value(42, "estimated")

    def test_derived_needs_no_source(self):
        assert pv.derived(42).value == 42

    def test_unavailable_must_carry_no_value(self):
        with pytest.raises(ValueError):
            pv.Value(42, "unavailable")

    def test_unknown_confidence_rejected(self):
        with pytest.raises(ValueError):
            pv.Value(42, "probably")


class TestSerialisation:
    def test_omits_empty_fields(self):
        assert pv.derived(42).to_dict().keys() >= {"value", "confidence"}
        assert "note" not in pv.derived(42).to_dict()

    def test_carries_source_and_timestamp(self):
        d = pv.measured(42, "https://example.gov/data").to_dict()
        assert d["source_url"] == "https://example.gov/data"
        assert d["retrieved_at"]

    def test_precision_survives(self):
        d = pv.estimated(
            True, "https://fcc.gov", precision="census_block"
        ).to_dict()
        assert d["precision"] == "census_block"


class TestUnwrap:
    def test_available_value_unwraps(self):
        assert pv.derived(42).unwrap() == 42

    def test_unavailable_returns_the_default(self):
        assert pv.unavailable("source down").unwrap(default="unknown") == "unknown"

    def test_is_available_flag(self):
        assert pv.derived(1).is_available is True
        assert pv.unavailable("timeout").is_available is False


class TestImmutability:
    def test_values_cannot_be_mutated(self):
        v = pv.derived(42)
        with pytest.raises(Exception):
            v.value = 99  # type: ignore[misc]


class TestDegradation:
    def test_records_station_and_reason(self):
        d = pv.Degradation("fema", "503 from the NFHL service").to_dict()
        assert d["station"] == "fema"
        assert "503" in d["reason"]
        assert d["at"]
