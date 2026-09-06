"""Render the index as the tree the model reads.

No search index and no shortlist: the whole candidate set goes into one prompt
and the model picks a number out of it.

**Indented tree, not flat paths.** Measured on the shipped 48 candidates: the
flat `path` strings are 14,718 chars, the indented tree is 2,742 -- 5.4x. Flat
paths repeat their mid-path text on every line; the tree shows the hierarchy
once instead of restating it.

**Every row, numbered only where selectable.** Superior rows carry the
discriminating language ("Socket screws:"), and coded non-candidate rows carry
the rate split -- `7318.15.60` (shanks < 6 mm) vs `7318.15.80` (>= 6 mm) is the
6.2% / 8.5% boundary. Drop either kind and the model cannot see the distinction
that moves the rate.

**No codes and no rates in the text.** The model returns an integer, so it can
neither emit an HTS code nor invent one, and it never saw a rate next to a
choice. `candidates[n]` maps the integer back to the record, which already
carries the code, the path, the rate and its source.
"""

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
