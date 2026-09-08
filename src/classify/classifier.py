"""Select one HTS candidate per component, validate it, and cache the answer."""

import csv
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import BaseModel

from src.classify.cache import ClassificationCache
from src.classify.prompts import component_prompt, system_prompt
from src.bom.flatten import Component
from src.hts.index import HtsRecord
from src.hts.render import CandidateTree
from src.usage import TokenUsage
from src.events import Events

MODEL = "gpt-5.6-terra"
MAX_OUTPUT_TOKENS = 1000


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
    def __init__(self, tree: CandidateTree, cache: ClassificationCache, client, usage=None,
                 events=None):
        self.tree = tree
        self.cache = cache
        self.client = client
        self.system = system_prompt(tree)
        self.usage = usage if usage is not None else TokenUsage()
        self.events = events if events is not None else Events()

    async def select(self, component: Component) -> Selection:
        cached = self.cache.get(component.reference)
        if cached is not None:
            for choice, candidate in enumerate(self.tree.candidates):
                if candidate.htsno == cached["htsno"]:
                    selection = Selection(choice=choice, evidence=cached["evidence"])
                    if resolve(component, selection, self.tree.candidates).code is not None:
                        self.usage.record("classification", component.reference, MODEL)
                        return selection
                    break
        prompt = component_prompt(component)
        response = await self.usage.request(
            self.client.responses.parse, "classification", component.reference,
            model=MODEL,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            instructions=self.system,
            input=[{"role": "user", "content": prompt}],
            text_format=Selection,
        )
        if response.status != "completed" or response.output_parsed is None:
            raise RuntimeError(f"{component.reference}: no complete classification")
        selection = response.output_parsed
        classification = resolve(component, selection, self.tree.candidates)
        if classification.code is not None:
            self.cache.put(component.reference, classification.code, classification.evidence)
        return selection

    async def run(self, components: list[Component]) -> list[Classification]:
        rows = []
        for position, component in enumerate(components, 1):
            with self.events.action(
                "classification", reference=component.reference,
                position=position, total=len(components),
            ) as result:
                row = resolve(component, await self.select(component), self.tree.candidates)
                rows.append(row)
                result["classification"] = asdict(row)
        return rows


def write_classified(rows: list[Classification], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(Classification.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
