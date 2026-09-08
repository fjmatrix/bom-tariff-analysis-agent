"""Validate HTS selections and cache reuse without live model calls."""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.classify.cache import ClassificationCache
from src.classify.prompts import component_prompt
from src.classify.classifier import MODEL, Classifier, HeadingSelection, Selection, resolve
from src.bom.flatten import Component
from src.config import HTS_JSON
from src.hts.index import HtsIndex
from src.hts.render import render


@pytest.fixture
def index():
    return HtsIndex.load(HTS_JSON)


@pytest.fixture
def tree(index):
    return render(index)


@pytest.fixture
def component():
    return Component("X1", "Stainless steel hex nut M4", 1, 1.0, 1)


def test_valid_selection_resolves_to_loaded_code(component, tree):
    choice = next(i for i, r in enumerate(tree.candidates) if r.htsno == "7318.16.00.60")
    result = resolve(component, Selection(choice=choice, evidence="Nuts"), tree.candidates)
    assert result.status == "classified"
    assert result.code == "7318.16.00.60"


@pytest.mark.parametrize("choice,evidence,reason", [
    (None, "Motor is outside the loaded tree", "schedule_gap"),
    (-1, "Other", "index_out_of_range"),
    (999, "Other", "index_out_of_range"),
    (0, "", "evidence_not_in_path"),
    (0, "invented rationale", "evidence_not_in_path"),
])
def test_invalid_and_abstained_selections_have_no_usable_code(
    component, tree, choice, evidence, reason,
):
    result = resolve(component, Selection(choice=choice, evidence=evidence), tree.candidates)
    assert result.status != "classified"
    assert result.code is None
    assert result.reason == reason
    assert result.evidence == evidence


class StubClient:
    """Counts calls so a cache hit is observable rather than assumed."""

    def __init__(self, index):
        self.index = index
        self.responses = self
        self.requests = []
        choice = next(i for i, r in enumerate(render(index, headings=["7318"], include_parents=True).candidates)
                      if r.htsno == "7318.16.00.60")
        self.selection = Selection(choice=choice, evidence="Nuts")

    async def parse(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            status="completed", output_parsed=(HeadingSelection(
                choices=[next(i for i, r in enumerate(r for r in self.index.records if r.digits == 4)
                              if r.htsno == "7318")],
                reason="matched", rationale="Steel nut", missing_attributes=[],
            ) if kwargs["text_format"] is HeadingSelection else self.selection),
        )


def test_disk_cache_refreshes_after_description_change(component, tree, index, tmp_path):
    client = StubClient(index)
    path = tmp_path / "nested/cache.sqlite3"
    first = asyncio.run(Classifier(index, ClassificationCache(path), client).run([component]))[0]
    classifier = Classifier(index, ClassificationCache(path), client)
    asyncio.run(classifier.run([component]))
    assert len(client.requests) == 2

    component.name = "Stainless steel coach screw"
    result = asyncio.run(classifier.run([component]))[0]
    assert len(client.requests) == 4
    assert result.name == component.name
    assert result.code == first.code
    assert classifier.usage.report()["totals"]["cache_hits"] == 1
    request = client.requests[0]
    assert request["model"] == MODEL
    assert request["instructions"] == classifier.system
    assert request["text_format"] is HeadingSelection
    assert tree.text not in request["input"][0]["content"]


def test_prompt_and_model_changes_reuse_cache(component, tree, index, tmp_path, monkeypatch):
    client = StubClient(index)
    classifier = Classifier(index, ClassificationCache(tmp_path / "cache.sqlite3"), client)
    asyncio.run(classifier.run([component]))
    classifier.system += "\nChanged classification instructions"
    monkeypatch.setattr("src.classify.classifier.MODEL", "another-model")
    asyncio.run(classifier.run([component]))
    assert len(client.requests) == 2


def test_reordered_candidates_reuse_hts_number(component, tree, index, tmp_path):
    client = StubClient(index)
    cache = ClassificationCache(tmp_path / "cache.sqlite3")
    first = asyncio.run(Classifier(index, cache, client).run([component]))[0]
    reordered = HtsIndex(list(reversed(index.records)))
    result = asyncio.run(Classifier(reordered, cache, client).run([component]))[0]
    assert result == first
    assert len(client.requests) == 2


@pytest.mark.parametrize("htsno,evidence", [
    ("missing-code", "Coach screws"),
    ("7318.16.00.60", "invented rationale"),
    ("7318.16.00.60", ""),
])
def test_unsupported_cached_classification_is_refreshed(
    component, tree, index, tmp_path, htsno, evidence,
):
    client = StubClient(index)
    cache = ClassificationCache(tmp_path / "cache.sqlite3")
    cache.put(component.reference, htsno, evidence,
              hashlib.sha256(component_prompt(component).encode()).hexdigest())
    classifier = Classifier(index, cache, client)
    result = asyncio.run(classifier.run([component]))[0]
    asyncio.run(classifier.run([component]))
    assert len(client.requests) == 2
    assert cache.get(component.reference) == {
        "reference": component.reference, "htsno": result.code, "evidence": "Nuts", "rationale": "",
    }


def test_cache_writes_are_shared_and_only_store_classification_fields(tmp_path):
    path = tmp_path / "cache.sqlite3"
    first = ClassificationCache(path)
    second = ClassificationCache(path)
    first.put("X'1", "7318.11.00.00", "Coach screws")
    second.put("X2", "7318.16.00.60", "Nuts")
    second.put("X'1", "7318.16.00.60", "Nuts")
    assert first.get("X'1") == {
        "reference": "X'1", "htsno": "7318.16.00.60", "evidence": "Nuts", "rationale": "",
    }
    assert first.get("X2") == second.get("X2")
    assert first.get("missing") is None
    with closing(sqlite3.connect(path)) as connection:
        columns = connection.execute("PRAGMA table_info(classifications)").fetchall()
        assert [column[1] for column in columns] == ["reference", "htsno", "evidence", "context_hash", "rationale"]
        assert connection.execute("SELECT COUNT(*) FROM classifications").fetchone()[0] == 2


@pytest.mark.parametrize("choice,evidence", [
    (None, "No supported candidate"),
    (-1, "Other"),
    (0, "invented rationale"),
])
def test_unresolved_classification_is_not_cached(component, tree, index, tmp_path, choice, evidence):
    client = StubClient(index)
    client.selection = Selection(choice=choice, evidence=evidence)
    cache = ClassificationCache(tmp_path / "cache.sqlite3")
    result = asyncio.run(Classifier(index, cache, client).run([component]))[0]
    assert result.code == "7318"
    assert result.status == "partial"
    assert cache.get(component.reference) is None


def test_completed_classification_survives_later_failure(component, tree, index, tmp_path):
    path = tmp_path / "cache.sqlite3"
    client = StubClient(index)
    parse = client.parse

    async def fail_second(**kwargs):
        if len(client.requests) >= 2:
            raise RuntimeError("provider unavailable")
        return await parse(**kwargs)

    client.parse = fail_second
    with pytest.raises(RuntimeError, match="provider unavailable"):
        asyncio.run(Classifier(index, ClassificationCache(path), client).run([
            component, replace(component, reference="X2"),
        ]))
    assert ClassificationCache(path).get(component.reference)["htsno"] == "7318.16.00.60"
    assert ClassificationCache(path).get("X2") is None


@pytest.mark.parametrize("status,selection", [
    ("completed", None),
    ("incomplete", Selection(choice=0, evidence="Coach screws")),
])
def test_missing_or_incomplete_answer_is_not_cached(
    component, tree, index, tmp_path, status, selection,
):
    client = SimpleNamespace(responses=SimpleNamespace(
        parse=AsyncMock(return_value=SimpleNamespace(status=status, output_parsed=selection)),
    ))
    cache = ClassificationCache(tmp_path / "cache.sqlite3")
    with pytest.raises(RuntimeError, match="no complete classification"):
        asyncio.run(Classifier(index, cache, client).run([component]))
    assert cache.get(component.reference) is None
