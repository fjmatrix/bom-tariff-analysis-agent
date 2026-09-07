"""M2 assertions.

Reconciliation is the only check that runs in the pipeline; it is what caught
the quantity convention. The structural checks it used to carry now live in
scripts/diagnose_bom.py -- exercised at the bottom of this file so they do not
bitrot, but not on the critical path."""

import csv

import pytest

from src.bom.flatten import Bom, ValidationError
from src.config import BOM_CSV


@pytest.fixture(scope="module")
def bom() -> Bom:
    return Bom.load(BOM_CSV)


def test_leaves_reconcile_to_root(bom):
    """The one runtime guard: leaf extended cost sums to the root, to the cent."""
    stats = bom.reconcile()
    assert stats["total_usd"] == 1348.83
    assert bom.root.extended_cost_usd == pytest.approx(1348.83)


def test_quantities_are_absolute_not_per_parent(bom):
    """M00696 (Nema23 Motor) is qty 4; its motor child is ALSO qty 4, not 1.

    Propagating the parent multiplier would make it 16 and inflate the BOM to
    USD 1,735.99. This is the trap; this test is the tripwire.
    """
    parent = next(r for r in bom.rows if r.reference == "M00696")
    assert parent.quantity == 4
    child = next(c for c in bom.children(parent.line) if c.reference == "M00697")
    assert child.quantity == 4
    assert parent.extended_cost_usd == pytest.approx(
        sum(c.extended_cost_usd for c in bom.children(parent.line))
    )


def test_multiplier_propagation_would_break_reconciliation(bom):
    """The wrong implementation, run explicitly, so the delta is documented."""
    stack, wrong = {}, 0.0
    for row in bom.rows:
        mult = (stack.get(row.level - 1, 1.0) if row.level else 1.0) * row.quantity
        stack[row.level] = mult
        for lvl in [k for k in stack if k > row.level]:
            del stack[lvl]
        if not row.has_child_bom:
            wrong += mult * row.unit_price_usd
    assert wrong == pytest.approx(1735.99, abs=0.01)
    assert wrong != pytest.approx(1348.83, abs=0.01)


# -- roll-up -----------------------------------------------------------------

def test_component_aggregation(bom):
    components = bom.components()
    assert len(components) == 147
    assert sum(1 for c in components if c.occurrences > 1) == 11


def test_recurring_reference_sums_across_assemblies(bom):
    """M01697 (DIN912 M4x12) appears twice, at qty 6 and 17."""
    c = next(x for x in bom.components() if x.reference == "M01697")
    assert c.quantity == 23
    assert c.occurrences == 2


def test_components_reconcile_to_root(bom):
    total = sum(c.extended_cost_usd for c in bom.components())
    assert total == pytest.approx(1348.83, abs=0.01)


def test_din912_population(bom):
    """The headline classification case: 172 pcs, USD 15.22."""
    d912 = [c for c in bom.components() if "DIN912" in c.name]
    assert sum(c.quantity for c in d912) == 172
    assert sum(c.extended_cost_usd for c in d912) == pytest.approx(15.22, abs=0.01)


def test_assembly_context_is_populated(bom):
    c = next(x for x in bom.components() if x.reference == "M01697")
    assert c.context.startswith("EVO-M [M0 use] > ")
    assert "DIN912" in c.context


def test_origin_is_carried_through_aggregation_and_export(bom, tmp_path):
    from src.bom.flatten import write_components

    part = next(c for c in bom.components() if c.reference == "M01697")
    assert part.country_of_origin == "CN"
    path = tmp_path / "components.csv"
    write_components([part], path)
    with path.open(newline="") as fh:
        assert next(csv.DictReader(fh))["country_of_origin"] == "CN"


# -- the guards actually fire ------------------------------------------------

def _rows(overrides: dict[int, dict]) -> list[dict]:
    with open(BOM_CSV, newline="") as fh:
        rows = list(csv.DictReader(fh))
    for line, patch in overrides.items():
        rows[line].update(patch)
    return rows


def _write(rows: list[dict], path):
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_reconciliation_catches_a_changed_leaf(tmp_path):
    """Perturb one leaf price; the leaf sum no longer matches the root."""
    rows = _rows({})
    leaf = next(i for i, r in enumerate(rows) if r["has_child_bom"] == "False")
    rows[leaf]["unit_price_usd"] = "999.00"
    path = _write(rows, tmp_path / "bad.csv")
    with pytest.raises(ValidationError, match="leaf sum"):
        Bom.load(path).reconcile()


def test_aggregation_catches_inconsistent_unit_price(tmp_path):
    """M01697 appears twice; give the occurrences different prices.

    Not reconciliation -- this lives in components(), because summing quantity
    while silently taking one of two prices corrupts the extended cost.
    """
    rows = _rows({})
    first = next(i for i, r in enumerate(rows) if r["component_reference"] == "M01697")
    rows[first]["unit_price_usd"] = "0.99"
    path = _write(rows, tmp_path / "price.csv")
    with pytest.raises(ValidationError, match="inconsistent unit price"):
        Bom.load(path).components()


def test_aggregation_does_not_merge_different_origins(tmp_path):
    rows = _rows({})
    first = next(i for i, r in enumerate(rows) if r["component_reference"] == "M01697")
    rows[first]["country_of_origin"] = "TW"
    with pytest.raises(ValidationError, match="inconsistent country of origin"):
        Bom.load(_write(rows, tmp_path / "origins.csv")).components()


# -- the forensic checks still work (they are not in the pipeline) -----------

def test_diagnostics_clean_on_shipped_bom():
    from scripts.diagnose_bom import CHECKS

    bom = Bom.load(BOM_CSV)
    assert {name: check(bom) for name, check in CHECKS.items()} == {
        name: [] for name in CHECKS
    }
