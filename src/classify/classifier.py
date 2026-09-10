"""Select one HTS candidate per component, validate it, and cache the answer."""

import csv
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import BaseModel

from src.classify.cache import ClassificationCache
from src.classify.prompts import component_prompt, system_prompt
from src.bom.flatten import Component
from src.config import CLASSIFICATION_MAX_OUTPUT_TOKENS, CLASSIFICATION_MODEL
from src.hts.render import CandidateTree
from src.usage import TokenUsage


class Selection(BaseModel):
    choice: int | None
    rationale: str


@dataclass
class Classification:
    reference: str
    name: str
    status: str
    reason: str
    code: str | None
    evidence: str
    rationale: str = ""


class Classifier:
    def __init__(self, tree: CandidateTree, cache: ClassificationCache, client, usage=None):
        self.tree = tree
        self.cache = cache
        self.client = client
        self.system = system_prompt(tree)
        self.usage = usage if usage is not None else TokenUsage()

    async def classify(self, component: Component) -> Classification:
        selection = None
        cached = self.cache.get(component.reference)
        if cached is not None:
            for choice, candidate in enumerate(self.tree.candidates):
                if candidate.htsno == cached["htsno"]:
                    selection = Selection(choice=choice, rationale=cached["rationale"])
                    break

        cache_hit = selection is not None
        if cache_hit:
            self.usage.record("classification", component.reference, CLASSIFICATION_MODEL)
        else:
            selection = await self._request_selection(component)

        classification = self._build_classification(component, selection)
        if not cache_hit and classification.code is not None:
            self.cache.put(component.reference, classification.code, classification.evidence,
                           classification.rationale)
        return classification

    async def _request_selection(self, component: Component) -> Selection:
        prompt = component_prompt(component)
        response = await self.usage.request(
            self.client.responses.parse, "classification", component.reference,
            model=CLASSIFICATION_MODEL,
            max_output_tokens=CLASSIFICATION_MAX_OUTPUT_TOKENS,
            instructions=self.system,
            input=[{"role": "user", "content": prompt}],
            text_format=Selection,
        )
        if response.status != "completed" or response.output_parsed is None:
            raise RuntimeError(f"{component.reference}: no complete classification")
        return response.output_parsed

    def _build_classification(self, component: Component, selection: Selection) -> Classification:
        code = None
        evidence = ""
        if selection.choice is None:
            status, reason = "unclassified", "no_supported_candidate"
        elif not 0 <= selection.choice < len(self.tree.candidates):
            status, reason = "needs_review", "index_out_of_range"
        else:
            chosen = self.tree.candidates[selection.choice]
            status, reason = "classified", "clean"
            code = chosen.htsno
            evidence = chosen.path
        return Classification(
            component.reference, component.name, status, reason, code, evidence,
            selection.rationale,
        )


def write_classified(rows: list[Classification], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(Classification.__dataclass_fields__))
        writer.writeheader()
        writer.writerows(asdict(row) for row in rows)
