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
| 3 | `hts/render.py` — numbered tree, every row, rates withheld | todo |
| 4 | `agent/loop.py` — select, validate evidence, three branches, cache | todo |
| 5 | `eval/` — golden set, arms A/B/C | todo |
| 6-9 | DataWeb, effective rate, exposure, recommendations, CLI | todo |

## File structure

```
htsdata.json                 input: HTS export (heading 7318, 82 rows)
EVOM V1.0 priced.csv         input: priced BOM (216 rows)

src/
  config.py                  input paths
                             for the audit trail
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
eval/                 [M6]   golden set + arms A/B/C
out/                         components.csv, classified.csv, exposure.csv,
                             audit.jsonl, dataweb_cache.json   (gitignored)
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
