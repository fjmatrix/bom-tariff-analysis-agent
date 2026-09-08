# BOM Tariff Exposure Agent

Classify purchased parts in a priced BOM, calculate HTS duty by origin country,
and write a decision brief and compact JSONL comparisons.

The model receives all three tools on each turn and chooses a tool or a final answer.
Python executes the selected function and returns its result to the model.
Classification must finish before country discovery, and country discovery must
finish before duty calculation. The brief is accepted only after calculation
succeeds. Invalid calls return feedback, repeated calls reuse completed work,
and the conversation is limited to eight model turns.

OpenAI and DataWeb requests use async clients. Python callers await `run(...)`;
the command-line entry points manage the event loop. Requests remain sequential.

The tools are:

- `classify_bom()`: one structured model selection per uncached distinct part.
- `find_top_import_countries()`: query DataWeb for leading U.S. import origins
  for each distinct classified HTS code over the last 12 complete months.
- `calculate_duty_scenarios()`: deterministic HTS rate selection, arithmetic,
  and country comparisons grouped by part reference.

Classification and country discovery retain their results and write their output
files, returning only `{"status":"success"}` to the model after completion.

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
classifications from the shared SQLite database at `.cache/classification.sqlite3`,
independently of the output directory. It stores only `reference`, `htsno`, and
`evidence`, keyed by part reference. Description, assembly context, model, and
prompt changes do not invalidate entries. Cached codes and evidence are checked
against the current HTS tree; unsupported entries are refreshed. Only validated
classifications are cached. Old JSON selection caches are no longer used.

## Interactive terminal

Textual is an optional presentation layer over the same analysis runner. Install
the optional extra in the existing environment:

```bash
uv pip install --python .venv/bin/python -e '.[tui]'
```

Then launch the interface with the same BOM, country-count, and output arguments:

```bash
.venv/bin/python -m src.tui \
  --bom examples/two_parts.csv \
  --top-countries 5 \
  --out out/two_parts
```

The Parts tab starts with the ten largest purchased parts by BOM value and shows
their cumulative share. Search or select **All parts** to explore the whole BOM;
every part is analyzed regardless of the view. Select a row for assembly context,
HTS evidence, trade rankings, and origin comparisons. Duty and savings sorts
become meaningful after calculation. The adjacent workflow shows real model and
tool activity, cache hits, per-part classification, per-HTS lookup progress,
elapsed time, and reported token counts. A shared HTS lookup updates every related
part. The compact layout stacks activity below results in narrow terminals.

**Opportunities** shows modeled savings and quote ceilings from `brief_data` as
soon as calculation finishes. **Needs review** collects unresolved classifications,
rates, and country lookup errors. **Brief** displays the generated narrative.
Coverage and assumptions remain visible. The original output files are still
written to the displayed output directory, and calculated results remain visible
if the subsequent brief request fails.

Use Tab / Shift+Tab to move focus, arrow keys to navigate tables, **c** to cancel
analysis while retaining the screen, and **q** or **Ctrl+C** to cancel and quit.
Letter shortcuts apply when a text input is not consuming them. Cancellation
finalizes `token_usage.json` as `cancelled` and retains already-written artifacts
and cached classifications; it does not implement resumable runs.

Python callers can observe the workflow without importing Textual:

```python
events = []
brief = await run(bom_path, top_countries, out_dir, on_event=events.append)
```

Each `WorkflowEvent` has a per-run sequence, UTC timestamp, name, status, action
ID, and data. Actions emit `started` followed by `completed`, `failed`,
`cancelled`, or `rejected`. Lookup skips and usage/results are standalone events.
Observers are synchronous, should return promptly, and must treat event data as
read-only. Observer exceptions are logged without interrupting analysis. Passing
an observer replaces console rendering; omitting it retains plain terminal output.
Custom injected country-discovery functions keep their existing two-argument
interface and report tool-level progress; the built-in discovery also emits
per-HTS events. Network calls remain sequential.

Focused UI verification with fake services (requires the optional extra):

```bash
.venv/bin/python -m pytest tests/test_events.py tests/test_tui.py tests/test_run.py -q
```

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

## Decision brief

`brief.md` uses four sections:

1. **Summary:** known duty per finished product, duty as a percentage of total
   BOM cost, modeled savings, and the potentially addressable share of known duty.
   Coverage is shown by purchased-part count and BOM value.
2. **Product Exposure:** the top 10 purchased parts by current duty, with origin,
   duty per finished product, and duty as a percentage of the entire BOM cost.
   The input describes one finished product, so this is a part breakdown.
3. **Sourcing Opportunities:** the best positive-saving origin per part, current
   and alternative duty, savings, current purchase price, and the break-even
   alternative purchase price per piece. Tied options count only once in savings.
4. **Recommended Actions:** prioritized quote and verification steps, plus
   unresolved exposure, unsupported alternatives, trade period, and lookup errors.

Python calculates the figures and rankings in `brief_data.json` before the model
writes the narrative. Total BOM cost includes unresolved parts; known duty does
not treat their missing exposure as zero. With no supported parts, exposure is
unavailable. With zero known duty, the addressable percentage is not applicable.
No identified opportunity means no positive saving in the supported, discovered
alternatives, rather than proof that no sourcing opportunity exists.

Annual production volume and an exchange rate are absent from the input, so the
brief reports USD per finished product and marks annual exposure unavailable.
Annual modeled exposure would be unit exposure times annual finished-product
volume; BOM quantities are not annual volumes.

The break-even purchase price is:

```text
current per-piece price × (1 + current duty rate) / (1 + alternative duty rate)
```

This matches purchase price plus modeled duty. It excludes freight, tooling,
qualification, switching costs, and unmodeled duties. It is a quote ceiling,
not an available supplier price. For the demo washer, a supported CA scenario
saves $0.58 per finished product at the current $0.50 purchase price and has a
$0.529 break-even price per piece. A JP scenario with 2.9% duty instead has a
$0.514091 break-even price. Actual alternatives depend on country discovery.

## Token usage

Every classification request and agent turn prints input, cached input, output,
reasoning, and total tokens. Final terminal totals separate classification and
agent usage and include their combined run total. Local classification-cache hits
are logged with zero tokens and do not count as API calls.

`token_usage.json` is updated after every event and finalized on success or
failure. It contains chronological events (part reference or turn number, model,
response ID, status, and counts), stage totals, and run totals. Each invocation
starts a fresh trace, including when reusing an output directory. No prompts or
credentials are included in the trace.

Counts come from response usage, including incomplete responses. Cached input
and reasoning are subsets of input and output and are not added to the total
again, following [OpenAI's accounting guidance](https://developers.openai.com/cookbook/articles/per_run_spending_controller_responses_api#limits-and-other-costs).
An exception or response without usage is recorded with null counts and increases
`calls_without_usage`; aggregate counts sum only reported usage. SDK-internal
retries with no returned usage cannot be measured by this trace, so it is not
an account billing reconciliation.

## Input and outputs

The BOM uses this CSV format:

```text
level,component_reference,component_name,component_quantity,parent_bom_reference,has_child_bom,unit_price_usd,country_of_origin
```

Quantities are already per finished product. Repeated references are aggregated
and must have the same unit price and origin. The assembly root must reconcile
with the total purchased-part value.

Outputs are `brief.md`, `brief_data.json`, `token_usage.json`, `scenarios.jsonl`,
`trade_countries.jsonl`, `components.csv`, and `classified.csv`.
Tool calls, results, and token counts print in the terminal.
Both comparison tools return objects. Scenarios are keyed by part reference;
country rankings are keyed by HTS code. Each JSONL line contains one such entry,
so merging the line objects reconstructs the corresponding comparison object. Numeric values remain
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
metadata. The agent's calculation tool returns these under `scenarios`, alongside
the computed `brief_data` used to write the brief. The latter includes the shared
`trade_period` (null when no lookups ran) and `lookup_errors` keyed by HTS code.

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
| `src/brief.py` | Summary, coverage, exposure rankings, and sourcing price ceilings |
| `src/usage.py` | Per-request token trace, stage totals, and run totals |
| `src/run.py` | Model-selected tool loop and brief |

Focused verification, without live API calls:

```bash
.venv/bin/python -m pytest tests/test_brief.py tests/test_usage.py \
  tests/test_run.py tests/test_agent_loop.py -q
```
