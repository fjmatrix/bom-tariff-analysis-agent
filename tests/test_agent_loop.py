"""Validate HTS selections and cache reuse without live model calls."""

from types import SimpleNamespace

import pytest

from src.classify.cache import SelectionCache
from src.classify.classifier import MODEL, Classifier, Selection, resolve
from src.bom.flatten import Component
from src.config import HTS_JSON
from src.hts.index import HtsIndex
from src.hts.render import render


@pytest.fixture
def tree():
    return render(HtsIndex.load(HTS_JSON))


@pytest.fixture
def component():
    return Component("X1", "Stainless steel hex nut M4", 1, 1.0, 1)


def test_valid_selection_resolves_to_loaded_code(component, tree):
    choice = next(i for i, r in enumerate(tree.candidates) if r.htsno == "7318.16.00.60")
    result = resolve(component, Selection(choice=choice, evidence="Nuts"), tree.candidates)
    assert result.status == "classified"
    assert result.code == "7318.16.00.60"


@pytest.mark.parametrize("choice,evidence,reason", [
    (None, "Motor is outside the loaded tree", "no_supported_candidate"),
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

    def __init__(self):
        self.responses = self
        self.requests = []

    def parse(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            status="completed", output_parsed=Selection(choice=0, evidence="Coach screws"),
        )


def test_disk_cache_and_changed_description(component, tree, tmp_path):
    client = StubClient()
    path = tmp_path / "cache.json"
    Classifier(tree, SelectionCache(path), client).run([component])
    classifier = Classifier(tree, SelectionCache.load(path), client)
    classifier.run([component])
    assert len(client.requests) == 1

    component.name = "Stainless steel coach screw"
    classifier.run([component])
    assert len(client.requests) == 2
    request = client.requests[-1]
    assert request["model"] == MODEL
    assert request["instructions"] == classifier.system
    assert request["text_format"] is Selection
    assert tree.text not in request["input"][0]["content"]


def test_prompt_change_invalidates_old_schema_cache(component, tree, tmp_path):
    client = StubClient()
    cache = SelectionCache(tmp_path / "cache.json")
    cache.put(component.reference, "legacy-prompt", {
        "choice": 0, "evidence": "Coach screws", "confidence": 0.9,
    })
    classifier = Classifier(tree, cache, client)
    classifier.run([component])
    classifier.system += "\nChanged classification instructions"
    classifier.run([component])
    assert len(client.requests) == 2


@pytest.mark.parametrize("status,selection", [
    ("completed", None),
    ("incomplete", Selection(choice=0, evidence="Coach screws")),
])
def test_missing_or_incomplete_answer_is_not_cached(
    component, tree, tmp_path, status, selection,
):
    client = SimpleNamespace(responses=SimpleNamespace(
        parse=lambda **kwargs: SimpleNamespace(status=status, output_parsed=selection),
    ))
    cache = SelectionCache(tmp_path / "cache.json")
    with pytest.raises(RuntimeError, match="no complete classification"):
        Classifier(tree, cache, client).run([component])
    assert cache.entries == {}
