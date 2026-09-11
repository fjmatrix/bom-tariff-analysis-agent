"""Check injected benchmark data, spend-weighted hierarchy, and final brief integration."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from bls_fixtures import get_price_index
from src.bom.flatten import Bom, Component
from src.brief import build_brief_data, cost_pressure_markdown
from src.classify.classifier import Classification
from src.config import BOM_CSV
from src.cost_pressure import build_cost_pressure, classify_cost_pressure
from src.hts.index import HtsIndex
from src.run import run


def test_injected_indices_cover_purchased_bom_references():
    bom = Bom.load(BOM_CSV)
    for part in bom.components():
        record = get_price_index(part.reference)
        assert record["component_reference"] == part.reference
        assert record == get_price_index(part.reference)
        record["current_index"] = 0
        assert get_price_index(part.reference)["current_index"] > 0
    assert get_price_index("unknown-reference") is None
    assert get_price_index("7318.16.00.60") is None
    data = build_cost_pressure(bom.components(), bom, price_indices={
        part.reference: get_price_index(part.reference) for part in bom.components()
    })
    assert data["summary"]["total_baseline_spend"] == pytest.approx(1348.83)
    assert data["summary"]["items_with_index"] == 162
    assert data["products"][0]["total_index_implied_cost_pressure"] == pytest.approx(
        sum(item["index_implied_cost_pressure"] for item in data["items"])
    )


def test_pressure_rollup_preserves_repeated_references_and_absolute_quantities(tmp_path, monkeypatch):
    path = tmp_path / "bom.csv"
    path.write_text(
        "level,component_reference,component_name,component_quantity,parent_bom_reference,"
        "has_child_bom,unit_price_usd\n"
        "0,ROOT,Product,1,,True,300\n"
        "1,HEADING,Assembly,2,ROOT,True,75\n"
        "2,UP,Part,2,HEADING,False,50\n"
        "2,DOWN,Part,1,HEADING,False,50\n"
        "1,HEADING,Assembly,1,ROOT,True,150\n"
        "2,UP,Part,1,HEADING,False,50\n"
        "2,MISSING,Part,1,HEADING,False,100\n"
    )
    records = {
        "UP": {"current_index": 110, "baseline_index": 100},
        "DOWN": {"current_index": 90, "baseline_index": 100},
    }
    bom = Bom.load(path)
    data = build_cost_pressure(bom.components(), bom, price_indices=records)
    assert [item["index_implied_cost_pressure"] for item in data["items"]] == [10, -5, 5, None]
    assert [row["line"] for row in data["headings"]] == [1, 4]
    assert [row["total_baseline_spend"] for row in data["headings"]] == [150, 50]
    assert [row["total_index_implied_cost_pressure"] for row in data["headings"]] == [5, 5]
    product = data["products"][0]
    assert product["total_baseline_spend"] == 200
    assert product["total_index_implied_cost_pressure"] == 10
    assert product["weighted_index_change_pct"] == 0.05
    assert product["input_price_trend"] == "Rising"
    assert product["items_with_index"] == 3
    assert product["items_without_index"] == 1
    assert product["items_with_positive_pressure"] == 2
    assert product["items_with_negative_pressure"] == 1
    components = bom.components()
    facts = build_brief_data(
        components, [Classification(part.reference, part.name, "classified", "clean", part.reference, "")
                     for part in components], {part.reference: {"reason": "unclassified"} for part in components},
        HtsIndex([]), {}, bom=bom, bls_data={"indices": records},
    )
    brief = cost_pressure_markdown(facts)
    assert "| Product: ROOT — Product | $200.00 | $10.00 | +5.00% | 3/4 |" in brief
    assert "| Heading: HEADING — Assembly | $150.00 | $5.00 | +3.33% | 2/2 |" in brief
    assert "| Heading: HEADING — Assembly | $50.00 | $5.00 | +10.00% | 1/2 |" in brief
    assert "overlapping rows must not be added together" in brief


@pytest.mark.parametrize("filename, expected_headings", [
    ("20_parts.csv", ["M00148", "M00174"]),
    ("five_parts.csv", []),
])
def test_brief_shows_first_assembly_breakdown(filename, expected_headings):
    bom = Bom.load(BOM_CSV.parent / "examples" / filename)
    data = build_cost_pressure(bom.components(), bom, price_indices={
        part.reference: get_price_index(part.reference) for part in bom.components()
    })
    brief = cost_pressure_markdown({
        "summary": {"cost_pressure": "MEDIUM", "tariff_exposure": "Unknown",
                    "input_price_trend": "Stable", "exposure_is_partial": True},
        "index_implied_cost_pressure": data,
    })
    assert "| Product: M00653" in brief
    for row in data["headings"]:
        assert (f"| Heading: {row['component_reference']} —" in brief) == (
            row["component_reference"] in expected_headings
        )
    if filename == "20_parts.csv":
        assert "| $336.77 | $6.63 | +1.97% | 13/13 |" in brief
        assert "| $335.17 | $6.68 | +1.99% | 11/11 |" in brief
        assert "| $1.60 | $-0.05 | -3.00% | 2/2 |" in brief


def test_brief_keeps_assembly_alongside_direct_parts(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_text(
        "level,component_reference,component_name,component_quantity,parent_bom_reference,"
        "has_child_bom,unit_price_usd\n"
        "0,ROOT,Product,1,,True,36\n"
        "1,WRAPPER,Wrapper,1,ROOT,True,36\n"
        "2,M00218,Direct part,1,WRAPPER,False,18\n"
        "2,ASSEMBLY,Assembly,1,WRAPPER,True,18\n"
        "3,M00218,Nested part,1,ASSEMBLY,False,18\n"
    )
    bom = Bom.load(path)
    data = build_cost_pressure(bom.components(), bom, price_indices={
        part.reference: get_price_index(part.reference) for part in bom.components()
    })
    brief = cost_pressure_markdown({
        "summary": {"cost_pressure": "MEDIUM", "tariff_exposure": "Unknown",
                    "input_price_trend": "Stable", "exposure_is_partial": True},
        "index_implied_cost_pressure": data,
    })
    assert "| Product: ROOT — Product | $36.00 |" in brief
    assert "| Heading: ASSEMBLY — Assembly | $18.00 |" in brief
    assert "| Heading: WRAPPER" not in brief
    assert "Direct purchased parts remain included in the product total." in brief


def test_rollups_exclude_invalid_spend_and_index_from_both_totals(tmp_path, monkeypatch):
    path = tmp_path / "bom.csv"
    path.write_text(
        "level,component_reference,component_name,component_quantity,parent_bom_reference,"
        "has_child_bom,unit_price_usd\n"
        "0,ROOT,Product,1,,True,200\n"
        "1,VALID,Valid assembly,1,ROOT,True,100\n"
        "2,UP,Part,1,VALID,False,100\n"
        "2,BAD_SPEND,Part,1,VALID,False,nan\n"
        "2,BAD_INDEX,Part,1,VALID,False,100\n"
        "1,EMPTY,Unavailable assembly,1,ROOT,True,0\n"
        "2,BAD_QUANTITY,Part,-1,EMPTY,False,100\n"
        "0,ZERO,Zero spend product,1,,False,0\n"
    )
    bom = Bom.load(path)
    data = build_cost_pressure([], bom, price_indices={
        row.reference: {"current_index": 110, "baseline_index": 0 if row.reference == "BAD_INDEX" else 100}
        for row in bom.leaves
    })
    product = data["products"][0]
    assert product["total_baseline_spend"] == 100
    assert product["total_index_implied_cost_pressure"] == 10
    assert product["weighted_index_change_pct"] == 0.1
    assert product["items_with_valid_spend_and_index"] == 1
    brief = cost_pressure_markdown({
        "summary": {"cost_pressure": "MEDIUM", "tariff_exposure": "Unknown",
                    "input_price_trend": "Rising", "exposure_is_partial": True},
        "index_implied_cost_pressure": data,
    })
    assert "| Product: ROOT — Product | $100.00 | $10.00 | +10.00% | 1/4 |" in brief
    assert "| Heading: EMPTY — Unavailable assembly | $0.00 | N/A | N/A | 0/1 |" in brief
    assert "| Product: ZERO — Zero spend product | $0.00 | $0.00 | N/A | 1/1 |" in brief


@pytest.mark.parametrize("current,baseline,price,trend,pressure", [
    (102, 100, 10, "Stable", 0.2),
    (98, 100, 10, "Stable", -0.2),
    (97, 100, 10, "Falling", -0.3),
    (110, 0, 10, "Unknown", None),
    (float("nan"), 100, 10, "Unknown", None),
    (110, 100, float("nan"), "Unknown", None),
    (110, 100, -1, "Unknown", None),
    (110, 100, None, "Unknown", None),
    (110, 100, 0, "Unknown", 0),
])
def test_validity_and_trend_boundaries(monkeypatch, current, baseline, price, trend, pressure):
    part = Component("PART", "Part", 1, price, 1)
    summary = build_cost_pressure([part], price_indices={
        "PART": {"current_index": current, "baseline_index": baseline},
    })["summary"]
    assert summary["input_price_trend"] == trend
    assert summary["total_index_implied_cost_pressure"] == pytest.approx(pressure)
    assert summary["total_baseline_spend"] == (10 if pressure not in (None, 0) else 0)


@pytest.mark.parametrize("exposure,trend,tariff,pressure", [
    (5, "Falling", "High", "HIGH"),
    (1, "Rising", "Medium", "HIGH"),
    (1, "Stable", "Medium", "MEDIUM"),
    (0, "Rising", "Low", "MEDIUM"),
    (0, "Stable", "Low", "LOW"),
    (None, "Unknown", "Unknown", "MEDIUM"),
])
def test_simple_cost_pressure_rule(exposure, trend, tariff, pressure):
    assert classify_cost_pressure(exposure, trend) == (tariff, pressure)


def test_unmatched_index_is_unavailable_in_final_brief():
    part = Component("UNMATCHED", "Part", 1, 50, 1)
    facts = build_brief_data([part], [], {part.reference: {"reason": "mock"}}, HtsIndex([]), {})
    totals = facts["index_implied_cost_pressure"]["summary"]
    assert totals["total_baseline_spend"] == 0
    assert totals["total_index_implied_cost_pressure"] is None
    assert totals["weighted_index_change_pct"] is None
    assert totals["items_without_index"] == 1
    brief = cost_pressure_markdown(facts)
    assert "**Input price trend:** Unknown" in brief
    assert "- Index-implied cost pressure: N/A" in brief
    assert "- Weighted input-price change: N/A" in brief


def test_final_brief_has_no_indices_when_classification_is_unresolved(tmp_path, monkeypatch):
    monkeypatch.setattr("src.classify.cache.ROOT", tmp_path)

    async def classify(self, part):
        return Classification(part.reference, part.name, "unclassified", "no_supported_candidate", None, "")

    async def discover(codes, top_n):
        return {}

    monkeypatch.setattr("src.classify.classifier.Classifier.classify", classify)
    requests = []
    steps = iter(["classify_bom", "find_top_import_countries", "calculate_cost_analysis", None])

    async def create(**kwargs):
        requests.append(kwargs)
        name = next(steps)
        output = [SimpleNamespace(type="function_call", name=name,
                                  call_id=f"call-{len(requests)}", arguments="{}")] if name else []
        return SimpleNamespace(status="completed", output=output, output_text="Brief body.")

    brief = asyncio.run(run(
        BOM_CSV, 1, tmp_path, client=SimpleNamespace(responses=SimpleNamespace(create=create)),
        country_discovery=discover, on_event=lambda event: None,
    ))
    facts = json.loads((tmp_path / "brief_data.json").read_text())
    totals = facts["index_implied_cost_pressure"]["summary"]
    assert totals["total_baseline_spend"] == 0
    assert facts["summary"]["known_current_duty_per_finished_product_usd"] is None
    assert facts["index_implied_cost_pressure"]["headings"]
    assert len(requests) == 4
    assert brief.startswith("**Cost pressure:** MEDIUM")
    assert "**Tariff exposure:** Unknown (partial coverage)" in brief
    assert "- Index-implied cost pressure: N/A" in brief
    assert "- Weighted input-price change: N/A" in brief
    assert "not observed supplier price changes" in brief
    assert "mock" not in brief.lower()
    assert "Valid baseline spend: $0.00" in brief
    assert "**Input cost pressure by product and heading**" in brief
    for row in facts["index_implied_cost_pressure"]["headings"]:
        assert (f"Heading: {row['component_reference']} — {row['name']}" in brief) == (
            row["parent_line"] == 0
        )
    assert "0/162 items" in brief
    assert brief.endswith("Brief body.\n")
    assert (tmp_path / "brief.md").read_text() == brief
