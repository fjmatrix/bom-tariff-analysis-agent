

from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

HTS_JSON = ROOT / "htsdata.json"
BOM_CSV = ROOT / "BOM.csv"
OUT_DIR = ROOT / "out"
