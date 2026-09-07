"""Flatten the BOM tree to purchased components and roll up cost.

The one thing to get right: `component_quantity` is ALREADY ABSOLUTE. It counts
pieces per finished product, not per parent unit. The seven subassemblies with
qty > 1 have children whose quantities are already multiplied through -- M00696
(Nema23 Motor) is EUR 28.63 at qty 4, and its children sum to EUR 114.52.

Propagating ancestor multipliers double-counts those subtrees and inflates the
BOM from EUR 1,348.83 to EUR 1,735.99. So:

    effective_qty     = component_quantity
    extended_cost_eur = component_quantity * unit_price_eur

The invariant that holds everywhere, and the one worth asserting, is that a
parent's extended cost equals the sum of its children's extended costs. Leaf
extended costs therefore sum to the root's extended cost exactly. `reconcile()`
checks it; if a future BOM export changes convention, that assertion fires
before any duty number is computed on top of it.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

CENT = 0.005  # reconciliation tolerance, EUR


@dataclass
class BomRow:
    line: int  # position in the file; the only stable identity for a node
    level: int
    reference: str
    name: str
    quantity: float
    parent_reference: str
    has_child_bom: bool
    unit_price_eur: float
    parent_line: int | None = None
    assembly_path: tuple[str, ...] = ()
    country_of_origin: str = ""

    @property
    def extended_cost_eur(self) -> float:
        return self.quantity * self.unit_price_eur


@dataclass
class Component:
    """One purchased part, aggregated across every place it appears."""

    reference: str
    name: str
    quantity: float
    unit_price_eur: float
    occurrences: int
    assembly_paths: list[tuple[str, ...]] = field(default_factory=list)
    country_of_origin: str = ""

    @property
    def extended_cost_eur(self) -> float:
        return self.quantity * self.unit_price_eur

    @property
    def context(self) -> str:
        """Assembly context for the classifier prompt.

        'Lock Nut DIN985 M4' inside 'Z Axis Assembly' is a better prompt than
        the bare name. Deepest occurrence wins; ties break on the first seen.
        """
        if not self.assembly_paths:
            return ""
        return " > ".join(max(self.assembly_paths, key=len))


class ValidationError(AssertionError):
    pass

# Indentation is the sole source of hierarchy.
class Bom:
    def __init__(self, rows: list[BomRow]):
        self.rows = rows

    @classmethod
    def load(cls, path: str | Path) -> "Bom":
        rows: list[BomRow] = []
        stack: dict[int, int] = {}  # level -> line index
        with open(path, newline="") as fh:
            for line, raw in enumerate(csv.DictReader(fh)):
                level = int(raw["level"])
                row = BomRow(
                    line=line,
                    level=level,
                    reference=raw["component_reference"].strip(),
                    name=raw["component_name"].strip(),
                    quantity=float(raw["component_quantity"]),
                    parent_reference=raw["parent_bom_reference"].strip(),
                    has_child_bom=raw["has_child_bom"].strip() == "True",
                    unit_price_eur=float(raw["unit_price_eur"]),
                    country_of_origin=raw.get("country_of_origin", "").strip().upper(),
                )
                for lvl in [k for k in stack if k >= level]:
                    del stack[lvl]
                row.parent_line = stack.get(level - 1)
                row.assembly_path = tuple(
                    rows[stack[k]].name for k in sorted(stack)
                )
                rows.append(row)
                stack[level] = line
        return cls(rows)

    # -- structure -----------------------------------------------------------

    def children(self, line: int) -> list[BomRow]:
        return [r for r in self.rows if r.parent_line == line]

    @property
    def root(self) -> BomRow:
        return self.rows[0]

    @property
    def leaves(self) -> list[BomRow]:
        return [r for r in self.rows if not r.has_child_bom]

    # -- reconciliation ------------------------------------------------------

    def reconcile(self) -> dict:
        """Leaves must sum to the root, to the cent.

        The one check that guards a number the pipeline computes. If a future
        export switches to per-parent quantities, every duty figure downstream
        is wrong by 29% and this is what says so. Everything else that used to
        live here was forensic, not preventive -- see scripts/diagnose_bom.py.
        """
        leaf_total = sum(r.extended_cost_eur for r in self.leaves)
        root_total = self.root.extended_cost_eur
        if abs(leaf_total - root_total) > CENT:
            raise ValidationError(
                f"leaf sum {leaf_total:,.2f} != root {root_total:,.2f} "
                f"(delta {leaf_total - root_total:+,.2f}). Quantity convention "
                f"changed? Run scripts/diagnose_bom.py to localize it."
            )
        return {
            "rows": len(self.rows),
            "leaves": len(self.leaves),
            "subassemblies": sum(1 for r in self.rows if r.has_child_bom),
            "total_eur": round(leaf_total, 2),
        }

    # -- roll-up -------------------------------------------------------------

    def components(self) -> list[Component]:
        """Aggregate leaves by reference. 11 references recur across assemblies.

        The per-reference price check runs before reconcile() on purpose: both
        would fire on an inconsistent price, and the specific diagnosis is the
        useful one.
        """
        grouped: dict[str, list[BomRow]] = defaultdict(list)
        for row in self.leaves:
            grouped[row.reference].append(row)

        out: list[Component] = []
        for reference, rows in grouped.items():
            prices = {round(r.unit_price_eur, 6) for r in rows}
            if len(prices) > 1:
                raise ValidationError(
                    f"{reference}: inconsistent unit price across occurrences {sorted(prices)}"
                )
            origins = {r.country_of_origin for r in rows}
            if len(origins) > 1:
                raise ValidationError(
                    f"{reference}: inconsistent country of origin across occurrences"
                )
            out.append(
                Component(
                    reference=reference,
                    name=rows[0].name,
                    quantity=sum(r.quantity for r in rows),
                    unit_price_eur=rows[0].unit_price_eur,
                    occurrences=len(rows),
                    assembly_paths=[r.assembly_path for r in rows],
                    country_of_origin=rows[0].country_of_origin,
                )
            )
        self.reconcile()
        out.sort(key=lambda c: c.extended_cost_eur, reverse=True)
        return out


def write_components(components: list[Component], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "component_reference",
                "component_name",
                "quantity",
                "unit_price_eur",
                "extended_cost_eur",
                "occurrences",
                "assembly_context",
                "country_of_origin",
            ]
        )
        for c in components:
            writer.writerow(
                [
                    c.reference,
                    c.name,
                    f"{c.quantity:g}",
                    f"{c.unit_price_eur:.2f}",
                    f"{c.extended_cost_eur:.2f}",
                    c.occurrences,
                    c.context,
                    c.country_of_origin,
                ]
            )


def main() -> None:
    from src.config import BOM_CSV, OUT_DIR

    bom = Bom.load(BOM_CSV)
    stats = bom.reconcile()
    components = bom.components()

    width = max(len(k) for k in stats)
    for key, value in stats.items():
        print(f"{key:<{width}}  {value}")
    print(f"{'unique_components':<{width}}  {len(components)}")
    print(f"{'recurring_refs':<{width}}  {sum(1 for c in components if c.occurrences > 1)}")

    out = OUT_DIR / "components.csv"
    write_components(components, out)
    print(f"\nwrote {out}\n")
    print(f"{'EUR':>9}  {'qty':>6}  component")
    for c in components[:10]:
        print(f"{c.extended_cost_eur:9.2f}  {c.quantity:6.0f}  {c.name}")


if __name__ == "__main__":
    main()
