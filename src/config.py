"""Every constant that moves a number, in one place.

Anything here gets printed into out/audit.jsonl alongside the results. An
assumption you can point at is defensible; one buried in a function is not.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HTS_JSON = ROOT / "htsdata.json"
BOM_CSV = ROOT / "EVOM V1.0 priced.csv"
OUT_DIR = ROOT / "out"

# --- Stage 1: roll-up -------------------------------------------------------
# The BOM's root row price. Sum of leaf extended costs must equal this exactly;
# see bom/flatten.py for why (component_quantity is already absolute).
BOM_ROOT_REFERENCE = "M00653"

# --- Stage 3b: effective rate ----------------------------------------------
# Placeholders until Stage 3 lands. Declared here so nothing downstream invents
# its own.
EUR_USD = 1.08
SECTION_301_RATE = 0.25
DATAWEB_PERIOD = "2024"


def constants() -> dict:
    """Snapshot for the audit trail."""
    return {
        "EUR_USD": EUR_USD,
        "SECTION_301_RATE": SECTION_301_RATE,
        "DATAWEB_PERIOD": DATAWEB_PERIOD,
        "hts_source": HTS_JSON.name,
        "bom_source": BOM_CSV.name,
    }
