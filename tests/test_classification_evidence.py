"""Candidate validation must not depend on the model copying HTS punctuation."""

import asyncio
import csv
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bom.flatten import Component
from src.classify.cache import ClassificationCache
from src.classify.classifier import Classifier, Selection, write_classified
from src.config import HTS_JSON
from src.hts.index import HtsIndex
from src.hts.render import render


@pytest.fixture(scope="module")
def tree():
    return render(HtsIndex.load(HTS_JSON))


@pytest.mark.parametrize("code,rationale", [
    ("8504.40.95.30", "Static converters: Other: Rectifiers and rectifying apparatus: Power supplies: With a power output exceeding 150 W but not exceeding 500 W"),
    ("8534.00.00.95", "Printed circuits ... Other"),
    ("8204.11.00.30", "Hand-operated spanners and wrenches ... Nonadjustable ... Open-end, box and combination open-end and box wrenches"),
    ("7318.15.60.40", "Having shanks or threads with a diameter of less than 6 mm — Socket screws — Other"),
    ("8204.11.00.60", "Hand-operated spanners and wrenches, and parts thereof: Nonadjustable, and parts thereof — Other (including parts)"),
])
def test_paraphrases_preserve_selection_and_attach_canonical_path(tree, code, rationale):
    # Representative choices exercise the five reported evidence formats;
    # the old output did not retain the model's actual selected indices.
    choice = next(i for i, candidate in enumerate(tree.candidates) if candidate.htsno == code)
    result = Classifier(tree, None, None)._build_classification(
        Component("X", "Part", 1, 1, 1), Selection(choice=choice, rationale=rationale),
    )
    assert result.status == "classified"
    assert result.code == code
    assert result.evidence == tree.candidates[choice].path
    assert result.rationale == rationale


@pytest.mark.parametrize("choice,reason", [(None, "no_supported_candidate"),
                                           (-1, "index_out_of_range"),
                                           (100000, "index_out_of_range")])
def test_invalid_choices_still_have_no_code(tree, choice, reason):
    result = Classifier(tree, None, None)._build_classification(
        Component("X", "Part", 1, 1, 1), Selection(choice=choice, rationale="Explanation"),
    )
    assert result.code is None
    assert result.reason == reason
    assert result.rationale == "Explanation"
    assert result.evidence == ""


def test_rationale_survives_cache_and_csv(tree, tmp_path):
    cache = ClassificationCache(tmp_path / "cache.sqlite3")
    parse = AsyncMock(return_value=SimpleNamespace(
        status="completed", output_parsed=Selection(choice=0, rationale="Likely match — assumed material"),
    ))
    classifier = Classifier(tree, cache, SimpleNamespace(responses=SimpleNamespace(parse=parse)))
    part = Component("X", "Part", 1, 1, 1)
    first = asyncio.run(classifier.classify(part))
    second = asyncio.run(classifier.classify(part))
    assert first == second
    assert parse.call_count == 1
    assert cache.get("X")["rationale"] == first.rationale
    write_classified([second], tmp_path / "classified.csv")
    with (tmp_path / "classified.csv").open() as source:
        row = next(csv.DictReader(source))
    assert row["rationale"] == first.rationale
    assert row["evidence"] == tree.candidates[0].path


def test_legacy_cache_migrates_and_regenerates_evidence(tree, tmp_path):
    path = tmp_path / "cache.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE classifications (reference TEXT PRIMARY KEY, htsno TEXT NOT NULL, evidence TEXT NOT NULL)")
        connection.execute("INSERT INTO classifications VALUES (?, ?, ?)",
                           ("X", tree.candidates[0].htsno, "Old excerpt"))
    cache = ClassificationCache(path)
    classifier = Classifier(tree, cache, None)
    result = asyncio.run(classifier.classify(Component("X", "Part", 1, 1, 1)))
    assert result.evidence == tree.candidates[0].path
    assert result.rationale == ""


def test_cache_reuses_code_after_candidate_reordering(tree, tmp_path):
    cache = ClassificationCache(tmp_path / "cache.sqlite3")
    candidate = tree.candidates[0]
    cache.put("X", candidate.htsno, "Old evidence", "Cached rationale")
    reordered = replace(tree, candidates=list(reversed(tree.candidates)))
    classifier = Classifier(reordered, cache, None)

    result = asyncio.run(classifier.classify(Component("X", "Part", 1, 1, 1)))

    assert result.code == candidate.htsno
    assert result.evidence == candidate.path
    assert result.rationale == "Cached rationale"
    assert classifier.usage.report()["totals"]["cache_hits"] == 1
    assert classifier.usage.report()["totals"]["api_calls"] == 0


def test_missing_cached_code_requests_and_caches_new_selection(tree, tmp_path):
    cache = ClassificationCache(tmp_path / "cache.sqlite3")
    cache.put("X", "missing-code", "Old evidence", "Old rationale")
    parse = AsyncMock(return_value=SimpleNamespace(
        status="completed", output_parsed=Selection(choice=0, rationale="New rationale"),
    ))
    classifier = Classifier(tree, cache, SimpleNamespace(responses=SimpleNamespace(parse=parse)))

    result = asyncio.run(classifier.classify(Component("X", "Part", 1, 1, 1)))

    assert result.code == tree.candidates[0].htsno
    assert cache.get("X")["htsno"] == result.code
    assert cache.get("X")["rationale"] == "New rationale"
    assert parse.call_count == 1
    assert classifier.usage.report()["totals"]["cache_hits"] == 0


@pytest.mark.parametrize("choice", [None, -1, 100000])
def test_unresolved_selection_is_not_cached(tree, tmp_path, choice):
    cache = ClassificationCache(tmp_path / "cache.sqlite3")
    parse = AsyncMock(return_value=SimpleNamespace(
        status="completed", output_parsed=Selection(choice=choice, rationale="Uncertain"),
    ))
    classifier = Classifier(tree, cache, SimpleNamespace(responses=SimpleNamespace(parse=parse)))

    result = asyncio.run(classifier.classify(Component("X", "Part", 1, 1, 1)))

    assert result.code is None
    assert cache.get("X") is None


@pytest.mark.parametrize("status,selection", [
    ("completed", None),
    ("incomplete", Selection(choice=0, rationale="Partial answer")),
])
def test_incomplete_response_is_not_cached(tree, tmp_path, status, selection):
    cache = ClassificationCache(tmp_path / "cache.sqlite3")
    parse = AsyncMock(return_value=SimpleNamespace(status=status, output_parsed=selection))
    classifier = Classifier(tree, cache, SimpleNamespace(responses=SimpleNamespace(parse=parse)))

    with pytest.raises(RuntimeError, match="X: no complete classification"):
        asyncio.run(classifier.classify(Component("X", "Part", 1, 1, 1)))

    assert cache.get("X") is None
