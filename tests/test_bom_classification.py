"""BOM classification owns batch progress, completion state, and CSV output."""

import asyncio
import csv
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.classify.classifier import Selection
from src.config import ROOT
from src.events import Events
from src.run import BomAnalysis


@pytest.fixture
def analysis(tmp_path, monkeypatch):
    monkeypatch.setattr("src.classify.cache.ROOT", tmp_path)
    parse = AsyncMock(return_value=SimpleNamespace(
        status="completed", output_parsed=Selection(choice=0, rationale="Match"),
    ))
    observed = []
    analysis = BomAnalysis(
        ROOT / "examples/two_parts.csv", 1, tmp_path / "output",
        SimpleNamespace(responses=SimpleNamespace(parse=parse)),
        events=Events(observed.append),
    )
    observed.clear()
    return analysis, parse, observed


def test_classify_bom_writes_rows_and_emits_progress_once(analysis):
    analysis, parse, observed = analysis

    assert asyncio.run(analysis.classify_bom()) == {"status": "success"}
    assert parse.call_count == len(analysis.components)
    assert [row.reference for row in analysis.classifications] == [
        part.reference for part in analysis.components
    ]
    for position, row in enumerate(analysis.classifications, 1):
        started, completed = observed[(position - 1) * 2:position * 2]
        assert started.name == completed.name == "classification"
        assert started.status == "started"
        assert completed.status == "completed"
        assert started.action_id == completed.action_id
        assert completed.data == {
            "reference": row.reference, "position": position,
            "total": len(analysis.components), "classification": asdict(row),
        }
    with (analysis.out_dir / "classified.csv").open() as source:
        rows = list(csv.DictReader(source))
    assert rows == [asdict(row) for row in analysis.classifications]
    assert (analysis.out_dir / "components.csv").exists()

    event_count = len(observed)
    assert asyncio.run(analysis.classify_bom()) == {"status": "success"}
    assert parse.call_count == len(analysis.components)
    assert len(observed) == event_count


@pytest.mark.parametrize("error,status", [
    (RuntimeError("provider unavailable"), "failed"),
    (asyncio.CancelledError(), "cancelled"),
])
def test_interrupted_batch_keeps_cached_progress_and_can_retry(analysis, error, status):
    analysis, parse, observed = analysis
    response = parse.return_value
    parse.side_effect = [response, error]
    first, second = analysis.components

    with pytest.raises(type(error)):
        asyncio.run(analysis.classify_bom())

    assert analysis.classifications is None
    assert analysis.classifier.cache.get(first.reference) is not None
    assert analysis.classifier.cache.get(second.reference) is None
    assert [event.status for event in observed] == ["started", "completed", "started", status]
    assert not (analysis.out_dir / "classified.csv").exists()

    parse.side_effect = None
    assert asyncio.run(analysis.classify_bom()) == {"status": "success"}
    assert parse.call_count == 3
    assert len(analysis.classifications) == 2
    assert observed[1].data["classification"] == asdict(analysis.classifications[0])
    assert (analysis.out_dir / "classified.csv").exists()
