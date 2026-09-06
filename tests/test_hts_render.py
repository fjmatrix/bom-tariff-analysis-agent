"""M3 assertions. The tree is the entire prompt; these pin what is in it."""

import pytest

from src.config import HTS_JSON
from src.hts.index import HtsIndex
from src.hts.render import render


@pytest.fixture(scope="module")
def index() -> HtsIndex:
    return HtsIndex.load(HTS_JSON)


@pytest.fixture(scope="module")
def tree(index):
    return render(index)


def test_numbering_is_the_candidate_list(index, tree):
    """`candidates[n]` is the whole mapping from the model's integer to a code.

    If the walk ever numbered a row the index does not call a candidate, every
    choice would resolve to the wrong record.
    """
    assert tree.candidates == index.candidates


def test_every_row_is_rendered(index, tree):
    assert len(tree.text.splitlines()) == len(index.records)


def test_no_codes_and_no_rates_leak_into_the_text(index, tree):
    """The model cannot emit a code it never saw, or be steered by a rate."""
    assert "7318" not in tree.text
    assert "Free" not in tree.text
    assert "%" not in tree.text


def test_non_candidate_coded_rows_are_still_rendered(tree):
    """7318.15.60 vs .80 is the 6.2% / 8.5% split. Neither is selectable, and
    dropping them would hide the distinction that moves the rate."""
    lines = [ln.strip() for ln in tree.text.splitlines()]
    assert lines.count("Having shanks or threads with a diameter of less than 6 mm") > 1
    assert lines.count("Having shanks or threads with a diameter of 6 mm or more") > 1


def test_indentation_tracks_path_depth(index, tree):
    """Indentation is the only thing carrying hierarchy into the prompt."""
    for line, rec in zip(tree.text.splitlines(), index.records):
        assert len(line) - len(line.lstrip()) == 2 * rec.path.count(" > ")


def test_tree_is_far_smaller_than_flat_paths(index, tree):
    """The reason for the format. Flat paths restate mid-path text on every
    line; measured on the shipped 48 that is 5x the prompt."""
    flat = sum(len(r.path) + 1 for r in index.candidates)
    assert len(tree.text) < flat / 4


# -- shipped fixture shape ---------------------------------------------------
# Numbers pinned to htsdata.json, like the tail of test_hts_index.py.

def test_headline_cases_keep_their_numbers(tree):
    """The DIN912 readings the loop is built around, by the numbers the plan
    quotes. A renumbering here silently rewrites every cached selection."""
    codes = [r.htsno for r in tree.candidates]
    assert codes[19] == "7318.15.40.00"  # machine screws, Free
    assert codes[25] == "7318.15.60.40"  # socket screws, shank < 6 mm, 6.2%
    assert codes[30] == "7318.15.80.45"  # socket screws, shank >= 6 mm, 8.5%
    assert codes[39] == "7318.16.00.60"  # DIN985 negative control, both Free
    assert codes[40] == "7318.16.00.85"
