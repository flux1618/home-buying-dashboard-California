"""California Proposition 13 tax math.

The decisive California buyer-risk is not a small rate variation. A listing reports
the seller's frozen Prop 13 basis; a sale resets that basis to the buyer's price.
These tests keep that one-directional reset visible in the calculations and output.
"""

from __future__ import annotations

import pytest

from analyzer.core import rate_area, tax


class TestRateComposition:
    def test_city_of_la_total_matches_published_rate(self):
        area = rate_area.get_schedule("00004")
        assert area.total_rate_pct() == pytest.approx(1.187380)
        assert area.total_rate() == pytest.approx(0.01187380)

    def test_general_levy_plus_voter_debt_equals_total(self):
        area = rate_area.get_schedule("00004")
        assert area.general_levy_pct() == pytest.approx(1.000000)
        assert area.voter_debt_pct() == pytest.approx(0.187380)
        assert area.general_levy_pct() + area.voter_debt_pct() == pytest.approx(
            area.total_rate_pct()
        )

    def test_school_debt_is_separated_from_other_voter_debt(self):
        """LAUSD and LACCD bonds explain most, but not all, above-cap debt."""
        area = rate_area.get_schedule("00004")
        assert area.school_debt_pct() == pytest.approx(0.168148)
        assert area.school_debt_pct() / area.voter_debt_pct() == pytest.approx(0.897364)

    def test_breakdown_components_sum_to_published_total(self):
        area = rate_area.get_schedule("00004")
        breakdown = area.breakdown()
        assert sum(component["rate_pct"] for component in breakdown) == pytest.approx(
            area.total_rate_pct()
        )
        assert {component["kind"] for component in breakdown} == {
            rate_area.GENERAL_LEVY,
            rate_area.VOTER_DEBT,
        }

    @pytest.mark.parametrize("tra", ("00004", "00005", "00006", "00008", "00067"))
    def test_city_of_la_bundle_tras_are_identical(self, tra):
        assert rate_area.get_schedule(tra).total_rate_pct() == pytest.approx(1.187380)

    @pytest.mark.parametrize(
        ("tra", "expected_rate"),
        (("00001", 1.247089), ("00002", 1.247089), ("00003", 1.181000)),
    )
    def test_published_early_tra_totals_resolve(self, tra, expected_rate):
        assert rate_area.get_schedule(tra).total_rate_pct() == pytest.approx(expected_rate)


class TestRateResolution:
    @pytest.mark.parametrize(
        ("raw", "normalized"),
        (("4", "00004"), (4, "00004"), (" 00004 ", "00004"), (None, None), ("", None)),
    )
    def test_normalize_tra_canonicalizes_human_and_parcel_forms(self, raw, normalized):
        assert rate_area.normalize_tra(raw) == normalized

    def test_unknown_or_missing_tra_falls_back_without_raising(self):
        for tra in ("not-a-tra", "99999", None):
            area = rate_area.get_schedule(tra)
            assert area is rate_area.COUNTYWIDE_FALLBACK
            assert rate_area.is_measured(area) is False

    def test_fallback_is_a_labeled_countywide_estimate(self):
        area = rate_area.COUNTYWIDE_FALLBACK
        assert area.total_rate_pct() == pytest.approx(1.20)
        assert area.general_levy_pct() == pytest.approx(rate_area.GENERAL_LEVY_RATE * 100)
        assert "ESTIMATE" in area.note


class TestAssessedValue:
    def test_sale_resets_base_year_value_to_purchase_price(self):
        """A buyer does not inherit the seller's lower, long-held assessment."""
        assert tax.base_year_value(650_000) == pytest.approx(650_000)
        assert tax.assessed_value(650_000) == pytest.approx(650_000)

    def test_homeowners_exemption_reduces_assessed_value_by_seven_thousand(self):
        assert tax.assessed_value(650_000, tax.HOMEOWNERS_EXEMPTION) == pytest.approx(643_000)

    def test_exemption_cannot_make_assessed_value_negative(self):
        assert tax.assessed_value(5_000, tax.HOMEOWNERS_EXEMPTION) == 0.0

    @pytest.mark.parametrize("function,args", ((tax.base_year_value, (-1,)), (tax.assessed_value, (-1,))))
    def test_negative_price_is_rejected(self, function, args):
        with pytest.raises(ValueError):
            function(*args)


class TestFactoredBaseYearValue:
    def test_two_percent_factor_compounds_annually(self):
        assert tax.factored_base_year_value(100_000, 3) == pytest.approx(100_000 * 1.02**3)

    def test_annual_factor_is_a_cap_not_a_fixed_rate(self):
        """A lower CPI adjustment stays lower, while a larger supplied value is capped."""
        assert tax.factored_base_year_value(100_000, 2, 0.01) == pytest.approx(100_000 * 1.01**2)
        assert tax.factored_base_year_value(100_000, 2, 0.05) == pytest.approx(100_000 * 1.02**2)

    def test_negative_base_or_holding_period_is_rejected(self):
        with pytest.raises(ValueError):
            tax.factored_base_year_value(-1, 1)
        with pytest.raises(ValueError):
            tax.factored_base_year_value(1, -1)


class TestProp8:
    def test_declining_market_uses_lower_market_value(self):
        """Proposition 8 prevents an owner from being taxed above a depressed market value."""
        factored = tax.factored_base_year_value(500_000, 3)
        assert tax.taxable_value(factored, 450_000) == pytest.approx(450_000)

    def test_taxable_value_uses_factored_basis_when_market_is_higher_or_unknown(self):
        factored = 510_000.0
        assert tax.taxable_value(factored, 650_000) == pytest.approx(factored)
        assert tax.taxable_value(factored) == pytest.approx(factored)

    def test_negative_factored_value_is_rejected(self):
        with pytest.raises(ValueError):
            tax.taxable_value(-1)


class TestScenarios:
    def test_exemption_scenario_always_costs_less(self):
        owner, no_exemption, area = tax.both_scenarios(600_000, "00004")
        assert owner.annual_tax < no_exemption.annual_tax
        assert no_exemption.annual_tax - owner.annual_tax == pytest.approx(
            tax.HOMEOWNERS_EXEMPTION * area.total_rate()
        )

    def test_monthly_tax_is_annual_tax_over_twelve(self):
        owner, no_exemption, _ = tax.both_scenarios(600_000, "00004")
        assert owner.monthly_tax == pytest.approx(owner.annual_tax / 12.0)
        assert no_exemption.monthly_tax == pytest.approx(no_exemption.annual_tax / 12.0)

    def test_annual_tax_rejects_negative_rate(self):
        with pytest.raises(ValueError):
            tax.annual_tax(600_000, -0.01)


class TestSupplemental:
    def test_supplemental_tax_is_assessment_delta_times_rate_and_proration(self):
        amount = tax.supplemental_tax(593_000, 400_000, 0.0118738, 1)
        assert amount == pytest.approx((593_000 - 400_000) * 0.0118738 * tax.SUPPLEMENTAL_PRORATION[1])

    def test_january_to_may_closing_warns_of_two_bills(self):
        """The fiscal year has crossed January, so the county issues two supplemental bills."""
        block = tax.supplemental_block(600_000, 400_000, "00004", closing_month=1)
        assert block["available"] is True
        assert block["proration_factor"] == pytest.approx(tax.SUPPLEMENTAL_PRORATION[1])
        assert block["two_bills_expected"] is True
        assert "TWO supplemental bills" in block["note"]

    def test_july_closing_uses_full_remaining_fiscal_year_and_no_two_bill_warning(self):
        block = tax.supplemental_block(600_000, 400_000, "00004", closing_month=7)
        assert block["proration_factor"] == pytest.approx(tax.SUPPLEMENTAL_PRORATION[7])
        assert block["two_bills_expected"] is False

    def test_missing_prior_assessment_leaves_supplemental_unavailable(self):
        block = tax.supplemental_block(600_000, None, "00004")
        assert block["available"] is False
        assert block["source_url"].startswith("https://")

    def test_invalid_month_and_negative_rate_are_rejected(self):
        with pytest.raises(ValueError):
            tax.supplemental_tax(600_000, 400_000, 0.01, 13)
        with pytest.raises(ValueError):
            tax.supplemental_tax(600_000, 400_000, -0.01, 7)


class TestListingBasis:
    def test_long_held_seller_basis_makes_listing_tax_too_low_for_buyer(self):
        """Unlike SC's assessment-ratio issue, the normal Prop 13 reset moves upward."""
        comparison = tax.listing_basis_comparison(800_000, 300_000, "00004")
        assert comparison["available"] is True
        assert comparison["direction"] == "up"
        assert comparison["buyer_annual_tax"] > comparison["seller_annual_tax"]
        assert comparison["delta_annual"] > 0

    def test_basis_reset_note_labels_large_upward_reset(self):
        note = tax.basis_reset_note(300_000, 800_000)
        assert note is not None
        assert note.value == "up_sharply"
        assert note.confidence == "estimated"
        assert note.source_url and note.source_url.startswith("https://")
        assert "understates" in (note.note or "")

    def test_unknown_seller_assessment_does_not_make_a_numeric_claim(self):
        assert tax.basis_reset_note(None, 800_000) is None
        comparison = tax.listing_basis_comparison(800_000, None, "00004")
        assert comparison["available"] is False
        assert "understates" in comparison["note"]


class TestProvenance:
    def test_measured_tra_rate_and_derived_tax_carry_distinct_confidence(self):
        block = tax.tax_block(600_000, "00004", current_assessed=400_000)
        rate_value = block["rate"]["total_rate_value"]
        monthly_value = block["scenario_owner_occupied"]["monthly_tax_value"]
        assert rate_value["confidence"] == "measured"
        assert monthly_value["confidence"] == "estimated"
        assert rate_value["source_url"].startswith("https://")
        assert monthly_value["source_url"].startswith("https://")
        assert block["fiscal_year"] == rate_area.FISCAL_YEAR

    def test_unresolved_tra_marks_rate_as_estimated(self):
        block = tax.tax_block(600_000, "unknown")
        assert block["rate_resolved"] is False
        assert block["rate"]["total_rate_value"]["confidence"] == "estimated"


class TestTaxBlock:
    def test_block_returns_scenarios_rate_breakdown_and_supplemental_estimate(self):
        block = tax.tax_block(600_000, "00004", current_assessed=400_000, closing_month=1)
        assert "scenario_owner_occupied" in block
        assert "scenario_no_exemption" in block
        assert block["rate"]["breakdown"]
        assert block["supplemental"]["available"] is True
        assert block["delta_annual"] == pytest.approx(
            block["scenario_no_exemption"]["annual_tax"]
            - block["scenario_owner_occupied"]["annual_tax"],
            abs=0.01,
        )
