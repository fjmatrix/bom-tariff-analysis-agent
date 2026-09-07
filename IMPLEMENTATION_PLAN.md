# BOM Tariff Exposure — Implementation

Deliverable: classify a priced BOM, calculate HTS duty for current and alternative
origins, and summarize part duty costs and potential origin savings.

## Model-directed workflow

All three tools are available on every Responses API turn with `tool_choice="auto"`
and parallel calls disabled. The model chooses the next action from tool
descriptions, instructions, and prior results.

1. `classify_bom()` classifies each distinct purchased part.
2. `find_top_import_countries()` queries DataWeb for leading origins per HTS code.
3. `calculate_duty_scenarios()` applies HTS rates and calculates scenario results.
4. The model writes the brief after all tools have succeeded.

Each dependent tool reports a prerequisite error if called out of order.
Unknown tools and invalid arguments return feedback. Premature final responses
receive a completion reminder. Repeated valid tool calls reuse results. Eight
turns cap the conversation, and incomplete model responses fail the run.

## Minimal classifier

`Selection` contains only `choice: int | None` and `evidence: str`. The model
reads the existing numbered HTS tree and may abstain when the description does
not support a candidate. Python checks the candidate index and evidence.

The classifier keeps caching and CSV output. Legacy runner-up logic, confidence
fallbacks, rate-based ambiguity suppression, and the separate audit/CLI flow
have been removed. Selections are cached by a hash of the model and full classification prompts;
legacy and malformed entries are refreshed on demand.

## HTS country rates

The index retains and inherits the Special field alongside General.
`src/duty/rates.py` selects a listed special rate when the country maps to its
program symbol; otherwise it selects General. Rates are Free or percentages.
Unsupported rates are unresolved.

For now, country/program membership assumes that origin and claim requirements
are satisfied. Explicit mappings cover individual agreements, CA/MX through S,
and DR-CAFTA countries through P. Group preferences, S+, Chapter 99, Column 2,
and additional rules remain outside the current calculation. Future rule work
belongs in `duty_rate()`.

There is no rates CSV or user-supplied country list. `--top-countries` defaults
to five. DataWeb ranks origins separately for each exact classified HTS code by
U.S. imports-for-consumption customs value over the last 12 complete months.
The current origin is always included even when it falls outside the ranking.

The rolling window is expressed as an exact start and end month. DataWeb returns
one customs-value column per partial year when the window crosses New Year; the
workflow sums those columns, maps DataWeb country names to ISO-2 codes using its
country endpoint, and writes `trade_countries.jsonl`. Per-code request failures are
recorded and leave that code with a current-origin scenario only.

## Outputs and verification

Outputs: `brief.md`, `scenarios.jsonl`, `trade_countries.jsonl`, `components.csv`,
`classified.csv`, and `selection_cache.json`. Scenario objects are keyed by part
reference and contain the current origin and country-keyed duty/savings in USD.
Country ranking objects are keyed by HTS code, with the period and country-keyed
customs values. Each JSONL line contains one top-level entry. Unresolved reasons
and lookup errors stay on the affected entries; scenario totals and duplicate
trade metadata are omitted.

The bundled two-part demo has $0.58 current-origin duty. Alternative results
depend on the rolling country ranking. These are Column 1 snapshot calculations,
with unchanged purchase values and separately imported components.

Focused tests cover the agent conversation and recovery, classification and
cache invalidation, HTS inheritance and special-rate selection, exclusions,
rounding, and per-part origin savings. They use model stubs and make no live API
calls. Run commands and limitations are documented in README.md.
