"""Check report denominators, sourcing ceilings, and unknown exposure."""

from dataclasses import replace

import pytest

from src.bom.flatten import Component
from src.brief import build_brief_data
from src.classify.classifier import Classification
from src.duty.scenarios import calculate_duty_scenarios
from src.hts.index import HtsIndex, HtsRecord


@pytest.fixture
def inputs():
    components = [
        Component("A", "Part A", 20, 0.5, 1, country_of_origin="CN"),
        Component("B", "Part B", 2, 10, 1, country_of_origin="CN"),
    ]
    classifications = [
        Classification(part.reference, part.name, "classified", "clean", "1234.56.78", "Part")
        for part in components
    ]
    index = HtsIndex([HtsRecord("1234.56.78", "Part", general="10%", special="5% (JP) Free (S)")])
    return components, classifications, index


def facts(inputs, countries=("JP",)):
    components, classifications, index = inputs
    scenarios = calculate_duty_scenarios(
        components, classifications, index, {"1234.56.78": list(countries)},
    )
    return build_brief_data(components, classifications, scenarios, index)


def test_summary_ranking_and_nonzero_alternative_rate(inputs):
    result = facts(inputs)
    summary = result["summary"]
    assert summary["known_current_duty_per_finished_product_usd"] == 3
    assert summary["known_exposure_pct_total_bom_cost"] == 10
    assert summary["potential_savings_per_finished_product_usd"] == 1.5
    assert summary["potentially_addressable_pct_known_exposure"] == 50
    assert summary["covered_bom_cost_pct"] == 100
    assert result["annual_tariff_exposure_usd"] is None
    assert [row["reference"] for row in result["product_exposure"]] == ["B", "A"]
    assert result["product_exposure"][0]["exposure_pct_total_bom_cost"] == pytest.approx(6.6667)
    opportunity = result["sourcing_opportunities"][1]
    assert opportunity["current_purchase_price_per_piece_usd"] == 0.5
    # $0.523810 * 1.05 approximately equals $0.50 * 1.10, per piece, not per BOM.
    assert opportunity["break_even_alternative_purchase_price_per_piece_usd"] == 0.52381
    assert opportunity["duty_savings_per_finished_product_usd"] == 0.5


def test_ties_are_counted_once_and_beat_worse_origins(inputs):
    result = facts(inputs, ("JP", "CA", "MX"))
    assert result["summary"]["potential_savings_per_finished_product_usd"] == 3
    assert len(result["sourcing_opportunities"]) == 2
    assert result["sourcing_opportunities"][0]["alternative_origin"] == "CA"
    assert result["sourcing_opportunities"][0]["equally_saving_origins"] == ["CA", "MX"]


def test_partial_coverage_keeps_full_bom_denominator(inputs):
    inputs[1][1] = replace(inputs[1][1], status="needs_review", reason="uncertain")
    result = facts(inputs)
    summary = result["summary"]
    assert summary["exposure_is_partial"] is True
    assert summary["covered_parts"] == 1
    assert summary["covered_bom_cost_pct"] == pytest.approx(33.3333)
    assert summary["known_exposure_pct_total_bom_cost"] == pytest.approx(3.3333)
    assert result["unresolved_parts"][0]["reference"] == "B"


def test_all_unresolved_is_unknown_not_zero(inputs):
    inputs[2].get("1234.56.78").general = "2 cents/kg"
    result = facts(inputs)
    assert result["summary"]["known_current_duty_per_finished_product_usd"] is None
    assert result["summary"]["potential_savings_per_finished_product_usd"] is None
    assert result["summary"]["potentially_addressable_pct_known_exposure"] is None
    assert len(result["unresolved_parts"]) == 2


def test_zero_duty_and_zero_cost_have_no_invalid_percentages(inputs):
    inputs[2].get("1234.56.78").general = "Free"
    result = facts(inputs)
    assert result["summary"]["known_current_duty_per_finished_product_usd"] == 0
    assert result["summary"]["potentially_addressable_pct_known_exposure"] is None
    assert result["sourcing_opportunities"] == []
    for part in inputs[0]:
        part.unit_price_usd = 0
    result = facts(inputs)
    assert result["summary"]["known_exposure_pct_total_bom_cost"] is None


def test_unsupported_and_missing_alternatives_remain_visible(inputs):
    inputs[2].get("1234.56.78").special = "2 cents/kg (JP)"
    result = facts(inputs)
    assert len(result["unsupported_alternatives"]) == 2
    assert result["sourcing_opportunities"] == []
    result = facts(inputs, ())
    assert result["summary"]["known_current_duty_per_finished_product_usd"] == 3
    assert result["summary"]["potential_savings_per_finished_product_usd"] == 0
    assert result["sourcing_opportunities"] == []
