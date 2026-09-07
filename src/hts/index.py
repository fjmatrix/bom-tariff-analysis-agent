"""Parse an HTS JSON export into a queryable index.

Two jobs, and the second is the one that is easy to miss:

1. Give the model something to read. A row's own `description` is usually
   useless -- 28 of the 82 rows in the shipped file say "Other" -- so each row
   gets a `path` built from its whole lineage.
2. Give the rate back once the model has picked. 40 of the 48 candidates carry
   `general: ""` in the raw file, because a duty rate is set at the 8-digit
   legal line and the 10-digit statistical suffixes below it inherit. Read the
   field directly and 83% of the schedule computes a duty of zero.

A row with `superior: "true"` carries no code of its own; it exists to group
the rows beneath it ("Threaded articles:", "Lugnuts:", "Other:"). All 21 of them
in the shipped file are exactly the rows with an empty `htsno`. They hold the
discriminating language -- 7318.16.00.85 alone reads "Other", but under its
superior rows it reads "Nuts, not a lugnut, not stainless" -- so they belong in
every descendant's `path` and can never be a `parent`.

The file is already in depth-first pre-order, so this is not a tree traversal.
It is a linear scan that rebuilds the tree from the `indent` column -- the same
shape as parsing an indented outline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_TAG = re.compile(r"<[^>]+>")

PATH_SEP = " > "


@dataclass
class HtsRecord:
    htsno: str  # "" for superior rows -- see note below
    description: str  # markup stripped

    # Filled in during the scan, once the row's position is known.
    path: str = ""  # full lineage, superior rows included
    parent: str | None = None  # nearest ancestor WITH a code
    children: list[str] = field(default_factory=list)

    general: str = ""  # inherited
    special: str = ""  # inherited from the rate-bearing legal line
    rate_source: str = ""  # which code supplied it
    ad_valorem: float | None = None  # None only on grouping rows; see parse_rate

    @property
    def digits(self) -> int:
        return len(self.htsno.replace(".", ""))

    @property
    def is_terminal(self) -> bool:
        return bool(self.htsno) and not self.children

    @property
    def is_candidate(self) -> bool:
        """Selectable classification target.

        The digit floor keeps truncation artifacts out of the option list: 7319
        is terminal only because the shipped export stops there, not because
        sewing needles have no subdivisions.
        """
        return self.is_terminal and self.digits >= 8


def _clean(text: str | None) -> str:
    return _TAG.sub("", text or "").strip()


def parse_rate(general: str) -> float | None:
    """'Free' -> 0.0, '12.5%' -> 0.125, specific/compound -> None."""
    g = (general or "").strip()
    if not g:
        return None
    if g.lower() == "free":
        return 0.0
    m = re.fullmatch(r"([\d.]+)\s*%", g)
    return float(m.group(1)) / 100 if m else None




class HtsIndex:
    """Chapter-agnostic. Loads whatever the export contains."""

    def __init__(self, records: list[HtsRecord]):
        self.records = records
        self.by_code = {r.htsno: r for r in records if r.htsno}

    # -- construction --------------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "HtsIndex":
        with open(path) as fh:
            return cls.from_rows(json.load(fh))

    @classmethod
    def from_rows(cls, raw: list[dict]) -> "HtsIndex":
        """Scan the rows in order, keeping a stack of the lineage above you.

        `stack` is the path from the root down to the current row, exactly the
        way a recursive walk would hold it on the call stack. Here the recursion
        already happened -- when the schedule was flattened to a file -- so the
        stack is rebuilt by hand from the indent column.
        """
        records: list[HtsRecord] = []
        stack: list[HtsRecord] = []

        for row in raw:
            indent = int(row["indent"])

            while len(stack) > indent:  # pop out of branches we have left
                stack.pop()
            if len(stack) != indent:
                raise ValueError(
                    f"{row.get('htsno') or row.get('description')!r}: indent jumped to "
                    f"{indent} from depth {len(stack)}. Rows must not skip a level."
                )

            rec = HtsRecord(
                htsno=row.get("htsno") or "",
                description=_clean(row.get("description")),
                general=(row.get("general") or "").strip(),
                special=_clean(row.get("special")),
            )
            stack.append(rec)

            # Everything on the stack, root first, me last. Superior rows are
            # in here on purpose -- "Other" means nothing without them.
            rec.path = PATH_SEP.join(r.description for r in stack)

            # Walking back up from me, the first row that has a code. Superior
            # rows are skipped: they have text but no address to point at.
            rec.parent = next((r.htsno for r in reversed(stack[:-1]) if r.htsno), None)

            records.append(rec)

        index = cls(records)
        index._link_children()
        index._resolve_rates()
        return index

    def _link_children(self) -> None:
        for rec in self.records:
            if rec.htsno and rec.parent:
                self.by_code[rec.parent].children.append(rec.htsno)

    def _resolve_rates(self) -> None:
        """Climb the parent chain to the first row that states a rate."""
        for rec in self.records:
            if not rec.htsno:
                continue
            node: HtsRecord | None = rec
            while node is not None and not node.general:
                node = self.by_code.get(node.parent) if node.parent else None
            if node is not None:
                rec.general = node.general
                rec.special = rec.special or node.special
                rec.rate_source = node.rate_source or node.htsno
                rec.ad_valorem = parse_rate(node.general)

    # -- lookups -------------------------------------------------------------

    def get(self, code: str) -> HtsRecord | None:
        return self.by_code.get(code)

    def children_of(self, code: str) -> list[HtsRecord]:
        rec = self.by_code.get(code)
        return [self.by_code[c] for c in rec.children] if rec else []

    @property
    def candidates(self) -> list[HtsRecord]:
        return [r for r in self.records if r.is_candidate]

    def stats(self) -> dict:
        return {
            "rows": len(self.records),
            "candidates": len(self.candidates),
            "unresolved_rates": [r.htsno for r in self.candidates if r.ad_valorem is None],
            "truncated_terminals": [
                r.htsno for r in self.records if r.is_terminal and not r.is_candidate
            ],
        }


def main() -> None:
    from src.config import HTS_JSON

    index = HtsIndex.load(HTS_JSON)
    stats = index.stats()
    width = max(len(k) for k in stats)
    for key, value in stats.items():
        print(f"{key:<{width}}  {value}")
    print()
    for rec in index.candidates[:5]:
        print(f"{rec.htsno}  {rec.general:<6} (from {rec.rate_source})  {rec.path}")


if __name__ == "__main__":
    main()
