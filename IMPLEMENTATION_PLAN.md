# BOM Tariff Exposure — Implementation Plan

Pipeline with one agent embedded in it. Four deterministic stages, one agent loop,
one conditional fan-out. Named honestly so the code matches the claim.

```
flatten + roll up  →  CLASSIFY (agent loop)  →  DataWeb (1 call/code)
   deterministic          the real thing         data-dependent
     ↓
duty $ = qty x unit_cost x eff_rate  →  margin exposure  →  rank
              arithmetic                  arithmetic        sort
     ↓
RECOMMEND top-N (conditional LLM fan-out)
```

Retrieval is **LLM selection over materialized path strings**. No lexical index,
no embeddings, no scoring function.

---

## Scope gate (decide before writing code)

`htsdata.json` contains **heading 7318 only** — 48 ten-digit leaves. Of 162 BOM
leaves, roughly 40 can legitimately land there. The rest cannot: aluminium
profile is 7604, T8 lead screw 8483, terminal blocks 8536, SMPS 8504.

**Decision: build the candidate table chapter-agnostic, demo on 7318.**
Everything outside loaded scope routes to `out_of_scope` with a reason — never
guessed. Swapping in the full USITC export is a data change, not a code change.

Say this in the README. "40 classified, 122 out of loaded scope" reads as
judgment. Silently classifying a 360W power supply as a wood screw does not.

---

## Layout

```
htsdata.json                    # given (heading 7318)
EVOM V1.0 priced.csv            # given (216-row BOM)
src/
  hts/
    index.py          # parse -> records, path strings, inherited rates
    candidates.py     # materialized candidate table -> prompt blocks
    tools.py          # get_hts / compare_codes / get_children
  bom/
    flatten.py        # leaf extraction + roll-up
  agent/
    loop.py           # the agent loop
    prompts.py
    cache.py          # keyed by component_reference -> deterministic replay
  duty/
    dataweb.py        # origin share, cached, offline snapshot fallback
    effective_rate.py # base rate + program/301 modifiers
    exposure.py       # duty $, margin %, ranking
  recommend/
    fanout.py         # top-N only
  run.py              # CLI
eval/
  golden.csv          # ~25 hand-labeled lines, in-scope and out
  score.py            # three arms, see Evaluation
out/                  # components.csv, classified.csv, exposure.csv, audit.jsonl
```

Deps: `pandas`, `anthropic`, `httpx`, `pydantic`. Use `uv`. No `rank_bm25`.

---

## Stage 0 — Index the schedule (`src/hts/index.py`)

One pass, stack keyed by `indent`.

```python
@dataclass
class HtsRecord:
    htsno: str            # "" for superior rows (pure grouping lines)
    indent: int
    description: str      # <il> tags stripped
    path: str             # "Screws, bolts... > Threaded articles > Nuts > Other > Of stainless steel"
    parent: str | None    # nearest ancestor WITH an htsno
    children: list[str]
    general: str          # inherited
    rate_source: str      # which htsno the rate came from   <- audit
    ad_valorem: float | None   # 0.0 for "Free", None for specific/compound
    special: str
    units: list[str]
    is_leaf: bool         # 10-digit, or 8-digit with no children
```

Three things that are easy to get wrong and silently corrupt every number below:

1. **Rate inheritance.** 10-digit lines have `general == ""`. Walk up to the
   first non-empty `general`. Record `rate_source` so the audit can say
   `7318.16.00.85 inherited Free from 7318.16.00`.
2. **Superior rows.** Rows with `superior: "true"` and empty `htsno`
   ("Threaded articles:", "Socket screws:", "Other:") are **not** selectable
   targets, but they **must** appear in the path string — they carry all the
   discriminating language. In the path, out of the option list.
3. **Indent is not contiguous.** `7318.16.00` sits at indent 2, its children at
   indent 4. Use a dict-stack keyed by indent with pruning, never a list append.

Assertions to write now: 48 leaves, every leaf resolves a rate, every non-root
record resolves a parent, no `ad_valorem is None` in this file.

## Stage 0b — Candidate table (`src/hts/candidates.py`) — replaces retrieval

There is no search index. Build the path string once per record (Stage 0), store
it, and assemble prompts by lookup.

**Sizing decides the mode.** All 48 leaf paths for 7318 total ~6.1k tokens.

| Mode | When | Calls |
|---|---|---|
| **FLAT** — every in-scope leaf in one prompt | scope ≤ ~200 leaves | 1 |
| **DESCENT** — pick among children, level by level | full HTS (~13k lines) | 3-4 |

**v1 is FLAT.** 7318 fits in one call, so ship that. DESCENT is the scaling path
and the reason `get_children` exists at all; write the tool, exercise it in a
test, don't put it on the critical path.

**Prompt block format** — one numbered line per selectable leaf:

```
[17] 7318.16.00.60 | Screws, bolts, nuts ... > Threaded articles > Nuts > Other > Of stainless steel
[18] 7318.16.00.85 | Screws, bolts, nuts ... > Threaded articles > Nuts > Other > Other
```

**Hide the duty rates in the selection prompt.** The rates are in the index and
get attached after the code is chosen. A model that can see `Free` next to
`8.5%` has a thumb on the scale, and the resulting classification is not one
you can defend. This costs nothing and removes the whole objection.

**Elide the shared prefix.** Every path starts with the same 130-character
heading text. Strip the common prefix, state it once above the list.

## Stage 0c — Tools (`src/hts/tools.py`)

Under LLM-select the tool set changes shape. `search_hts` is gone — there is
nothing to search.

| Tool | Signature | Answers | v1? |
|---|---|---|---|
| `get_hts` | `(code)` | does this exist, full path, rate, rate_source | yes |
| `compare_codes` | `(codes: list[str])` | two candidates side by side, with conditions | yes |
| `get_children` | `(code)` | which leaf exactly | DESCENT only |

`get_siblings` has a different job than it did under lexical retrieval. It is no
longer a "wrong neighborhood" rescue — under FLAT select the model already saw
every neighborhood. It is now the **rate-bearing check**: given an attribute the
component name can't settle, do the alternatives actually differ in rate?

---

## Stage 1 — Flatten and roll up (`src/bom/flatten.py`, deterministic)

**`component_quantity` is already absolute — do not propagate ancestor
multipliers.** The 7 subassemblies with qty > 1 (`M00696` Nema23 ×4, `M00841` ×4,
`M01205` ×4, `M01677/78/79` ×2, `M00384` ×2) have children whose quantities are
already multiplied through: M00696 is €28.63 at qty 4, and its children sum to
€114.52. Multiplying again double-counts those subtrees and inflates the BOM
from €1,348.83 to €1,735.99.

```
effective_qty       = component_quantity
extended_cost_eur   = component_quantity x unit_price_eur
```

**Reconciliation assertion (keep this — it is what caught the above):** sum of
`has_child_bom == False` rows must equal the root `unit_price_eur`, 1348.83, to
the cent. It does.

Aggregate by `component_reference`, summing quantity across occurrences (11 refs
recur, e.g. `M01697` at 6 and 17). Assert `unit_price_eur` is consistent per ref;
fail loudly rather than averaging. Retain `assembly_path` per component and feed
it to the classifier — "Lock Nut DIN985 M4" inside "Z Axis Assembly" is a better
prompt than the bare name.

Output `out/components.csv`, ~150 unique rows.

---

## Stage 2 — Classify (`src/agent/loop.py`) — THE AGENT LOOP

**Step 1 — Select.** One call. Full in-scope candidate block, rates hidden.
Structured output:

```python
class Selection(BaseModel):
    choice: int | None          # None = abstain, nothing here fits
    evidence: str               # the literal phrase in the path that decided it
    unresolved: list[str]       # attributes the name cannot settle, e.g. ["material"]
    runner_up: int | None       # second plausible option, if genuinely close
    confidence: float
```

**Step 2 — Validate, deterministically, before any branch:**
- `choice` in range and resolves to a leaf via `get_hts`
- **`evidence` is a literal substring of the chosen path.** Cheap, and it catches
  a rationale the model invented rather than read. A well-formed code with
  fabricated justification is the failure mode that matters here.

**Step 3 — Branch. This is the agentic behavior.**

- **`choice is None` → out-of-scope confirmation.** Ask which chapter it belongs
  to instead, record it, terminate. No further tool calls. With 122 of 162 lines
  outside 7318, this is the single most-exercised path in the whole system, and a
  select prompt with no abstain option will confidently misclassify every one of
  them.

- **`unresolved` non-empty → is the ambiguity rate-bearing?** A duty rate is
  set at the 8-digit legal line, so two candidates sharing the first 8 digits
  **cannot** differ in rate. The check is a string comparison, not a tool call:
  - Same first 8 digits → decide, mark `attribute_unknown_rate_invariant`,
    **do not escalate**. `7318.16.00.60` (stainless) vs `.85` (other) are both
    Free. Sending that to a human costs money and changes no number.
  - Different 8-digit prefix → compare the rates; escalate only if they differ.

  A low-confidence 10-digit pick can also fall back to its 8-digit parent: you
  lose origin-mix precision at DataWeb, you keep the rate exactly right. That is
  a better failure mode than review, and it follows from the structure of the
  schedule rather than from inspecting the data.

- **`runner_up` set → `compare_codes([choice, runner_up])`,** which returns both
  full paths, rates, and the condition text. A second call adjudicates on the
  conditions. See the DIN912 case below.

**Step 4 — Terminate.** Resolved, or 3 tool calls spent → `needs_review`. Hard
cap; a loop without one is a bug.

**Cache by `component_reference`** — full trace, not just the answer. Reruns are
free and the demo is deterministic.

### The headline test case: DIN912 (replaces the lock-washer example)

The lock-washer failure was a *retrieval* failure — BM25 returning
`7318.21.00.30` at rank 1. Under FLAT LLM-select the model sees every leaf at
once, including `Nuts > Other`, so that failure mode is gone. **Do not carry
that story forward; its premise no longer exists.** The ambiguity that survives
is better, because it is a genuine reading-of-the-schedule question rather than
a retrieval artifact:

DIN912 socket head cap screws — **172 pcs, €15.22, the largest fastener
population in this BOM** — have three defensible homes:

| Code | Text | Rate |
|---|---|---|
| `7318.15.40.00` | Machine screws 9.5 mm or more in length **and** 3.2 mm or more in diameter | **Free** |
| `7318.15.60.xx` | Other screws and bolts > shanks < 6 mm > Socket screws | **6.2%** |
| `7318.15.80.xx` | Other screws and bolts > shanks ≥ 6 mm > Socket screws | **8.5%** |

An M4x12 satisfies the machine-screw conditions literally (4 ≥ 3.2, 12 ≥ 9.5)
and is also literally a socket screw. Both readings are supportable from the
text. Resolving it means pulling both lines and comparing their *conditions* —
`compare_codes`, i.e. the loop.

The 6mm threshold is separately rate-bearing and is decidable from the part
name: M6+ (103 pcs, €8.95) → 8.5%; M4/M5 (69 pcs, €6.27) → 6.2%. The model must
parse `M6x16` → 6 mm diameter → the ≥6 mm branch. A dimension extraction that
moves the rate 2.3 points, verifiable against a hand label.

Duty on this one component family: **€1.15 as socket screws, €0.00 as machine
screws.** One classification call, the entire fastener duty bill.

Pair it with DIN985 as the negative control: attribute unresolved (stainless?),
both candidates Free, correct behavior is **decide and move on**, not escalate.

---

## Stage 3 — DataWeb (`src/duty/dataweb.py`) — one call per distinct code

Data-dependent, not a decision. ~15-25 distinct codes after classification.
Import value by country of origin, most recent full year, per HTS10 →
`{country: share}`.

- **Cache to `out/dataweb_cache.json` and commit a snapshot.** The demo must run
  offline; a live API is a liability in an interview.
- On failure: snapshot; if no snapshot, mark `origin_unknown` and apply the
  general rate at 100% — never silently drop the line.

## Stage 3b — Effective rate (`src/duty/effective_rate.py`)

```
effective_rate = SUM over origins of ( share_c x rate_c )
rate_c = 0.0                  if c matches a program in `special`
       = general_ad_valorem   otherwise
       + SECTION_301_RATE     if c == China and the code is listed
```

Every constant in one `config.py`, printed into the audit output: `EUR_USD`,
`SECTION_301_RATE`, DataWeb period. Assumptions you can point at beat
assumptions you have to defend. Flag `ad_valorem is None` as `needs_manual` —
none occur in 7318, but the full export has them.

---

## Stages 4-5 — Duty and exposure (`src/duty/exposure.py`, arithmetic)

```
customs_value_usd  = component_quantity x unit_price_eur x EUR_USD
duty_usd           = customs_value_usd x effective_rate
duty_share_of_cogs = duty_usd / total_customs_value
```

Also **switchable exposure**: `customs_value x (effective_rate −
best_available_origin_rate)`. Separates "big line, unavoidable duty" from "big
line, paying 8.5% for no reason." Only the second is actionable.

Rank by `duty_usd` desc. State the assumption: BOM unit price proxies customs
value (real transaction value differs — freight, assists, first sale).

**Magnitude, stated up front:** 7318-plausible fasteners are €57.29 of a
€1,348.83 BOM — 3.3%, duty on the order of €3. The exposure ranking is real
arithmetic on small numbers until the full schedule is loaded. Frame the demo as
*the 3.3% of the BOM where classification is hardest*, and show the €1.15 DIN912
swing as the per-decision stake. Do not present €3 as a finding.

---

## Stage 6 — Recommendations (`src/recommend/fanout.py`) — top-N only

Conditional LLM invocation, `N = 10`. Grounded, not generic:

- **Origin shift** — name the alternate country from the DataWeb mix and the $ delta.
- **Tariff engineering** — where a sibling line carries a materially lower rate
  and the difference is a real product attribute, surface it as a question for a
  licensed broker, not as advice. The DIN912 machine-screw reading is exactly
  this shape.
- **Valuation** — flag first-sale only when the origin mix implies multi-tier supply.

Every recommendation cites the HTS code, the compared rate, and the $ delta, or
it is not emitted.

---

## Audit trail (cross-cutting — do not defer)

`out/audit.jsonl`, one object per component: chosen code, every tool call and
result, the `evidence` phrase and its substring check, `rate_source` htsno,
origin mix and whether it was live or snapshot, every constant used. Costs
almost nothing written as you go; it is what makes the system reviewable.

---

## Evaluation (`eval/`)

~25 hand-labeled lines — critically, **including out-of-scope ones**, since
that's 75% of the real input.

| Arm | Description |
|---|---|
| A | FLAT select, no abstain option, no loop |
| B | FLAT select + abstain |
| C | Full loop (abstain + rate-bearing check + compare_codes) |

Metrics: top-1 accuracy on in-scope lines; **out-of-scope precision/recall**;
total duty error in EUR; mean LLM calls per component.

The expected story: A→B is a large jump driven entirely by abstention. B→C is
smaller and concentrated in the rate-bearing cases — which is the honest result,
and more interesting than a uniform win.

**The FlightCat contrast, restated.** The old justification was that first-pass
retrieval failed. That argument dies with BM25 — say so rather than carry it. The
loop earns its place here on three different grounds: abstention over a candidate
set that is 75% wrong-by-construction, escalating only ambiguities that move the
rate, and adjudicating competing readings of the schedule text. Those are
decisions, not retrieval repair. Arm A vs C is the measurement that shows it.

---

## Milestones

| # | Deliverable | Est. |
|---|---|---|
| 1 | `hts/index.py` + assertions (48 leaves, rates resolve, paths correct) | **done** |
| 2 | `bom/flatten.py` + reconcile to 1348.83 | **done** |
| 3 | `hts/candidates.py` — path strings, prefix elision, rates hidden | 1h |
| 4 | `hts/tools.py` — four tools, JSON contracts | 1.5h |
| 5 | `agent/loop.py` — select, validate, three branches, cap, cache | 4h |
| 6 | `eval/` — golden set incl. out-of-scope, arms A/B/C | 2.5h |
| 7 | `duty/dataweb.py` + snapshot fallback | 2h |
| 8 | `duty/effective_rate.py` + `exposure.py` | 1.5h |
| 9 | `recommend/fanout.py` top-N | 1.5h |
| 10 | `run.py`, README with scope statement and eval table | 1.5h |

Milestones 1-6 are the project. 7-10 make it a deliverable.

**Build 3 before 5, and run arm A first.** Watching a no-abstain flat select
confidently classify the SMPS tells you which branch actually carries the weight.
Design the loop from that, not from imagination.
