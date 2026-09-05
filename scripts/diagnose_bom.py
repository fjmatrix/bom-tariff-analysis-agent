"""Forensic checks for when reconciliation fires.

These do not run in the pipeline. They detect nothing that Bom.reconcile()
misses in practice -- their value is *localization*: reconciliation says the
BOM is off by EUR 387, this says which seven subassemblies caused it. That is
how the quantity convention was found in the first place.

    .venv/bin/python -m scripts.diagnose_bom
"""

from __future__ import annotations

from collections import defaultdict

from src.bom.flatten import CENT, Bom
from src.config import BOM_CSV


def check_level_continuity(bom: Bom) -> list[str]:
    """A level jump orphans a row: the stack walk finds no parent for it."""
    out, prev = [], bom.rows[0].level
    if prev != 0:
        out.append(f"first row is level {prev}, expected 0")
    for row in bom.rows[1:]:
        if row.level > prev + 1:
            out.append(f"line {row.line}: level jumps {prev} -> {row.level}")
        prev = row.level
    return out


def check_parent_column(bom: Bom) -> list[str]:
    """The CSV encodes the tree twice -- `level` and `parent_bom_reference`.

    Nothing in the pipeline reads the column; the walk is authoritative. This
    is a cross-check on the assumption that `level` means what we think it
    means, worth running once against a new export and then forgetting.
    """
    out = []
    for row in bom.rows:
        if row.level == 0:
            continue
        walked = bom.rows[row.parent_line].reference if row.parent_line is not None else None
        if walked != row.parent_reference:
            out.append(
                f"line {row.line} ({row.reference}): column says "
                f"{row.parent_reference!r}, walk says {walked!r}"
            )
    return out


def check_cost_invariant(bom: Bom) -> list[str]:
    """Parent extended cost == sum of children extended costs, at every node.

    This is the localizer. When it fires on rows whose quantity is > 1, the
    export is using per-parent quantities rather than absolute ones.
    """
    out = []
    by_parent: dict[int, list] = defaultdict(list)
    for row in bom.rows:
        if row.parent_line is not None:
            by_parent[row.parent_line].append(row)
    for row in bom.rows:
        if not row.has_child_bom:
            continue
        kids = by_parent.get(row.line, [])
        if not kids:
            out.append(f"line {row.line} ({row.reference}): has_child_bom=True, no child rows")
            continue
        total = sum(k.extended_cost_eur for k in kids)
        if abs(total - row.extended_cost_eur) > CENT:
            out.append(
                f"line {row.line} ({row.reference}, qty {row.quantity:g}): extended "
                f"{row.extended_cost_eur:.2f} != sum(children) {total:.2f}"
            )
    return out


CHECKS = {
    "level continuity": check_level_continuity,
    "parent column vs tree walk": check_parent_column,
    "cost invariant per parent": check_cost_invariant,
}


def main() -> None:
    bom = Bom.load(BOM_CSV)
    leaf_total = sum(r.extended_cost_eur for r in bom.leaves)
    print(f"leaves {len(bom.leaves)}  sum EUR {leaf_total:,.2f}  root EUR {bom.root.extended_cost_eur:,.2f}\n")

    for name, check in CHECKS.items():
        problems = check(bom)
        print(f"{name:<28} {'OK' if not problems else f'{len(problems)} problem(s)'}")
        for problem in problems:
            print(f"    {problem}")


if __name__ == "__main__":
    main()
