"""Hand-calculated HTS country comparisons; no model calls."""

import csv
from dataclasses import replace

import pytest

from src.classify.classifier import Selection, resolve
from src.bom.flatten import Bom
from src.config import HTS_JSON, ROOT
from src.duty.rates import duty_rate, qualifies_for_special_program
from src.duty.scenarios import calculate_duty_scenarios, write_scenarios
from src.hts.index import HtsIndex, HtsRecord


@pytest.fixture
def demo():
    components = Bom.load(ROOT / "examples/two_parts.csv").components()
    index = HtsIndex.load(HTS_JSON)
    classifications = []
    for part, code in zip(components, ["7318.16.00.60", "7318.21.00.30"]):
        choice = next(i for i, r in enumerate(index.candidates) if r.htsno == code)
        selection = Selection(choice=choice, evidence=index.get(code).description)
        classifications.append(resolve(part, selection, index.candidates))
    return components, classifications, index, {
        classification.code: ["CA"] for classification in classifications
    }


def test_two_parts_four_rows_and_hand_calculated_totals(demo, tmp_path):
    result = calculate_duty_scenarios(*demo)
    assert result["rows"] == [
        {"reference": "DEMO-NUT", "country": "CA", "duty_eur": 0.0, "savings_eur": 0.0},
        {"reference": "DEMO-NUT", "country": "CN", "duty_eur": 0.0, "savings_eur": 0.0},
        {"reference": "DEMO-WASHER", "country": "CA", "duty_eur": 0.0, "savings_eur": 0.58},
        {"reference": "DEMO-WASHER", "country": "CN", "duty_eur": 0.58, "savings_eur": 0.0},
    ]
    assert result["current_duty_eur"] == 0.58
    assert result["best_savings_eur"] == 0.58
    assert result["duty_after_best_savings_eur"] == 0
    assert result["calculated_parts"] == result["total_parts"] == 2
    assert result["unresolved"] == []
    path = tmp_path / "scenarios.csv"
    write_scenarios(result["rows"], path)
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == ["reference", "country", "duty_eur", "savings_eur"]
    assert rows[-1]["duty_eur"] == "0.58"


@pytest.mark.parametrize("country,rate", [
    ("CN", 0.062), ("TW", 0.062), ("DE", 0.062),
    ("CA", 0), ("MX", 0), ("CR", 0), ("DO", 0), ("KR", 0), ("JP", 0.031),
])
def test_inherited_special_rates_and_country_program_mapping(demo, country, rate):
    code = demo[2].get("7318.14.10.30")
    assert code.rate_source == "7318.14.10"
    assert duty_rate(code, country) == pytest.approx(rate)


def test_special_program_must_be_listed_on_code():
    code = HtsRecord("1234.56.78", "Example", general="10%", special="Free (S+)")
    assert not qualifies_for_special_program(code, "CA")
    assert duty_rate(code, "CA") == 0.1
    code.special = "Free (S)"
    assert qualifies_for_special_program(code, " ca ")
    assert duty_rate(code, "CA") == 0


@pytest.mark.parametrize("general,special,country", [
    ("2 cents/kg", "", "CN"),
    ("", "", "CN"),
    ("10%", "2 cents/kg (S)", "CA"),
    ("10%", "Free (S) 2 cents/kg (JP)", "JP"),
])
def test_unsupported_rates_are_not_silently_free_or_general(general, special, country):
    code = HtsRecord("1234.56.78", "Example", general=general, special=special)
    assert duty_rate(code, country) is None


def test_general_free_with_no_special_is_zero(demo):
    assert duty_rate(demo[2].get("7318.16.00.60"), "CA") == 0


def test_multiple_alternatives_do_not_double_count_savings(demo):
    components, classifications, index, _ = demo
    countries = {row.code: ["CA", "MX", "JP"] for row in classifications}
    result = calculate_duty_scenarios(components, classifications, index, countries)
    assert result["best_savings_eur"] == 0.58
    assert len(result["best_alternatives"]) == 1
    japan = next(r for r in result["rows"] if r["reference"] == "DEMO-WASHER" and r["country"] == "JP")
    assert japan["duty_eur"] == 0.29


def test_baselines_follow_each_parts_origin_and_keep_negative_savings(demo):
    components, classifications, index, _ = demo
    components[1].country_of_origin = "JP"
    countries = {row.code: ["CN", "CA"] for row in classifications}
    result = calculate_duty_scenarios(components, classifications, index, countries)
    assert result["current_duty_eur"] == 0.29
    china = next(r for r in result["rows"] if r["reference"] == "DEMO-WASHER" and r["country"] == "CN")
    assert china["savings_eur"] == -0.29
    assert result["best_savings_eur"] == 0.29


def test_unclassified_and_unsupported_baseline_are_excluded(demo):
    components, classifications, index, countries = demo
    classifications[0] = replace(classifications[0], status="needs_review", reason="test_review")
    index.get(classifications[1].code).general = "2 cents/kg"
    result = calculate_duty_scenarios(components, classifications, index, countries)
    assert result["rows"] == []
    assert result["calculated_parts"] == 0
    assert result["unresolved"] == [
        {"reference": "DEMO-NUT", "reason": "test_review"},
        {"reference": "DEMO-WASHER", "reason": "unsupported_current_origin_rate"},
    ]


def test_unsupported_alternative_keeps_supported_baseline(demo):
    components, classifications, index, countries = demo
    index.get(classifications[1].code).special = "2 cents/kg (S)"
    result = calculate_duty_scenarios(components, classifications, index, countries)
    assert result["current_duty_eur"] == 0.58
    assert result["best_savings_eur"] == 0
    assert result["unresolved"] == [{
        "reference": "DEMO-WASHER", "country": "CA", "reason": "unsupported_alternative_rate",
    }]


def test_country_normalization_deduplication_and_current_only(demo):
    components, classifications, index, _ = demo
    countries = {row.code: [" ca ", "CA", "CN"] for row in classifications}
    result = calculate_duty_scenarios(components, classifications, index, countries)
    assert len(result["rows"]) == 4
    result = calculate_duty_scenarios(components, classifications, index, {})
    assert len(result["rows"]) == 2
    assert result["best_savings_eur"] == 0


def test_missing_origin_is_not_treated_as_general(demo):
    demo[0][1].country_of_origin = ""
    result = calculate_duty_scenarios(*demo)
    assert result["calculated_parts"] == 1
    assert result["unresolved"][0]["reason"] == "missing_or_invalid_origin"


def test_invalid_scenario_country_is_rejected(demo):
    components, classifications, index, _ = demo
    with pytest.raises(ValueError, match="two-letter"):
        calculate_duty_scenarios(
            components, classifications, index, {classifications[0].code: ["Canada"]},
        )
