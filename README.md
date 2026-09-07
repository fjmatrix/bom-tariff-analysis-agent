# BOM Tariff Exposure Agent

Takes a priced bill of materials, classifies each purchased component against
the US Harmonized Tariff Schedule, and ranks the lines by duty exposure.

A pipeline with **one agent embedded in it** — four deterministic stages, one
agent loop, one conditional fan-out. Named that way on purpose: calling the
whole thing "an agentic workflow" would not survive a reading of the code.

```
flatten + roll up  →  CLASSIFY (agent loop)  →  DataWeb (1 call/code)
   deterministic          the real thing         data-dependent
     ↓
duty $ = qty × unit_cost × eff_rate  →  margin exposure  →  rank
              arithmetic                  arithmetic        sort
     ↓
RECOMMEND top-N (conditional LLM fan-out)
```

Full design in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## Demo origin assumptions

`EVOM V1.0 priced.csv` includes `country_of_origin`, using ISO 3166-1 alpha-2
codes. **Every value is a synthetic demo assumption, not verified supplier or
shipment origin.** The descriptions cannot establish actual origin. Repeated
component references have the same assumed country.

The scenario assigns CN to generic electronics, fasteners and mechanical parts;
TW to hand tools; DE to aluminium profiles, PMMA sheet and Wago connectors; BE
to custom steel parts, custom motor/switch cables, the test plate and assembly
rows; GB to the Raspberry Pi; KR to the Samsung memory card; and SI to the CNC
controller and its license. These are scenario choices, not deductions from
brand headquarters or proof of preferential tariff eligibility. Raspberry Pi's
[UK manufacturing information](https://www.raspberrypi.com/news/explore-the-raspberry-pi-factory-floor-in-wales-uk/)
supports the plausibility of GB, but does not verify this particular BOM's unit.

The duty demo assumes components are imported separately for domestic assembly.
Use leaf rows to avoid double counting. BE on assembly rows is an assembly
scenario placeholder, not a legal origin determination for a finished machine.
The software license (M00368) has an assumed provider country only and needs
separate treatment before physical-goods duty calculations. The existing BOM
loader does not yet carry the origin column into its component objects.

## Scope

`htsdata.json` is **heading 7318 only** — 48 classifiable leaves. Of the 147
unique components, only the fasteners can honestly land there. The rest cannot:
aluminium profile is 7604, the T8 lead screw 8483, terminal blocks 8536, the
SMPS 8504. (147 is the count after dedup, which is what classification runs on;
the 162 figure elsewhere is leaf *rows*.)

The loader is chapter-agnostic; swapping in the full USITC export is a data
change, not a code change. Until then, anything outside loaded scope routes to
`out_of_scope` with a reason. It is never guessed.

Magnitude, stated up front: a loose name match on fasteners hits 33 components
worth **€48.79 of a €1,348.83 BOM — 3.6%**, so duty is on the order of €3. (The
match is €57.29 / 4.2% before removing the T8 lead screw and its nut, which are
8483.) The exact in-scope count comes out of eval arm A. The interesting number
is per-decision, not per-BOM — see the DIN912 case below.

## Status

| # | Milestone | State |
|---|---|---|
| 1 | `hts/index.py` — records, paths, inherited rates | **done** |
| 2 | `bom/flatten.py` — leaf extraction, roll-up, reconciliation | **done** |
| 3 | `hts/render.py` — numbered tree, every row, rates withheld | **done** |
| 4 | `agent/loop.py` — select, validate evidence, three branches, cache | **done** |
| 5 | `eval/` — golden set, arms A/B/C | todo |
| 6-9 | DataWeb, effective rate, exposure, recommendations, CLI | todo |

## File structure

```
htsdata.json                 input: HTS export (heading 7318, 82 rows)
EVOM V1.0 priced.csv         input: priced BOM (216 rows)

src/
  config.py                  input paths
  hts/
    index.py         [M1]    HtsIndex: parse -> HtsRecord, path strings,
                             inherited rates, parent/child links, lookups
    render.py        [M3]    numbered tree for the select prompt
  bom/
    flatten.py       [M2]    Bom: load, reconcile, roll up to Components
  agent/
    loop.py          [M4]    select -> validate -> branch -> terminate
    prompts.py       [M4]
    cache.py         [M4]    keyed by component_reference; deterministic replay
  duty/
    dataweb.py       [M7]    origin share per HTS10, cached + offline snapshot
    effective_rate.py [M8]   base rate + program/301 modifiers
    exposure.py      [M8]    duty $, margin share, switchable exposure, ranking
  recommend/
    fanout.py        [M9]    top-N only
  run.py             [M9]    CLI: the whole pipeline

scripts/
  diagnose_bom.py            forensic checks, run when reconcile() fires
tests/
  test_hts_index.py          invariants, then shipped-fixture shape (separated)
  test_bom_flatten.py        reconciliation, roll-up, the guards firing
  test_hts_render.py         numbering, every row present, nothing leaked
  test_agent_loop.py         every branch, on hand-built selections
eval/                 [M6]   golden set + arms A/B/C
out/                         components.csv, classified.csv, exposure.csv,
                             audit.jsonl, selection_cache.json,
                             dataweb_cache.json                 (gitignored)
```

## Workflow

Six stages. Only one of them is an agent.

**1. Flatten and roll up** — `Bom.load()` walks the 216 CSV rows depth-first,
keeping a stack keyed by `level`, and records each row's `parent_line` and
`assembly_path`. `leaves` are the 162 rows with `has_child_bom == False`.
`components()` groups those by `component_reference` into 147 unique parts,
summing quantity across the 11 that recur, checking unit price is consistent
per reference, and reconciling to the root. Deterministic; no model involved.
→ `out/components.csv`

**2. Classify** — the agent loop, one component at a time. The whole in-scope
candidate set goes into a single prompt with rates withheld; the model returns
a choice (or abstains), the phrase that decided it, and any attribute it could
not settle. **One LLM call per component, no tools** — the model reads a
numbered tree and returns an integer, so it never sees or emits an HTS code and
cannot invent one. Then deterministic validation — the evidence phrase must be a
literal substring of the chosen path — and one of three branches: confirm
out-of-scope; decide an unresolved attribute that cannot move the rate (same
first 8 digits); or escalate to `needs_review` when a genuine runner-up carries a
different rate. Every branch is Python.
→ `out/classified.csv`, `out/audit.jsonl`

**3. DataWeb** — one call per distinct HTS10 code (~15–25, not ~150), returning
import value by country of origin. Data-dependent on stage 2's output, but not
a decision. Cached to disk with a committed snapshot so the pipeline runs
offline. → `out/dataweb_cache.json`

**4. Effective rate** — `Σ(origin_share × rate)`, where the per-country rate is
Free if the origin matches a free program in the `special` column, otherwise the
general rate, plus Section 301 where it applies. Arithmetic.

**5. Exposure** — `duty = quantity × unit_price × EUR_USD × effective_rate`,
plus duty as a share of COGS and *switchable* exposure (the part of the duty an
origin change could actually remove). Rank descending. Arithmetic.
→ `out/exposure.csv`

**6. Recommend** — conditional LLM fan-out over the top N by exposure only.
Every recommendation cites an HTS code, the compared rate, and the $ delta, or
it is not emitted.

Stages 1, 3, 4 and 5 are deterministic. Stage 2 is the agent. Stage 6 is a
conditional invocation. That ratio is the honest description of the system.

## Run

For annual import customs value by country, set `DATAWEB_API_KEY` in `.env`:

```bash
.venv/bin/python -m src.duty.dataweb 7318.15.60 --year 2025
```

This calls DataWeb's `POST /api/v2/report2/runReport` and prints the report DTO
as JSON. It accepts 8- or 10-digit HTS codes, with or without dots. The fixed
report uses imports for consumption, countries displayed separately, and actual
USD customs values. The year defaults to 2025; no caching or share calculation
is included. Request format follows the [USITC API guide](https://www.usitc.gov/applications/dataweb/api/dataweb_query_api.html).

```bash
uv venv && uv pip install -e '.[dev]'
```

```bash
.venv/bin/python -m src.hts.index
```

```bash
.venv/bin/python -m src.bom.flatten
```

```bash
.venv/bin/python -m src.hts.render
```

One `5.6-terra` call per component. `--limit N` takes the N most expensive first; a
rerun is served from `out/selection_cache.json` and costs nothing.

```bash
.venv/bin/python -m src.agent.loop --limit 5
```

```bash
.venv/bin/python -m pytest -q
```

```bash
.venv/bin/python -m scripts.diagnose_bom
```

## What M1 and M2 establish

**The index (M1)** resolves 48 candidates, every one DataWeb-queryable with a
resolved ad valorem rate. Three things it handles that are quiet when wrong:

- **Rate inheritance.** 10-digit lines carry `general: ""`. `7318.16.00.85`
  inherits `Free` from `7318.16.00`, and `rate_source` records which ancestor
  supplied it.
- **Superior rows** (`superior: "true"`, empty `htsno` -- 21 of the 82). Pure
  grouping lines: `Threaded articles:`, `Lugnuts:`, `Socket screws:`. Nothing can
  be imported under them, but they carry the entire discriminator, so they go
  *in* the path string and *out* of the candidate list.
- **Indent is page layout, not code depth.** Uncoded superior rows consume levels,
  so a coded row sits 1-5 levels below its coded parent -- `7318.16.00` at indent
  2, its children at indent 4, with the superior row `Other:` at 3 between them. The
  traversal takes the deepest surviving coded ancestor rather than assuming the
  parent is one level up.

`7319` is a 4-digit terminal — the export stops there — so it is reported as a
truncated subtree rather than offered as a leaf.

**The roll-up (M2)** flattens 216 rows to 162 leaves and 147 unique components,
11 of which recur across assemblies.

The trap: **`component_quantity` is already absolute.** The seven subassemblies
with qty > 1 have children whose quantities are already multiplied through —
`M00696` (Nema23 Motor) is €28.63 at qty 4, and its motor child is *also* qty 4,
not 1. Propagating ancestor multipliers double-counts those subtrees and inflates
the BOM to €1,735.99. `test_multiplier_propagation_would_break_reconciliation`
runs the wrong implementation on purpose so the delta stays documented.

`Bom.reconcile()` is the only check that runs in the pipeline: leaves must sum
to the root at €1,348.83, to the cent. Five lines. It caught the convention, and
it fires if a future export changes it.

The structural checks that used to sit beside it — level continuity, the
`parent_bom_reference` cross-check, the per-parent cost invariant — moved to
`scripts/diagnose_bom.py`. They detect nothing reconciliation misses in
practice; their value is *localization*. Reconciliation says the BOM is off by
€387, the diagnostic says which seven subassemblies did it. Forensics, not a
guard.

## What M3 and M4 establish

**The tree (M3)** renders all 82 rows at 2,971 characters against 14,718 for the
flat candidate paths — 5× — because the tree shows the lineage once instead of
restating it on every line. Numbering is the candidate list itself, so
`candidates[n]` is the entire mapping from the model's answer to an HTS code.
Nothing else is in the text: no codes (the model cannot emit one it never saw)
and no rates (it cannot be steered by a `Free` sitting next to an `8.5%`).
`test_hts_render.py` asserts the absence of both, and pins `[19]`/`[25]`/`[30]`
to the DIN912 readings — a renumbering silently rewrites every cached selection.

**The loop (M4)** is one model call per component and five deterministic
branches. `resolve()` is pure, so all five are tested against hand-built
selections with no key and no network.

The measured thing worth knowing: **the rate-invariance suppression can only
escalate for 7 of the 48 candidates.** A rate is set at the 8-digit legal line,
and 41 candidates are 10-digit statistical suffixes hanging under one — their
siblings share the rate by construction, so an unresolved attribute there can
never move a number. Only the 7 that hang directly off the 4-digit heading have
siblings that are whole subheadings with rates of their own (coach screws 12.5%,
rivets Free, cotters 3.8%). That ratio is the branch's whole point, and it is
asserted in `test_agent_loop.py`, not just claimed here.

Low confidence does not escalate; it demotes the code to `rate_source`, the
8-digit line the rate came from. Origin-mix precision is lost, the rate stays
exact, and `selected_code` keeps what the model actually picked so the audit can
still show it. For the 8 candidates that state their own rate it is a no-op.

The cache stores the **selection**, not the classification. Re-running with
different branch logic — which is the entire difference between eval arms B and
C — calls nothing. A prompt fingerprint over the system prompt and model id
guards the one thing that must not be replayed: a cached `choice: 25` means a
different code once the tree it indexed changes.

## The classification cases this is built around

**DIN912 socket head cap screws — 172 pcs, €15.22, the largest fastener
population in the BOM** — have three defensible homes:

| Code | Text | Rate |
|---|---|---|
| `7318.15.40.00` | Machine screws ≥ 9.5 mm long **and** ≥ 3.2 mm diameter | **Free** |
| `7318.15.60.xx` | Other screws and bolts › shanks < 6 mm › Socket screws | **6.2%** |
| `7318.15.80.xx` | Other screws and bolts › shanks ≥ 6 mm › Socket screws | **8.5%** |

An M4x12 satisfies the machine-screw conditions literally (4 ≥ 3.2, 12 ≥ 9.5)
*and* is literally a socket screw. Both readings hold up against the text.
Resolving it means pulling both lines and comparing their conditions — a tool
call, not a retrieval fix. Duty on this family: **€1.15 as socket screws, €0.00
as machine screws.** One call, the entire fastener duty bill.

**DIN985 lock nuts** are the negative control: the name cannot settle stainless
vs other, but `7318.16.00.60` and `.85` are both Free. Correct behavior is to
decide and move on. Escalating a rate-invariant ambiguity costs a human and
changes no number.
