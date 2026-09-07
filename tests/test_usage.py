"""Token accounting uses reported totals and preserves unknown usage."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.usage import TokenUsage


def test_subsets_are_not_added_to_total_and_trace_is_saved_immediately(tmp_path):
    path = tmp_path / "nested/token_usage.json"
    tracker = TokenUsage(path)
    response = SimpleNamespace(
        id="response-1", status="incomplete", model="reported-model",
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=40, total_tokens=140,
            input_tokens_details=SimpleNamespace(cached_tokens=80),
            output_tokens_details=SimpleNamespace(reasoning_tokens=30),
        ),
    )
    asyncio.run(tracker.request(
        AsyncMock(return_value=response), "agent", "turn-1", model="requested-model",
    ))
    report = json.loads(path.read_text())
    assert report["status"] == "running"
    assert report["totals"]["total_tokens"] == 140
    assert report["totals"]["cached_input_tokens"] == 80
    assert report["totals"]["reasoning_tokens"] == 30
    assert report["events"][0]["response_id"] == "response-1"
    assert report["events"][0]["model"] == "reported-model"


def test_missing_usage_is_unknown_while_local_cache_is_zero():
    tracker = TokenUsage()
    tracker.record("agent", "turn-1", "model", status="completed")
    tracker.record("classification", "PART", "model")
    report = tracker.report()
    assert report["events"][0]["total_tokens"] is None
    assert report["events"][1]["total_tokens"] == 0
    assert report["totals"]["api_calls"] == 1
    assert report["totals"]["cache_hits"] == 1
    assert report["totals"]["calls_without_usage"] == 1
