"""Where this run finds its inputs: the two files, and the key.

The rate constants that used to sit here (EUR_USD, SECTION_301_RATE,
DATAWEB_PERIOD) had no reader and were placeholders for Stage 3b. They come
back when Stage 3b does, with the code that reads them.

`.env` is loaded here rather than at each entry point because every entry point
already imports this module for its paths. The path is pinned to ROOT so it
resolves the same from any working directory, and `override=False` leaves an
explicitly exported key winning over the file.
"""

from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

HTS_JSON = ROOT / "htsdata.json"
BOM_CSV = ROOT / "EVOM V1.0 priced.csv"
OUT_DIR = ROOT / "out"
