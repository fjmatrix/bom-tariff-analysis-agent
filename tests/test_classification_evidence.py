"""Candidate validation must not depend on the model copying HTS punctuation."""

import asyncio
import csv
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.bom.flatten import Component
from src.classify.cache import ClassificationCache
from src.classify.classifier import Classifier, Selection, resolve, write_classified
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
    result = resolve(Component("X", "Part", 1, 1, 1),
                     Selection(choice=choice, rationale=rationale), tree.candidates)
    assert result.status == "classified"
    assert result.code == code
    assert result.evidence == tree.candidates[choice].path
    assert result.rationale == rationale


@pytest.mark.parametrize("choice,reason", [(None, "no_supported_candidate"),
                                           (-1, "index_out_of_range"),
                                           (100000, "index_out_of_range")])
def test_invalid_choices_still_have_no_code(tree, choice, reason):
    result = resolve(Component("X", "Part", 1, 1, 1),
                     Selection(choice=choice, rationale="Explanation"), tree.candidates)
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
    first = asyncio.run(classifier.run([part]))[0]
    second = asyncio.run(classifier.run([part]))[0]
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
    result = asyncio.run(classifier.run([Component("X", "Part", 1, 1, 1)]))[0]
    assert result.evidence == tree.candidates[0].path
    assert result.rationale == ""
