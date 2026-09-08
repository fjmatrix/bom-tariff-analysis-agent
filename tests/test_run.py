"""Exercise the workflow with model and DataWeb responses replaced."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.classify.classifier import Selection
from src.config import HTS_JSON, ROOT
from src.hts.index import HtsIndex
from src.run import MAX_TURNS, BomAnalysis, run


@pytest.fixture(autouse=True)
def isolate_classification_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("src.classify.cache.ROOT", tmp_path)


def fake_usage(input_tokens=100, output_tokens=20):
    return SimpleNamespace(
        input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=input_tokens // 2),
        output_tokens_details=SimpleNamespace(reasoning_tokens=output_tokens // 2),
    )


async def fake_discovery(codes, top_n):
    assert top_n == 1
    return {
        code.replace(".", ""): {
            "period_start": "09/2025", "period_end": "08/2026",
            "countries": {"CA": {"customs_value_usd": 100}},
        } for code in sorted(set(codes))
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

    async def create(self, **kwargs):
        self.requests.append(deepcopy(kwargs))
        name = next(self.sequence)
        if name is not None:
            return SimpleNamespace(
                status="completed",
                usage=fake_usage(),
                output=[SimpleNamespace(
                    type="function_call", name=name,
                    call_id=f"call-{len(self.requests)}", arguments="{}",
                )],
            )
        result = next((
            json.loads(item["output"]) for item in reversed(kwargs["input"])
            if isinstance(item, dict) and item.get("type") == "function_call_output"
            and item["call_id"] in {
                call.call_id for call in kwargs["input"]
                if getattr(call, "name", None) == "calculate_duty_scenarios"
            }
        ), {})
        washer = result.get("scenarios", {}).get("DEMO-WASHER", {}).get("countries", {})
        brief = (f"Washer current duty: ${washer.get('CN', {}).get('duty_usd', 0):.2f}. "
                 f"Canada savings: ${washer.get('CA', {}).get('savings_usd', 0):.2f}.")
        return SimpleNamespace(
            status="completed", output_text=brief,
            usage=fake_usage(),
            output=[SimpleNamespace(type="message", content=brief)],
        )

    async def parse(self, **kwargs):
        self.classifier_calls += 1
        prompt = kwargs["input"][0]["content"]
        code = "7318.16.00.60" if "DEMO-NUT" in prompt else "7318.21.00.30"
        index = HtsIndex.load(HTS_JSON)
        choice = next(i for i, row in enumerate(index.candidates) if row.htsno == code)
        return SimpleNamespace(status="completed", usage=fake_usage(1000, 100), output_parsed=Selection(
            choice=choice, evidence=index.get(code).description,
        ))


def execute(client, tmp_path):
    return asyncio.run(run(
        ROOT / "examples/two_parts.csv", 1, tmp_path,
        client=client, country_discovery=fake_discovery,
    ))


def test_observed_run_preserves_outputs_and_emits_results_before_brief(tmp_path, capsys):
    expected = execute(DemoClient(), tmp_path / "plain")
    capsys.readouterr()
    observed = []
    actual = asyncio.run(run(
        ROOT / "examples/two_parts.csv", 1, tmp_path / "observed",
        client=DemoClient(), country_discovery=fake_discovery, on_event=observed.append,
    ))
    assert actual == expected
    assert capsys.readouterr().out == ""
    for name in ("brief.md", "brief_data.json", "scenarios.jsonl", "classified.csv", "components.csv"):
        assert (tmp_path / "plain" / name).read_bytes() == (tmp_path / "observed" / name).read_bytes()
    assert [event.sequence for event in observed] == list(range(1, len(observed) + 1))
    names = [event.name for event in observed]
    assert names.index("business_results") < names.index("brief")
    assert observed[-1].name == "run" and observed[-1].status == "completed"
    assert len([event for event in observed if event.name == "usage" and event.status == "cache_hit"]) == 2
    starts = {event.action_id for event in observed if event.status == "started"}
    ends = [event.action_id for event in observed if event.action_id and event.status != "started"]
    assert starts == set(ends) and len(starts) == len(ends)


def test_reused_tools_do_not_duplicate_part_events(tmp_path):
    observed = []
    asyncio.run(run(
        ROOT / "examples/two_parts.csv", 1, tmp_path,
        client=DemoClient(["classify_bom", "classify_bom", "find_top_import_countries",
                           "calculate_duty_scenarios", None]),
        country_discovery=fake_discovery, on_event=observed.append,
    ))
    assert len([event for event in observed if event.name == "classification" and event.status == "started"]) == 2
    assert any(event.name == "tool" and event.data["reused"] for event in observed)


def test_cancellation_closes_actions_and_finalizes_usage(tmp_path):
    observed = []

    async def exercise():
        client = DemoClient()
        entered = asyncio.Event()

        async def wait_for_model(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        client.parse = wait_for_model
        task = asyncio.create_task(run(
            ROOT / "examples/two_parts.csv", 1, tmp_path, client=client,
            country_discovery=fake_discovery, on_event=observed.append,
        ))
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    report = json.loads((tmp_path / "token_usage.json").read_text())
    assert report["status"] == "cancelled"
    assert report["events"][-1]["status"] == "cancelled"
    assert observed[-1].name == "run" and observed[-1].status == "cancelled"
    starts = {event.action_id for event in observed if event.status == "started"}
    ends = {event.action_id for event in observed if event.action_id and event.status != "started"}
    assert starts == ends
    assert not (tmp_path / "brief.md").exists()


def test_brief_failure_keeps_business_results(tmp_path):
    observed = []
    client = DemoClient()
    original = client.create

    async def fail_after_tools(**kwargs):
        if len(client.requests) == 3:
            raise RuntimeError("brief unavailable")
        return await original(**kwargs)

    client.create = fail_after_tools
    with pytest.raises(RuntimeError, match="brief unavailable"):
        asyncio.run(run(
            ROOT / "examples/two_parts.csv", 1, tmp_path, client=client,
            country_discovery=fake_discovery, on_event=observed.append,
        ))
    assert any(event.name == "business_results" for event in observed)
    assert (tmp_path / "brief_data.json").exists()
    assert observed[-1].name == "run" and observed[-1].status == "failed"


@pytest.mark.parametrize("fails", [False, True])
def test_owned_client_closes_after_success_or_failure(tmp_path, monkeypatch, fails):
    client = DemoClient()
    if fails:
        client.create = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr("src.run.AsyncOpenAI", lambda: context)
    if fails:
        with pytest.raises(RuntimeError, match="provider unavailable"):
            execute(None, tmp_path)
    else:
        execute(None, tmp_path)
    context.__aexit__.assert_awaited_once()


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
    for request in client.requests[1:3]:
        assert json.loads(request["input"][-1]["output"]) == {"status": "success"}
    assert "$0.58" in brief
    assert (tmp_path / "brief.md").read_text() == brief
    scenarios = [json.loads(line) for line in (tmp_path / "scenarios.jsonl").read_text().splitlines()]
    result = json.loads(client.requests[3]["input"][-1]["output"])
    assert scenarios == [{reference: part} for reference, part in result["scenarios"].items()]
    facts = json.loads((tmp_path / "brief_data.json").read_text())
    assert result["brief_data"] == facts
    assert facts["trade_period"] == {"period_start": "09/2025", "period_end": "08/2026"}
    assert facts["lookup_errors"] == {}
    assert facts["summary"]["known_current_duty_per_finished_product_usd"] == 0.58
    assert facts["summary"]["potentially_addressable_pct_known_exposure"] == 100
    assert facts["sourcing_opportunities"][0]["break_even_alternative_purchase_price_per_piece_usd"] == 0.529
    countries = [json.loads(line) for line in (tmp_path / "trade_countries.jsonl").read_text().splitlines()]
    assert len(countries) == 2
    assert {code for row in countries for code in row} == {"7318160060", "7318210030"}
    output = capsys.readouterr().out
    assert "Agent calls classify_bom()" in output
    assert "Agent calls find_top_import_countries()" in output
    assert "Tokens [classification DEMO-WASHER]" in output
    assert "Tokens [agent turn-4]" in output
    assert "Token totals [run]" in output
    usage = json.loads((tmp_path / "token_usage.json").read_text())
    assert usage["status"] == "completed"
    assert usage["totals"]["total_tokens"] == 2680
    assert usage["stages"]["classification"]["total_tokens"] == 2200
    assert usage["stages"]["agent"]["total_tokens"] == 480
    assert usage["totals"]["api_calls"] == 6
    assert [event["label"] for event in usage["events"]] == [
        "turn-1", "DEMO-NUT", "DEMO-WASHER", "turn-2", "turn-3", "turn-4",
    ]

    replay = DemoClient()
    replay_out = tmp_path / "another-output"
    execute(replay, replay_out)
    assert replay.classifier_calls == 0
    assert (tmp_path / ".cache/classification.sqlite3").is_file()
    assert not (replay_out / "selection_cache.json").exists()
    replay_usage = json.loads((replay_out / "token_usage.json").read_text())
    assert replay_usage["totals"]["total_tokens"] == 480
    assert replay_usage["stages"]["classification"]["cache_hits"] == 2
    assert replay_usage["stages"]["classification"]["api_calls"] == 0


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

    async def discovery(codes, top_n):
        calls.append((codes, top_n))
        return await fake_discovery(codes, top_n)

    analysis = BomAnalysis(
        ROOT / "examples/two_parts.csv", 1, tmp_path, client, discovery,
    )
    first = asyncio.run(analysis.classify_bom())
    assert asyncio.run(analysis.classify_bom()) == first == {"status": "success"}
    assert client.classifier_calls == 2

    assert asyncio.run(analysis.find_top_import_countries()) == {"status": "success"}
    rankings = analysis.country_rankings
    assert asyncio.run(analysis.find_top_import_countries()) == {"status": "success"}
    assert analysis.country_rankings is rankings
    assert calls == [(["7318.16.00.60", "7318.21.00.30"], 1)]

    result = asyncio.run(analysis.calculate_duty_scenarios())
    assert asyncio.run(analysis.calculate_duty_scenarios()) is result
    assert set(result) == {"DEMO-NUT", "DEMO-WASHER"}
    assert rankings["7318210030"]["period_start"] == "09/2025"


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


def test_country_lookup_errors_still_allow_current_origin_scenarios(tmp_path):
    async def discovery(codes, top_n):
        return {
            code.replace(".", ""): {
                "period_start": "09/2025", "period_end": "08/2026",
                "countries": {}, "error": "unavailable",
            } for code in codes
        }

    analysis = BomAnalysis(
        ROOT / "examples/two_parts.csv", 1, tmp_path, DemoClient(), discovery,
    )
    assert asyncio.run(analysis.classify_bom()) == {"status": "success"}
    assert asyncio.run(analysis.find_top_import_countries()) == {"status": "success"}
    result = asyncio.run(analysis.calculate_duty_scenarios())
    assert result["DEMO-WASHER"]["countries"] == {
        "CN": {"duty_usd": 0.58, "savings_usd": 0.0},
    }
    assert set(result) == {"DEMO-NUT", "DEMO-WASHER"}
    assert analysis.brief_data["trade_period"] == {
        "period_start": "09/2025", "period_end": "08/2026",
    }
    assert analysis.brief_data["lookup_errors"] == {
        "7318160060": "unavailable", "7318210030": "unavailable",
    }
    assert json.loads((tmp_path / "brief_data.json").read_text()) == analysis.brief_data


@pytest.mark.parametrize("arguments", ['{"country": "CA"}', "invalid", "null", "[]"])
def test_bad_arguments_do_not_execute_tool(tmp_path, arguments):
    client = DemoClient(["classify_bom"] * MAX_TURNS)
    original = client.create

    async def create(**kwargs):
        response = await original(**kwargs)
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
    usage = json.loads((tmp_path / "token_usage.json").read_text())
    assert usage["status"] == "failed"
    assert usage["stages"]["agent"]["api_calls"] == MAX_TURNS


def test_incomplete_response_does_not_execute_tool(tmp_path):
    client = DemoClient()
    original = client.create

    async def create(**kwargs):
        response = await original(**kwargs)
        response.status = "incomplete"
        return response

    client.create = create
    with pytest.raises(RuntimeError, match="Incomplete model response"):
        execute(client, tmp_path)
    assert client.classifier_calls == 0
    assert not (tmp_path / "brief.md").exists()
    usage = json.loads((tmp_path / "token_usage.json").read_text())
    assert usage["status"] == "failed"
    assert usage["totals"]["total_tokens"] == 120
    assert usage["events"][0]["status"] == "incomplete"


def test_classification_exception_preserves_outer_usage(tmp_path):
    client = DemoClient()

    async def parse(**kwargs):
        raise RuntimeError("provider unavailable")

    client.parse = parse
    with pytest.raises(RuntimeError, match="provider unavailable"):
        execute(client, tmp_path)
    usage = json.loads((tmp_path / "token_usage.json").read_text())
    assert usage["status"] == "failed"
    assert usage["totals"]["total_tokens"] == 120
    assert usage["totals"]["calls_without_usage"] == 1
    assert usage["events"][-1]["stage"] == "classification"
    assert usage["events"][-1]["total_tokens"] is None
