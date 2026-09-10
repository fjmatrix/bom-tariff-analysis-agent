"""Hand-calculated HTS country comparisons; no model calls."""

import json
from dataclasses import replace

import pytest

from src.classify.classifier import Classifier, Selection
from src.bom.flatten import Bom
from src.config import HTS_JSON, ROOT
from src.duty.rates import duty_rate, qualifies_for_special_program
from src.duty.scenarios import calculate_duty_scenarios, write_scenarios
from src.hts.index import HtsIndex, HtsRecord
from src.hts.render import render


@pytest.fixture
def demo():
    components = Bom.load(ROOT / "examples/two_parts.csv").components()
    index = HtsIndex.load(HTS_JSON)
    tree = render(index)
    classifier = Classifier(tree, None, None)
    classifications = []
    for part, code in zip(components, ["7318.16.00.60", "7318.21.00.30"]):
        choice = next(i for i, r in enumerate(tree.candidates) if r.htsno == code)
        selection = Selection(choice=choice, rationale=index.get(code).description)
        classifications.append(classifier._build_classification(part, selection))
    return components, classifications, index, {
        classification.code: ["CA"] for classification in classifications
    }


def test_two_parts_group_countries_and_round_to_cents(demo, tmp_path):
    result = calculate_duty_scenarios(*demo)
    assert result == {
        "DEMO-NUT": {
            "country_of_origin": "CN",
            "countries": {
                "CA": {"duty_usd": 0.0, "savings_usd": 0.0},
                "CN": {"duty_usd": 0.0, "savings_usd": 0.0},
            },
        },
        "DEMO-WASHER": {
            "country_of_origin": "CN",
            "countries": {
                "CA": {"duty_usd": 0.0, "savings_usd": 0.58},
                "CN": {"duty_usd": 0.58, "savings_usd": 0.0},
            },
        },
    }
    path = tmp_path / "nested/scenarios.jsonl"
    write_scenarios(result, path)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines == [{reference: scenario} for reference, scenario in result.items()]


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


def test_multiple_alternatives_compare_with_current_origin(demo):
    components, classifications, index, _ = demo
    countries = {row.code: ["CA", "MX", "JP"] for row in classifications}
    result = calculate_duty_scenarios(components, classifications, index, countries)
    washer = result["DEMO-WASHER"]["countries"]
    assert washer["CA"]["savings_usd"] == washer["MX"]["savings_usd"] == 0.58
    assert washer["JP"] == {"duty_usd": 0.29, "savings_usd": 0.29}


def test_baselines_follow_each_parts_origin_and_keep_negative_savings(demo):
    components, classifications, index, _ = demo
    components[1].country_of_origin = "JP"
    countries = {row.code: ["CN", "CA"] for row in classifications}
    result = calculate_duty_scenarios(components, classifications, index, countries)
    washer = result["DEMO-WASHER"]
    assert washer["country_of_origin"] == "JP"
    assert washer["countries"]["JP"] == {"duty_usd": 0.29, "savings_usd": 0.0}
    assert washer["countries"]["CN"]["savings_usd"] == -0.29
    assert washer["countries"]["CA"]["savings_usd"] == 0.29


def test_unclassified_and_unsupported_baseline_have_reasons(demo, tmp_path):
    components, classifications, index, countries = demo
    classifications[0] = replace(classifications[0], status="needs_review", reason="test_review")
    index.get(classifications[1].code).general = "2 cents/kg"
    result = calculate_duty_scenarios(components, classifications, index, countries)
    assert result == {
        "DEMO-NUT": {"country_of_origin": "CN", "reason": "test_review"},
        "DEMO-WASHER": {
            "country_of_origin": "CN", "reason": "unsupported_current_origin_rate",
        },
    }
    path = tmp_path / "scenarios.jsonl"
    write_scenarios(result, path)
    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {reference: scenario} for reference, scenario in result.items()
    ]


def test_unsupported_alternative_keeps_supported_baseline(demo):
    components, classifications, index, countries = demo
    index.get(classifications[1].code).special = "2 cents/kg (S)"
    result = calculate_duty_scenarios(components, classifications, index, countries)
    washer = result["DEMO-WASHER"]["countries"]
    assert washer["CN"] == {"duty_usd": 0.58, "savings_usd": 0.0}
    assert washer["CA"] == {"reason": "unsupported_alternative_rate"}


def test_country_normalization_deduplication_and_current_only(demo):
    components, classifications, index, _ = demo
    countries = {row.code.replace(".", ""): [" ca ", "CA", "CN"] for row in classifications}
    result = calculate_duty_scenarios(components, classifications, index, countries)
    assert all(set(part["countries"]) == {"CA", "CN"} for part in result.values())
    result = calculate_duty_scenarios(components, classifications, index, {})
    assert all(set(part["countries"]) == {"CN"} for part in result.values())
    assert all(part["countries"]["CN"]["savings_usd"] == 0 for part in result.values())


def test_missing_origin_is_not_treated_as_general(demo):
    demo[0][1].country_of_origin = ""
    result = calculate_duty_scenarios(*demo)
    assert "countries" in result["DEMO-NUT"]
    assert result["DEMO-WASHER"]["reason"] == "missing_or_invalid_origin"


def test_missing_classification_and_unknown_code(demo):
    components, classifications, index, countries = demo
    classifications = [replace(classifications[1], code="0000.00.00")]
    result = calculate_duty_scenarios(components, classifications, index, countries)
    assert result["DEMO-NUT"]["reason"] == "not_classified"
    assert result["DEMO-WASHER"]["reason"] == "unknown_hts_code"


def test_empty_scenarios_write_empty_file(demo, tmp_path):
    result = calculate_duty_scenarios([], [], demo[2], {})
    assert result == {}
    path = tmp_path / "scenarios.jsonl"
    write_scenarios(result, path)
    assert path.read_text() == ""


def test_invalid_scenario_country_is_rejected(demo):
    components, classifications, index, _ = demo
    with pytest.raises(ValueError, match="two-letter"):
        calculate_duty_scenarios(
            components, classifications, index, {classifications[0].code: ["Canada"]},
        )
