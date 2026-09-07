"""Exercise the workflow with model and DataWeb responses replaced."""

import csv
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from src.classify.classifier import Selection
from src.config import HTS_JSON, ROOT
from src.hts.index import HtsIndex
from src.run import MAX_TURNS, BomAnalysis, run


def fake_discovery(codes, top_n):
    assert top_n == 1
    return {
        "period_start": "09/2025",
        "period_end": "08/2026",
        "measure": "consumption_customs_value_usd",
        "top_n": top_n,
        "rankings": [{
            "hts_code": code.replace(".", ""),
            "countries": [{
                "country": "CA",
                "country_name": "Canada",
                "customs_value_usd": 100,
            }],
        } for code in sorted(set(codes))],
        "errors": [],
    }


class DemoClient:
    def __init__(self, sequence=None):
        self.responses = self
        self.requests = []
        self.classifier_calls = 0
        self.sequence = iter(sequence or [
            "classify_bom",
            "find_top_import_countries",
            "calculate_duty_scenarios",
            None,
        ])

    def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        name = next(self.sequence)
        if name is not None:
            return SimpleNamespace(
                status="completed",
                output=[SimpleNamespace(
                    type="function_call", name=name,
                    call_id=f"call-{len(self.requests)}", arguments="{}",
                )],
            )
        result = next((
            json.loads(item["output"]) for item in reversed(kwargs["input"])
            if isinstance(item, dict) and item.get("type") == "function_call_output"
            and "current_duty_eur" in json.loads(item["output"])
        ), {"current_duty_eur": 0, "best_savings_eur": 0})
        brief = (f"Current duty: €{result['current_duty_eur']:.2f}. "
                 f"Modeled savings: €{result['best_savings_eur']:.2f}.")
        return SimpleNamespace(
            status="completed", output_text=brief,
            output=[SimpleNamespace(type="message", content=brief)],
        )

    def parse(self, **kwargs):
        self.classifier_calls += 1
        prompt = kwargs["input"][0]["content"]
        code = "7318.16.00.60" if "DEMO-NUT" in prompt else "7318.21.00.30"
        index = HtsIndex.load(HTS_JSON)
        choice = next(i for i, row in enumerate(index.candidates) if row.htsno == code)
        return SimpleNamespace(status="completed", output_parsed=Selection(
            choice=choice, evidence=index.get(code).description,
        ))


def execute(client, tmp_path):
    return run(
        ROOT / "examples/two_parts.csv", 1, tmp_path,
        client=client, country_discovery=fake_discovery,
    )


def test_workflow_passes_results_and_writes_all_deliverables(tmp_path, capsys):
    client = DemoClient()
    brief = execute(client, tmp_path)
    assert client.classifier_calls == 2
    assert len(client.requests) == 4
    expected_tools = {
        "classify_bom", "find_top_import_countries", "calculate_duty_scenarios",
    }
    for request in client.requests:
        assert {tool["name"] for tool in request["tools"]} == expected_tools
        assert request["tool_choice"] == "auto"
        assert request["parallel_tool_calls"] is False
    assert client.requests[1]["input"][-1]["call_id"] == "call-1"
    assert client.requests[2]["input"][-1]["call_id"] == "call-2"
    assert client.requests[3]["input"][-1]["call_id"] == "call-3"
    assert "€0.58" in brief
    assert (tmp_path / "brief.md").read_text() == brief
    with (tmp_path / "scenarios.csv").open(newline="") as fh:
        assert len(list(csv.DictReader(fh))) == 4
    with (tmp_path / "trade_countries.csv").open(newline="") as fh:
        countries = list(csv.DictReader(fh))
    assert len(countries) == 2
    assert {row["hts_code"] for row in countries} == {"7318160060", "7318210030"}
    output = capsys.readouterr().out
    assert "Agent calls classify_bom()" in output
    assert "Agent calls find_top_import_countries()" in output

    replay = DemoClient()
    execute(replay, tmp_path)
    assert replay.classifier_calls == 0


def test_agent_recovers_from_out_of_order_tools_and_early_brief(tmp_path):
    client = DemoClient([
        "find_top_import_countries",
        None,
        "classify_bom",
        "calculate_duty_scenarios",
        "find_top_import_countries",
        "calculate_duty_scenarios",
        None,
    ])
    execute(client, tmp_path)
    first_error = json.loads(client.requests[1]["input"][-1]["output"])
    assert "Call classify_bom" in first_error["error"]
    assert "before writing the brief" in client.requests[2]["input"][-1]["content"]
    calculator_error = json.loads(client.requests[4]["input"][-1]["output"])
    assert "find_top_import_countries" in calculator_error["error"]
    assert client.classifier_calls == 2
    assert (tmp_path / "brief.md").exists()


def test_repeated_tools_reuse_completed_work(tmp_path):
    client = DemoClient()
    calls = []

    def discovery(codes, top_n):
        calls.append((codes, top_n))
        return fake_discovery(codes, top_n)

    analysis = BomAnalysis(
        ROOT / "examples/two_parts.csv", 1, tmp_path, client, discovery,
    )
    first = analysis.classify_bom()
    assert analysis.classify_bom() == first
    assert client.classifier_calls == 2

    rankings = analysis.find_top_import_countries()
    assert analysis.find_top_import_countries() is rankings
    assert calls == [(["7318.16.00.60", "7318.21.00.30"], 1)]

    result = analysis.calculate_duty_scenarios()
    assert analysis.calculate_duty_scenarios() is result
    assert result["trade_data"]["period_start"] == "09/2025"


def test_unknown_tool_returns_feedback_without_dispatch(tmp_path):
    client = DemoClient([
        "delete_files",
        "classify_bom",
        "find_top_import_countries",
        "calculate_duty_scenarios",
        None,
    ])
    execute(client, tmp_path)
    error = json.loads(client.requests[1]["input"][-1]["output"])
    assert error == {"error": "Unknown tool: delete_files"}


@pytest.mark.parametrize("arguments", ['{"country": "CA"}', "invalid", "null", "[]"])
def test_bad_arguments_do_not_execute_tool(tmp_path, arguments):
    client = DemoClient(["classify_bom"] * MAX_TURNS)
    original = client.create

    def create(**kwargs):
        response = original(**kwargs)
        response.output[0].arguments = arguments
        return response

    client.create = create
    with pytest.raises(RuntimeError, match="did not finish"):
        execute(client, tmp_path)
    assert client.classifier_calls == 0
    assert not (tmp_path / "classified.csv").exists()


@pytest.mark.parametrize("sequence", [
    [None] * MAX_TURNS,
    ["classify_bom"] * MAX_TURNS,
])
def test_non_finishing_agent_is_bounded_and_does_not_write_brief(tmp_path, sequence):
    client = DemoClient(sequence)
    with pytest.raises(RuntimeError, match=f"within {MAX_TURNS} turns"):
        execute(client, tmp_path)
    assert len(client.requests) == MAX_TURNS
    assert not (tmp_path / "brief.md").exists()


def test_incomplete_response_does_not_execute_tool(tmp_path):
    client = DemoClient()
    original = client.create

    def create(**kwargs):
        response = original(**kwargs)
        response.status = "incomplete"
        return response

    client.create = create
    with pytest.raises(RuntimeError, match="Incomplete model response"):
        execute(client, tmp_path)
    assert client.classifier_calls == 0
    assert not (tmp_path / "brief.md").exists()
