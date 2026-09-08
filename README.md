# BOM Tariff Exposure Agent

![BOM tariff analysis showing sourcing opportunities and the completed workflow](docs/assets/cover.png)

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![OpenAI](https://img.shields.io/badge/LLM-OpenAI-412991?logo=openai&logoColor=white)](https://platform.openai.com/)
[![Data: USITC DataWeb](https://img.shields.io/badge/Data-USITC%20DataWeb-1f6feb)](https://dataweb.usitc.gov/)

Connect your product’s BOM and purchase costs with tariff rates and import data
to quantify cost pressure, pinpoint exposed parts, and identify sourcing actions
that protect margin. Compare origins, estimate savings, and set purchase-price
targets in a clear decision brief.

## Workflow

```mermaid
%%{init: {"flowchart": {"wrappingWidth": 340}} }%%
flowchart TB
    IN[/"`BOM.csv
    quantities · unit prices · origins`"/]
    S1["`**1 · Classify**
    flatten the BOM, identify HTS codes for purchased parts`"]
    S2["`**2 · Enrich**
    look up tariff rates and the top 5 import origins per code`"]
    S3["`**3 · Evaluate & recommend**
    compare duty, savings, and break-even prices; rank actions`"]
    OUT[/"`Decision brief
    exposure · opportunities · actions`"/]

    IN --> S1 --> S2 --> S3 --> OUT

    classDef step stroke:#4c8eda,stroke-width:1.5px
    classDef io stroke-dasharray:4 3
    class S1,S2,S3 step
    class IN,OUT io
```

Import origins are ranked by U.S. customs value in USD over the last 12 complete
months. Python calculates duty and savings; the model classifies parts and writes
the brief.

## Run

Requires Python 3.11+, the project dependencies, and `OPENAI_API_KEY` and
`DATAWEB_API_KEY` in `.env` or your shell. With the existing environment:

```bash
.venv/bin/python -m src.run --bom examples/two_parts.csv --out out/two_parts
```

Defaults: `--bom BOM.csv`, `--out out`. For the interactive terminal, install the
optional TUI extra and launch:

```bash
uv pip install --python .venv/bin/python -e '.[tui]'
.venv/bin/python -m src.tui --bom examples/two_parts.csv --out out/two_parts
```

## Input and outputs

BOM CSV columns (see [example](examples/two_parts.csv)):

```text
level,component_reference,component_name,component_quantity,parent_bom_reference,has_child_bom,unit_price_usd,country_of_origin
```

Quantities are per finished product. Repeated references must share a unit price
and origin; the assembly root must reconcile with total purchased-part value.

- **Decision:** `brief.md` and `brief_data.json` — exposure, coverage, sourcing opportunities, and actions.
- **Comparisons:** `scenarios.jsonl` and `trade_countries.jsonl` — duty by origin and import rankings.
- **Trace:** `components.csv`, `classified.csv`, and `token_usage.json` — parts, classifications, and API usage.

## Scope

The bundled `htsdata.json` covers heading **7318**. Estimates use General and
supported Special rates, assuming program eligibility; Chapter 99, Column 2,
and additional duties are excluded. Unsupported classifications or rates are
flagged for review.

Amounts are USD per finished product, assuming parts are imported separately at
unchanged BOM prices. Savings and quote ceilings exclude freight, tooling,
switching costs, and unmodeled duties; they are estimates, not supplier offers
or verified total import duties.
