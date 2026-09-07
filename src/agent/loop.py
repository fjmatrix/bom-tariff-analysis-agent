"""Stage 2 -- classify one component per LLM call, then branch in Python.

One call, no tools. The model reads a numbered tree and returns an integer; it
never sees an HTS code, so it cannot invent one, and it never sees a duty rate,
so it cannot be steered by one. Everything after the integer arrives is
deterministic, and that -- not the call -- is what makes this a loop.

Three of the branches are judgment calls worth stating:

**Abstention.** Most of the BOM is not in a heading-7318 tree, and a select
prompt with no abstain option confidently misclassifies every one of them.

**Suppression.** A rate is set at the 8-digit legal line, so alternatives
sharing the chosen line's first 8 digits cannot differ in rate. 7318.16.00.60
(stainless) and .85 (other) are both Free -- a DIN985 lock nut whose material
the name cannot settle costs a human to escalate and changes no number.

**Escalation without a second call.** A rate-bearing runner-up stops here. The
model saw both options in the one call and already chose between them; asking it
again is not escalation, and there would be no principle for picking a winner if
the answers disagreed.

Low confidence does neither. It demotes the code to `rate_source` -- the 8-digit
line the rate was inherited from. Origin-mix precision is lost, the rate stays
exactly right, and `selected_code` keeps what the model actually picked.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import BaseModel

from src.agent.cache import SelectionCache, fingerprint
from src.agent.prompts import component_prompt, system_prompt
from src.bom.flatten import Component
from src.hts.index import HtsIndex, HtsRecord
from src.hts.render import CandidateTree, render

MODEL = "gpt-5.6-luna"
MAX_OUTPUT_TOKENS = 16000
LOW_CONFIDENCE = 0.5  # below this the code falls back to its rate_source line


class Selection(BaseModel):
    """The model's whole output. Contract stated in prompts.SYSTEM."""

    choice: int | None
    abstain_chapter: str
    evidence: str
    unresolved: list[str]
    runner_up: int | None
    confidence: float


@dataclass
class Classification:
    reference: str
    name: str
    status: str  # classified | needs_review | out_of_scope
    reason: str
    choice: int | None  # the integer returned, before it was resolved
    code: str | None  # what the duty stages use; may be demoted to rate_source
    selected_code: str | None  # what `choice` resolved to, before demotion
    ad_valorem: float | None
    rate_source: str | None
    evidence: str
    unresolved: list[str]
    runner_up_code: str | None
    confidence: float
    abstain_chapter: str


def _could_change_duty(chosen: HtsRecord, alternatives: list[HtsRecord]) -> bool:
    """Could landing on one of these alternatives instead change the duty?

    A rate is fixed at the 8-digit legal line, so an alternative sharing the
    chosen line's first 8 digits is rate-identical by construction -- the string
    comparison that replaced a `get_siblings` tool call. Only where the prefix
    differs is there a rate to look up and compare.
    """
    legal_line = chosen.htsno.replace(".", "")[:8]
    return any(
        alt.htsno.replace(".", "")[:8] != legal_line
        and alt.ad_valorem != chosen.ad_valorem
        for alt in alternatives
    )


def _review_reason(
    selection: Selection, chosen: HtsRecord, runner_up: HtsRecord | None, index: HtsIndex
) -> str | None:
    """Why this needs a human, or None to accept the choice.

    The siblings are the candidates that differ from the chosen line by exactly
    the attribute their shared parent splits on, which is what an unresolved
    attribute puts back in play. Every candidate has a coded parent, so there is
    no rootless case to guard.
    """
    if not selection.evidence or selection.evidence not in chosen.path:
        return "evidence_not_in_path"  # '' is a substring of every path
    if runner_up and _could_change_duty(chosen, [runner_up]):
        return "runner_up_rate_differs"
    siblings = [
        r
        for r in index.children_of(chosen.parent)
        if r.is_candidate and r.htsno != chosen.htsno
    ]
    if selection.unresolved and _could_change_duty(chosen, siblings):
        return "unresolved_attribute_rate_bearing"
    return None


def resolve(
    component: Component,
    selection: Selection,
    index: HtsIndex,
    candidates: list[HtsRecord],
) -> Classification:
    """Validate the answer and pick a branch. Pure; no I/O, no model."""

    def in_range(i: int | None) -> bool:
        return i is None or 0 <= i < len(candidates)

    chosen = runner_up = None
    code = None

    if selection.choice is None:
        status, reason = "out_of_scope", "no_candidate_fits"
    elif not (in_range(selection.choice) and in_range(selection.runner_up)):
        status, reason = "needs_review", "index_out_of_range"
    else:
        chosen = candidates[selection.choice]
        runner_up = (
            candidates[selection.runner_up]
            if selection.runner_up is not None
            else None
        )
        code = chosen.htsno
        reason = _review_reason(selection, chosen, runner_up, index)
        status = "needs_review" if reason else "classified"
        if reason is None:
            reason = (
                "attribute_unknown_rate_invariant" if selection.unresolved else "clean"
            )
            if selection.confidence < LOW_CONFIDENCE:
                code = chosen.rate_source

    return Classification(
        reference=component.reference,
        name=component.name,
        status=status,
        reason=reason,
        choice=selection.choice,
        code=code,
        selected_code=chosen.htsno if chosen else None,
        ad_valorem=chosen.ad_valorem if chosen else None,
        rate_source=chosen.rate_source if chosen else None,
        evidence=selection.evidence,
        unresolved=selection.unresolved,
        runner_up_code=runner_up.htsno if runner_up else None,
        confidence=selection.confidence,
        abstain_chapter=selection.abstain_chapter,
    )


class Classifier:
    def __init__(
        self,
        index: HtsIndex,
        tree: CandidateTree,
        cache: SelectionCache,
        client=None,
    ):
        self.index = index
        self.tree = tree
        self.cache = cache
        self._client = client
        self.system = system_prompt(tree)
        self.fingerprint = fingerprint(self.system, MODEL)

    @property
    def client(self):
        """Constructed on first miss, so a fully cached replay needs no key."""
        if self._client is None:
            import openai

            self._client = openai.OpenAI()
        return self._client

    def select(self, component: Component) -> Selection:
        cached = self.cache.get(component.reference, self.fingerprint)
        if cached is not None:
            return Selection.model_validate(cached)

        response = self.client.responses.parse(
            model=MODEL,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            instructions=self.system,
            input=[{"role": "user", "content": component_prompt(component)}],
            text_format=Selection,
        )
        selection = response.output_parsed
        if selection is None:
            # No parsed content: the response was truncated mid-reasoning, or
            # the model refused. Neither is a classification, and neither is
            # worth a fallback -- there is nothing to fall back to.
            raise RuntimeError(
                f"{component.reference}: no selection in response "
                f"(status={response.status}, {response.incomplete_details})"
            )
        self.cache.put(component.reference, self.fingerprint, selection.model_dump())
        return selection

    def run(self, components: list[Component]) -> list[Classification]:
        out = [
            resolve(c, self.select(c), self.index, self.tree.candidates)
            for c in components
        ]
        self.cache.save()
        return out


# -- outputs -----------------------------------------------------------------


def write_classified(rows: list[Classification], path: str | Path) -> None:
    """The spreadsheet view. audit.jsonl below is the same rows plus the
    constants; this one exists to be opened and sorted by a human."""
    fields = list(Classification.__dataclass_fields__)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        for row in rows:
            record = asdict(row) | {"unresolved": ";".join(row.unresolved)}
            writer.writerow([record[f] for f in fields])


def write_audit(rows: list[Classification], path: str | Path, run: dict) -> None:
    """One object per component, each self-contained.

    With one call and no tools there is no trace to reconstruct: the integer,
    the phrase and its check, the branch, the rate and the line it was inherited
    from, and the constants in force are the whole record. `run` repeats on
    every line so a single line can be read on its own.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for row in rows:
            fh.write(json.dumps({**asdict(row), "run": run}) + "\n")


def main() -> None:
    from src.bom.flatten import Bom
    from src.config import BOM_CSV, HTS_JSON, OUT_DIR

    parser = argparse.ArgumentParser(
        description="Classify BOM components against the HTS."
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="classify only the N most expensive components, for a cheap smoke "
        "run. Default is all of them, one API call each on a cold cache.",
    )
    limit = parser.parse_args().limit

    index = HtsIndex.load(HTS_JSON)
    tree = render(index)
    cache = SelectionCache.load(OUT_DIR / "selection_cache.json")
    classifier = Classifier(index, tree, cache)

    components = Bom.load(BOM_CSV).components()[:limit]
    rows = classifier.run(components)

    run = {
        "model": MODEL,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "low_confidence": LOW_CONFIDENCE,
        "prompt_fingerprint": classifier.fingerprint,
        "candidates": len(tree.candidates),
    }
    write_classified(rows, OUT_DIR / "classified.csv")
    write_audit(rows, OUT_DIR / "audit.jsonl", run)

    counts = Counter(f"{r.status}/{r.reason}" for r in rows)
    width = max(len(k) for k in counts)
    for key in sorted(counts):
        print(f"{key:<{width}}  {counts[key]}")
    print(f"\nwrote {OUT_DIR / 'classified.csv'} and {OUT_DIR / 'audit.jsonl'}")


if __name__ == "__main__":
    main()
