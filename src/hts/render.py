

from __future__ import annotations

from dataclasses import dataclass

from src.hts.index import PATH_SEP, HtsIndex, HtsRecord

@dataclass(frozen=True)
class CandidateTree:
    text: str
    candidates: list[HtsRecord]  # position IS the number shown in `text`


def render(index: HtsIndex) -> CandidateTree:
    """Walk the records in file order; number the selectable ones as they pass.

    Depth comes out of the path rather than a stored field -- `path` was joined
    from the lineage stack, so its separator count is the row's depth.
    """
    lines: list[str] = []
    candidates: list[HtsRecord] = []

    for rec in index.records:
        indent = "  " * rec.path.count(PATH_SEP)
        if rec.is_candidate:
            lines.append(f"{indent}[{len(candidates)}] {rec.description}")
            candidates.append(rec)
        else:
            lines.append(f"{indent}{rec.description}")

    return CandidateTree(text="\n".join(lines), candidates=candidates)


def main() -> None:
    from src.config import HTS_JSON

    tree = render(HtsIndex.load(HTS_JSON))
    print(tree.text)
    print()
    print(f"{len(tree.candidates)} selectable, {len(tree.text)} chars")


if __name__ == "__main__":
    main()
