

from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")

HTS_JSON = ROOT / "htsdata.json"
BLS_SERIES = ROOT / "ei.series"
BOM_CSV = ROOT / "BOM.csv"
OUT_DIR = ROOT / "out"

CLASSIFICATION_MODEL = "gpt-5.6-sol"
CLASSIFICATION_MAX_OUTPUT_TOKENS = 1000

AGENT_MODEL = "gpt-5.6-terra"
AGENT_MAX_OUTPUT_TOKENS = 9000
AGENT_MAX_TURNS = 8
