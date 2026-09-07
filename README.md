# BOM Tariff Exposure Agent

Classify purchased parts in a priced BOM, calculate HTS duty by origin country,
and write a short decision brief and compact JSONL comparisons.

The model receives all three tools on each turn and chooses a tool or a final answer.
Python executes the selected function and returns its result to the model.
Classification must finish before country discovery, and country discovery must
finish before duty calculation. The brief is accepted only after calculation
succeeds. Invalid calls return feedback, repeated calls reuse completed work,
and the conversation is limited to eight model turns.

The tools are:

- `classify_bom()`: one structured model selection per uncached distinct part.
- `find_top_import_countries()`: query DataWeb for leading U.S. import origins
  for each distinct classified HTS code over the last 12 complete months.
- `calculate_duty_scenarios()`: deterministic HTS rate selection, arithmetic,
  and country comparisons grouped by part reference.

## Run

Use the existing `uv` environment and `OPENAI_API_KEY` in the repository's `.env`
or your shell. The DataWeb step also requires `DATAWEB_API_KEY`. From the
repository root:

```bash
.venv/bin/python -m src.run \
  --bom examples/two_parts.csv \
  --top-countries 5 \
  --out out/two_parts
```

`--top-countries` controls how many origins DataWeb retains per HTS code and
defaults to 5. Users no longer need to choose countries. Each part's current
origin is always included even when it is outside the top results. `--bom`
defaults to `BOM.csv`; `--out` defaults to `out`.

Rates are read from `htsdata.json`; no rates CSV is used. The old `--rates`
option has been removed.

On a cold cache, the two-part demo normally uses two classification calls and
four outer model calls: classify, discover countries, calculate, then summarize.
Recovery or repeated tool requests can add turns. Later runs reuse cached
classifications. Changing
a part description, assembly context, model, or classification prompt/tree
invalidates that selection. The cache stores selections by a hash of the model
and complete prompts. Legacy or malformed entries are refreshed on demand.

## HTS rate selection

`src/duty/rates.py` implements:

```python
if qualifies_for_special_program(code, country):
    rate = matching_special_rate
else:
    rate = general_rate
```

The index inherits General and Special rates from the rate-bearing legal line.
The selector handles Free and percentage rates, including multiple Special
entries such as `Free (S,KR) 3.1% (JP)`. Missing, specific, or compound rates are
reported as unresolved instead of treated as zero.

Country/program mappings cover AU, BH, CL, CO, IL, JO, JP, KR, MA, OM, PA, PE,
and SG; CA/MX map to S, and CR/DO/SV/GT/HN/NI map to P. The mapped symbol must
also appear on the selected HTS code. All other countries use General for now.

Special-program eligibility is a **scenario assumption**: country membership and
a listed symbol stand in for eligibility. The BOM does not establish shipment
rules of origin or an importer claim. Those conditions are required for actual
special treatment, as explained by the
[USITC](https://www.usitc.gov/faq/question/what_do_all_columns_mean.htm).

Group preferences (A/A*/A+, B, D, E), S+, Chapter 99, Column 2, and additional
duties are not implemented. Extend `duty_rate()` when adding those rules.
Results use the bundled HTS snapshot and do not represent verified total import
duties or live supplier offers.

## Country discovery

DataWeb is queried once per distinct classified HTS code. The request covers
the 12 complete calendar months before the run month and breaks U.S. imports for
consumption out by origin. "Volume" means consumption customs value in USD; this
is comparable across tariff lines whose physical quantity units may differ.
The two partial-year columns returned for a range crossing New Year are summed
before countries are ranked.

Results are keyed by HTS code and written to `trade_countries.jsonl`, one code
per line, with the exact period and customs values keyed by ISO-2 country.
Countries appear in descending customs-value order; names are omitted. The official
[DataWeb API guide](https://www.usitc.gov/applications/dataweb/api/dataweb_query_api.html)
documents the report endpoint and result layout. DataWeb describes imports for
consumption as merchandise cleared through U.S. customs.

Rankings change as each new complete month enters the window. For example, a
run on September 7, 2026 uses September 2025 through August 2026. A lookup
failure is stored as `error` on the affected HTS entry; affected parts still
receive a current-origin calculation but no discovered alternatives.

## Demo calculation

The demo contains a $20 stainless steel nut and a $10 helical spring lock washer,
both currently from CN. Current-origin duty from the bundled HTS is $0.58:

| reference | country | duty_usd | savings_usd |
|---|---|---:|---:|
| DEMO-NUT | CN | 0.00 | 0.00 |
| DEMO-WASHER | CN | 0.58 | 0.00 |

The nut's General rate is Free. The washer's General rate is 5.8%, its S special
rate is Free, and its JP special rate is 2.9%. The alternative rows and best
savings depend on the rolling DataWeb ranking. On September 7, 2026, JP was in
the washer's top five and produced $0.29 of modeled savings. These figures
replace the previous illustrative rates-CSV demo.

Amounts are USD per finished product. Parts are assumed imported separately,
using unchanged BOM purchase values as the duty base. Duty is rounded to cents
per part. Savings compare each scenario with the part's current origin.
The brief identifies the best alternative for each part.

## Input and outputs

The BOM uses this CSV format:

```text
level,component_reference,component_name,component_quantity,parent_bom_reference,has_child_bom,unit_price_usd,country_of_origin
```

Quantities are already per finished product. Repeated references are aggregated
and must have the same unit price and origin. The assembly root must reconcile
with the total purchased-part value.

Outputs are `brief.md`, `scenarios.jsonl`, `trade_countries.jsonl`, `components.csv`,
`classified.csv`, and `selection_cache.json`. Tool calls and results print in the terminal.
Both comparison tools return objects. Scenarios are keyed by part reference;
country rankings are keyed by HTS code. Each JSONL line contains one such entry,
so merging the line objects reconstructs the tool result. Numeric values remain
numbers, and empty results produce empty files.

Example `scenarios.jsonl` line:

```json
{"DEMO-WASHER":{"country_of_origin":"CN","countries":{"CA":{"duty_usd":0.0,"savings_usd":0.58},"CN":{"duty_usd":0.58,"savings_usd":0.0}}}}
```

Example `trade_countries.jsonl` line:

```json
{"7318210030":{"period_start":"09/2025","period_end":"08/2026","countries":{"CA":{"customs_value_usd":100}}}}
```

Scenarios contain only part entries, without repeated summary rankings or trade
metadata. The brief uses the calculated per-part duty and savings amounts.

The classifier returns only a candidate number (or null) and evidence. Python
validates the index and checks the evidence against the candidate's HTS path.
Unsupported or ambiguous parts can abstain. There are no runner-up comparisons,
confidence-based code demotions, or separate audit CLI.

Parts without a usable classification, origin, or supported baseline rate are
returned with a `reason` instead of country calculations. An unsupported
alternative rate adds a `reason` only to that country entry. The loaded HTS tree covers heading 7318,
so full-BOM results cover only that scope.

## Implementation

| File | Responsibility |
|---|---|
| `src/bom/flatten.py` | BOM roll-up, reconciliation, and origin |
| `src/hts/index.py`, `render.py` | HTS records, inherited rates, and candidate tree |
| `src/classify/classifier.py`, `prompts.py`, `cache.py` | Structured classification and caching |
| `src/duty/dataweb.py` | Trailing-12-month import-origin discovery |
| `src/duty/rates.py` | Country/program qualification and HTS rate selection |
| `src/duty/scenarios.py` | Per-part duty, savings, and JSONL |
| `src/run.py` | Model-selected tool loop and brief |

Focused verification, without live API calls:

```bash
.venv/bin/python -m pytest tests/test_agent_loop.py tests/test_dataweb.py \
  tests/test_duty_scenarios.py tests/test_run.py tests/test_bom_flatten.py -q
```
