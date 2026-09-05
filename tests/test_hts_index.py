"""M1 assertions. These guard the numbers every later stage is built on."""

import pytest

from src.config import HTS_JSON
from src.hts.index import HtsIndex, parse_rate, parse_special_programs


@pytest.fixture(scope="module")
def index() -> HtsIndex:
    return HtsIndex.load(HTS_JSON)


# -- invariants (must hold for ANY export) -----------------------------------

def test_every_candidate_resolves_a_rate(index):
    """A candidate with no ad valorem rate silently zeroes a duty line."""
    unresolved = [r.htsno for r in index.candidates if r.ad_valorem is None]
    assert unresolved == []


def test_every_coded_record_resolves_a_parent(index):
    orphans = [
        r.htsno
        for r in index.records
        if r.htsno and r.parent is not None and r.parent not in index.by_code
    ]
    assert orphans == []


def test_candidates_are_terminal_and_specific(index):
    """The definition of a selectable target, restated as a check."""
    for r in index.candidates:
        assert r.htsno and not r.children and r.digits >= 8


# -- superior rows: in the path, out of the options --------------------------

def test_superior_rows_are_not_candidates(index):
    assert all(r.htsno for r in index.candidates)


def test_superior_text_appears_in_child_paths(index):
    """'Socket screws:' has no code but is the whole discriminator."""
    rec = index.get("7318.15.60.40")
    assert "Socket screws:" in rec.path
    assert "Threaded articles:" in rec.path


def test_markup_is_stripped(index):
    """Descriptions carry <il> tags around dimensions."""
    assert all("<" not in r.path for r in index.records)
    assert "6 mm" in index.get("7318.13.00.30").description


# -- rate inheritance --------------------------------------------------------

def test_ten_digit_line_inherits_from_coded_ancestor(index):
    rec = index.get("7318.16.00.85")
    assert rec.general == "Free"
    assert rec.ad_valorem == 0.0
    assert rec.rate_source == "7318.16.00"


def test_rate_source_records_self_when_rate_is_local(index):
    rec = index.get("7318.11.00.00")
    assert rec.rate_source == "7318.11.00.00"
    assert rec.ad_valorem == pytest.approx(0.125)


def test_non_contiguous_indent_still_links(index):
    """7318.16.00 is at indent 2, its children at indent 4."""
    kids = index.children_of("7318.16.00")
    assert {k.htsno for k in kids} == {
        "7318.16.00.15",
        "7318.16.00.30",
        "7318.16.00.45",
        "7318.16.00.60",
        "7318.16.00.85",
    }


# -- the cases the agent loop is built around --------------------------------

def test_din912_competing_readings_carry_different_rates(index):
    """M2 of the plan: machine screw (Free) vs socket screw (6.2% / 8.5%)."""
    assert index.get("7318.15.40.00").ad_valorem == 0.0
    assert index.get("7318.15.60.40").ad_valorem == pytest.approx(0.062)
    assert index.get("7318.15.80.45").ad_valorem == pytest.approx(0.085)


def test_din985_attribute_gap_is_rate_invariant(index):
    """Stainless vs other under Nuts: unresolvable from the name, and free
    either way. The loop must decide, not escalate."""
    rates = {r.ad_valorem for r in index.children_of("7318.16.00")}
    assert rates == {0.0}


def test_rate_is_fixed_at_eight_digits(index):
    """The rule that replaced get_siblings.

    A duty rate is set at the 8-digit legal line, so two candidates sharing the
    first 8 digits cannot differ in rate. That makes the loop's rate-bearing
    check a string comparison instead of a tool call.
    """
    groups: dict[str, set[float | None]] = {}
    for r in index.candidates:
        groups.setdefault(r.htsno[:10], set()).add(r.ad_valorem)
    assert all(len(rates) == 1 for rates in groups.values())


# -- parsers -----------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("Free", 0.0), ("12.5%", 0.125), ("2.8%", 0.028), ("", None), ("2.2 cents/kg", None)],
)
def test_parse_rate(raw, expected):
    assert parse_rate(raw) == (pytest.approx(expected) if expected is not None else None)


def test_parse_special_programs_handles_whitespace():
    got = parse_special_programs("Free (A+,AU,B,BH,CL,CO,D,E,IL, JO,KR,MA, OM,P,PA,PE,S,SG)")
    assert "A+" in got and "JO" in got and "OM" in got
    assert len(got) == 18


def test_parse_special_programs_empty():
    assert parse_special_programs("") == frozenset()


# -- shipped fixture shape ---------------------------------------------------
# These pin htsdata.json (heading 7318). They are EXPECTED to fail when the full
# USITC export is loaded -- update the numbers then, do not delete the test. Kept
# apart from the invariants above so a red bar here means "the data changed",
# not "the code broke".

def test_shipped_fixture_shape(index):
    stats = index.stats()
    assert stats["rows"] == 82
    assert stats["candidates"] == 48
    assert stats["truncated_terminals"] == ["7319"]  # export stops below 7319
