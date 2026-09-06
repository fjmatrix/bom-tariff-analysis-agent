"""M4 assertions.

`resolve()` is pure, so every branch is testable without a key, a network call
or a recorded fixture. The model's half is one integer; these tests are about
what Python does with it.
"""

import json

import pytest

from src.agent.cache import SelectionCache, fingerprint
from src.agent.loop import LOW_CONFIDENCE, MODEL, Classifier, Selection, resolve
from src.agent.prompts import system_prompt
from src.bom.flatten import Component
from src.config import HTS_JSON
from src.hts.index import HtsIndex
from src.hts.render import render


@pytest.fixture(scope="module")
def index() -> HtsIndex:
    return HtsIndex.load(HTS_JSON)


@pytest.fixture(scope="module")
def tree(index):
    return render(index)


@pytest.fixture(scope="module")
def n(tree):
    """code -> the number the model would return for it."""
    return {r.htsno: i for i, r in enumerate(tree.candidates)}


def component():
    return Component(
        reference="X1",
        name="Socket Head Cap Screw DIN912 M6x16",
        quantity=1,
        unit_price_eur=1.0,
        occurrences=1,
        assembly_paths=[("EVO", "EVO - DIN912 Bag - M6x16")],
    )


def selection(**kw):
    base = dict(
        choice=None,
        abstain_chapter="",
        evidence="",
        unresolved=[],
        runner_up=None,
        confidence=0.9,
    )
    return Selection(**{**base, **kw})


def run(sel, index, tree):
    return resolve(component(), sel, index, tree.candidates)


# -- abstain -----------------------------------------------------------------

def test_abstain_records_the_chapter_and_stops(index, tree):
    """Most of the BOM takes this path. An SMPS is 8504 and there is no honest
    line for it in a heading-7318 tree."""
    out = run(selection(choice=None, abstain_chapter="8504"), index, tree)
    assert (out.status, out.reason) == ("out_of_scope", "no_candidate_fits")
    assert out.code is None and out.ad_valorem is None
    assert out.abstain_chapter == "8504"


# -- validation --------------------------------------------------------------

def test_fabricated_evidence_escalates(index, tree, n):
    """A well-formed choice with a rationale the model wrote rather than read.
    The range check cannot see this; the substring check can."""
    out = run(
        selection(choice=n["7318.15.60.40"], evidence="hex socket drive, metric"),
        index,
        tree,
    )
    assert (out.status, out.reason) == ("needs_review", "evidence_not_in_path")
    assert out.selected_code == "7318.15.60.40"  # the choice is still recorded


def test_empty_evidence_does_not_pass_vacuously(index, tree, n):
    """'' is a substring of every path."""
    out = run(selection(choice=n["7318.15.60.40"], evidence=""), index, tree)
    assert out.reason == "evidence_not_in_path"


def test_evidence_may_be_quoted_from_an_ancestor_line(index, tree, n):
    """'Socket screws:' is a superior row -- no code, and the whole
    discriminator. It is in the chosen line's path, so it is valid evidence."""
    out = run(
        selection(choice=n["7318.15.60.40"], evidence="Socket screws:"), index, tree
    )
    assert out.status == "classified"


def test_out_of_range_choice_escalates(index, tree):
    out = run(selection(choice=999, evidence="Other"), index, tree)
    assert (out.status, out.reason) == ("needs_review", "index_out_of_range")
    assert out.code is None


# -- the two ambiguity branches ----------------------------------------------

def test_din912_runner_up_at_a_different_rate_escalates(index, tree, n):
    """The headline case. Machine screw (Free) and socket screw (6.2%) are both
    supportable from the text; the difference is EUR 1.15 on this family and a
    legal question, not a reading-comprehension one."""
    out = run(
        selection(
            choice=n["7318.15.60.40"],
            evidence="Socket screws:",
            runner_up=n["7318.15.40.00"],
        ),
        index,
        tree,
    )
    assert (out.status, out.reason) == ("needs_review", "runner_up_rate_differs")
    assert out.runner_up_code == "7318.15.40.00"


def test_runner_up_at_the_same_rate_does_not_escalate(index, tree, n):
    """A genuine second reading that cannot move a number is not a review."""
    out = run(
        selection(
            choice=n["7318.16.00.60"],
            evidence="Of stainless steel",
            runner_up=n["7318.16.00.85"],
        ),
        index,
        tree,
    )
    assert out.status == "classified"


def test_din985_unresolved_attribute_is_suppressed_when_rate_invariant(index, tree, n):
    """The negative control. The name cannot settle stainless vs other, and
    7318.16.00.60 and .85 are both Free -- escalating costs a human and changes
    no number. Decide, mark it, move on."""
    out = run(
        selection(
            choice=n["7318.16.00.60"], evidence="Nuts", unresolved=["material"]
        ),
        index,
        tree,
    )
    assert (out.status, out.reason) == ("classified", "attribute_unknown_rate_invariant")
    assert out.code == "7318.16.00.60"


def test_unresolved_attribute_escalates_when_a_sibling_carries_another_rate(
    index, tree, n
):
    """Same branch, opposite answer. 7318.19.00.00 is the catch-all under the
    4-digit heading, so its siblings are whole subheadings -- coach screws at
    12.5%, rivets at Free, cotters at 3.8%. An unresolved attribute there really
    can move the rate, and the suppression must not fire."""
    out = run(
        selection(
            choice=n["7318.19.00.00"],
            evidence="Other",
            unresolved=["article type"],
        ),
        index,
        tree,
    )
    assert (out.status, out.reason) == (
        "needs_review",
        "unresolved_attribute_rate_bearing",
    )


def test_candidate_zero_is_a_real_runner_up(index, tree, n):
    """[0] is Coach screws at 12.5%. A falsy-zero check here reads it as 'no
    runner-up' and silently skips the escalation."""
    out = run(
        selection(choice=n["7318.16.00.60"], evidence="Nuts", runner_up=0), index, tree
    )
    assert (out.status, out.reason) == ("needs_review", "runner_up_rate_differs")
    assert out.runner_up_code == "7318.11.00.00"


def test_suppression_is_the_common_case_by_construction(index, tree):
    """How often each side of that branch can fire, measured rather than hoped.

    41 of the 48 candidates are 10-digit suffixes under an 8-digit legal line,
    so their siblings share the rate by construction and an unresolved attribute
    there can never escalate. Only the 7 that hang directly off the 4-digit
    heading have siblings that are whole subheadings with rates of their own.
    """
    cross_rate = [
        r.htsno
        for r in index.candidates
        if any(
            s.htsno.replace(".", "")[:8] != r.htsno.replace(".", "")[:8]
            for s in index.children_of(r.parent)
            if s.is_candidate
        )
    ]
    assert len(cross_rate) == 7
    assert all(index.get(c).parent == "7318" for c in cross_rate)


# -- confidence fallback -----------------------------------------------------

def test_low_confidence_demotes_to_rate_source(index, tree, n):
    """Give up the statistical suffix, keep the rate. The model's own pick stays
    in `selected_code` so the audit can still show what it said."""
    out = run(
        selection(
            choice=n["7318.15.60.40"],
            evidence="Socket screws:",
            confidence=LOW_CONFIDENCE - 0.1,
        ),
        index,
        tree,
    )
    assert out.status == "classified"
    assert out.selected_code == "7318.15.60.40"
    assert out.code == "7318.15.60" == out.rate_source
    assert out.ad_valorem == pytest.approx(0.062)


def test_low_confidence_is_a_no_op_where_the_line_states_its_own_rate(index, tree, n):
    """8 of the 48 candidates hang off a 4- or 6-digit heading; for them
    rate_source is the candidate itself and there is nothing to fall back to."""
    out = run(
        selection(
            choice=n["7318.15.40.00"], evidence="Machine screws", confidence=0.1
        ),
        index,
        tree,
    )
    assert out.code == out.selected_code == "7318.15.40.00"


# -- cache -------------------------------------------------------------------

class StubClient:
    """Stands in for openai.OpenAI. Counts calls so a cache hit is
    observable rather than assumed."""

    def __init__(self, selection):
        self.selection = selection
        self.calls = 0
        self.responses = self

    def parse(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return type("R", (), {"output_parsed": self.selection})()


def test_second_run_replays_from_cache_without_calling(index, tree, tmp_path):
    sel = selection(choice=25, evidence="Socket screws:")
    client = StubClient(sel)
    cache = SelectionCache(tmp_path / "cache.json")

    Classifier(index, tree, cache, client).run([component()])
    assert client.calls == 1

    reloaded = SelectionCache.load(tmp_path / "cache.json")
    Classifier(index, tree, reloaded, client).run([component()])
    assert client.calls == 1  # served from disk


def test_a_changed_prompt_invalidates_the_cache(index, tree, tmp_path):
    """A cached `choice: 25` means nothing once the tree it indexed changes."""
    sel = selection(choice=25, evidence="Socket screws:")
    client = StubClient(sel)
    cache = SelectionCache(tmp_path / "cache.json")
    Classifier(index, tree, cache, client).run([component()])

    stale = SelectionCache.load(tmp_path / "cache.json")
    assert stale.get("X1", fingerprint("a different tree", MODEL)) is None


def test_the_system_prompt_is_sent_as_a_stable_prefix(index, tree, tmp_path):
    """~1,150 identical tokens per component across 147 components.

    Nothing here can make the provider cache that prefix. What this asserts is
    the part the code does own: the tree goes in `instructions` rather than
    being pasted into the per-component message, so the prefix is byte-identical
    call to call and cacheable at all."""
    client = StubClient(selection(choice=25, evidence="Socket screws:"))
    Classifier(index, tree, SelectionCache(tmp_path / "c.json"), client).run(
        [component()]
    )
    assert client.kwargs["instructions"] == system_prompt(tree)
    assert tree.text not in client.kwargs["input"][0]["content"]
    assert client.kwargs["text_format"] is Selection
    assert "tools" not in client.kwargs


# -- outputs -----------------------------------------------------------------

def test_audit_line_carries_the_constants_and_the_inheritance(index, tree, tmp_path, n):
    """A reviewer cannot check a duty number without knowing which line supplied
    the rate -- 40 of the 48 candidates inherit theirs."""
    from src.agent.loop import write_audit

    rows = [
        run(selection(choice=n["7318.15.60.40"], evidence="Socket screws:"), index, tree)
    ]
    path = tmp_path / "audit.jsonl"
    write_audit(rows, path, {"model": MODEL, "low_confidence": LOW_CONFIDENCE})

    record = json.loads(path.read_text().splitlines()[0])
    assert record["choice"] == n["7318.15.60.40"]
    assert record["selected_code"] == "7318.15.60.40"
    assert record["rate_source"] == "7318.15.60"
    assert record["run"]["low_confidence"] == LOW_CONFIDENCE
