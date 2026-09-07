"""Select one HTS candidate per component, validate it, and cache the answer."""

import csv
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import BaseModel

from src.classify.cache import SelectionCache, fingerprint
from src.classify.prompts import component_prompt, system_prompt
from src.bom.flatten import Component
from src.hts.index import HtsRecord
from src.hts.render import CandidateTree

MODEL = "gpt-5.6-luna"
MAX_OUTPUT_TOKENS = 16000


class Selection(BaseModel):
    choice: int | None
    evidence: str


@dataclass
class Classification:
    reference: str
    name: str
    status: str
    reason: str
    code: str | None
    evidence: str


def resolve(
    component: Component, selection: Selection, candidates: list[HtsRecord],
) -> Classification:
    code = None
    if selection.choice is None:
        status, reason = "unclassified", "no_supported_candidate"
    elif not 0 <= selection.choice < len(candidates):
        status, reason = "needs_review", "index_out_of_range"
    else:
        chosen = candidates[selection.choice]
        if not selection.evidence or selection.evidence not in chosen.path:
            status, reason = "needs_review", "evidence_not_in_path"
        else:
            status, reason = "classified", "clean"
            code = chosen.htsno
    return Classification(
        component.reference, component.name, status, reason, code, selection.evidence,
    )


class Classifier:
    def __init__(self, tree: CandidateTree, cache: SelectionCache, client):
        self.tree = tree
        self.cache = cache
        self.client = client
        self.system = system_prompt(tree)

    def select(self, component: Component) -> Selection:
        prompt = component_prompt(component)
        key = fingerprint(f"{self.system}\n{prompt}", MODEL)
        cached = self.cache.get(component.reference, key)
        if cached is not None:
            return Selection.model_validate(cached)
        response = self.client.responses.parse(
            model=MODEL,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            instructions=self.system,
            input=[{"role": "user", "content": prompt}],
            text_format=Selection,
        )
        if response.status != "completed" or response.output_parsed is None:
            raise RuntimeError(f"{component.reference}: no complete classification")
        selection = response.output_parsed
        self.cache.put(component.reference, key, selection.model_dump())
        return selection

    def run(self, components: list[Component]) -> list[Classification]:
        rows = [resolve(c, self.select(c), self.tree.candidates) for c in components]
        self.cache.save()
        return rows


def write_classified(rows: list[Classification], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(Classification.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
