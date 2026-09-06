"""Throwaway: one component, one real call, everything printed.

Exists to answer "does the OpenAI path work end to end", which the unit tests
cannot answer because they stub the client. Deliberately outside `loop.py`:
it bypasses the cache so every run is a live call, which is the opposite of
what the loop wants and exactly what a smoke run wants.

Delete this file once M4 is settled.

    .venv/bin/python -m scripts.smoke
"""

from __future__ import annotations

import json
from dataclasses import asdict

from src.agent.loop import MAX_OUTPUT_TOKENS, MODEL, Selection, resolve
from src.agent.prompts import component_prompt, system_prompt
from src.bom.flatten import Bom
from src.config import BOM_CSV, HTS_JSON
from src.hts.index import HtsIndex
from src.hts.render import render


def rule(label: str) -> None:
    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")


def main() -> None:
    index = HtsIndex.load(HTS_JSON)
    tree = render(index)
    component = Bom.load(BOM_CSV).components()[0]  # sorted by cost; [0] is dearest

    rule("TREE (the whole of what the model reads)")
    print(tree.text)
    print(f"\n{len(tree.candidates)} selectable, {len(tree.text)} chars")

    rule("USER MESSAGE")
    message = component_prompt(component)
    print(message)

    import openai

    response = openai.OpenAI().responses.parse(
        model=MODEL,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        instructions=system_prompt(tree),
        input=[{"role": "user", "content": message}],
        text_format=Selection,
    )

    rule("RAW RESPONSE")
    print(response.model_dump_json(indent=2))

    rule("RESOLVED (what the loop would have written)")
    print(json.dumps(asdict(resolve(component, response.output_parsed, index, tree.candidates)), indent=2))


if __name__ == "__main__":
    main()
