"""Paths to the two input files. That is all this needs to be today.

The rate constants that used to sit here (EUR_USD, SECTION_301_RATE,
DATAWEB_PERIOD) had no reader and were placeholders for Stage 3b. They come
back when Stage 3b does, with the code that reads them.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HTS_JSON = ROOT / "htsdata.json"
BOM_CSV = ROOT / "EVOM V1.0 priced.csv"
OUT_DIR = ROOT / "out"
