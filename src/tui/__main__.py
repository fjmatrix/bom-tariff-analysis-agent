"""Observe BOM analysis in an interactive terminal."""

import argparse
from pathlib import Path

from src.config import BOM_CSV, OUT_DIR


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bom", type=Path, default=BOM_CSV, help="Priced BOM CSV")
    parser.add_argument("--top-countries", type=int, default=5,
                        help="Leading import origins per HTS code (default: 5)")
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="Output directory")
    args = parser.parse_args()
    if args.top_countries < 1:
        parser.error("--top-countries must be at least 1")
    try:
        from src.tui.app import BomApp
    except ModuleNotFoundError as error:
        if error.name != "textual":
            raise
        parser.error("Textual is optional. Install it with: uv pip install --python .venv/bin/python -e '.[tui]'")
    BomApp(args.bom, args.top_countries, args.out).run()


if __name__ == "__main__":
    main()
