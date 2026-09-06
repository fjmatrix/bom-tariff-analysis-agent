# BOM Tariff Exposure — Implementation Plan

Four deterministic stages, one agent loop, one conditional fan-out.

```
flatten + roll up  →  CLASSIFY (agent loop)  →  DataWeb (1 call/code)
   deterministic          the real thing         data-dependent
     ↓
duty $ = qty x unit_cost x eff_rate  →  margin exposure  →  rank  →  RECOMMEND top-N
              arithmetic                  arithmetic       sort     conditional LLM
```

Retrieval is **LLM selection over the whole candidate set**. No lexical index, no
embeddings, no scoring function.

---

## Scope gate

`htsdata.json` is **heading 7318 only** — 48 ten-digit leaves. The BOM has 147
unique components; most cannot land in 7318: aluminium profile is 7604, T8 lead
screw 8483, terminal blocks 8536, SMPS 8504.

**Build the candidate table chapter-agnostic, demo on 7318.** Everything outside
loaded scope routes to `out_of_scope` with a reason — never guessed. Swapping in
the full USITC export is a data change, not a code change.

Say this in the README. "N classified, the rest out of loaded scope" reads as
judgment. Silently classifying a 360W power supply as a wood screw does not.

**Denominator is 147 unique components, not 162 leaf rows.** Classification runs
after dedup; quote the number classification actually operates on.

**Magnitude, measured.** A loose name match (`DIN|screw|bolt|nut|washer|…`) hits
33 components worth **€48.79 of €1,348.83 — 3.6%**, so duty is on the order of
€3. (The match is €57.29 / 4.2% before removing the T8 lead screw and its nut,
which are 8483.) The exact in-scope count comes out of eval arm A, not from a
guess written here. Frame the demo as *the 3.6% of the BOM where classification
is hardest* and show the €1.15 DIN912 swing as the per-decision stake. **Do not
present €3 as a finding.**

---

## Layout

```
src/
  hts/index.py            # done — parse, path strings, inherited rates
  hts/render.py           # numbered tree for the prompt   (M3)
  bom/flatten.py          # done — leaf extraction + roll-up
  agent/loop.py           # the agent loop                 (M4)
  agent/prompts.py, cache.py
  duty/dataweb.py         # origin share, cached snapshot  (M6)
  duty/effective_rate.py, exposure.py                      (M7)
  recommend/fanout.py     # top-N only                     (M8)
  run.py                                                   (M9)
eval/golden.csv, score.py
out/                      # components.csv, classified.csv, exposure.csv, audit.jsonl
```

Deps: `pandas`, `anthropic`, `httpx`, `pydantic`. Use `uv`.

---

## Stage 0 — Index the schedule — **done**

`src/hts/index.py`. One linear scan, stack keyed by `indent`. Three things that
would silently corrupt every number below, all now asserted in tests:

1. **Rate inheritance.** 40 of 48 candidates have `general == ""`; the rate is
   set at the 8-digit legal line. `rate_source` records which code supplied it.
2. **Superior rows** (empty `htsno`) are never selectable but must appear in
   every descendant's `path` — they carry the discriminating language.
3. **Indent is not contiguous.** Rebuild from the indent column, don't append.

Verified against the shipped file: 82 rows, 48 candidates, all 10-digit, every
one resolves a rate. `ad_valorem is None` occurs only on the four grouping rows
(`7318`, `7318.14`, `7318.15`, `7319`), which `is_candidate` already excludes.

## Stage 0b — Render the index for the model (M3)

No search index, no shortlist. The whole in-scope candidate set goes into one
prompt and the model picks.

**Indented tree, not flat paths.** Measured on the real 48: flat paths 14,995
chars, common-prefix elided 7,555, indented tree **2,750** — a 5.4× win. Flat
paths repeat their mid-path text on every line; the tree shows the hierarchy
instead of restating it.

```
  Other screws and bolts, whether or not with their nuts or washers:
    Other:
      Having shanks or threads with a diameter of less than 6 mm
        Socket screws:
          [25] Of stainless steel
          [26] Other
```

The tradeoff is real — flat gives each option as a self-contained string, the
tree makes the model compose lineage from indentation. It is a one-line format
swap, so **eval arm A settles it**, not argument.

**Number the options; do not show the codes.** The model returns `{"choice": 26}`,
mapped back through `candidates[26]`. It cannot return a code, so it cannot
invent one. Stricter and cheaper than validating a returned code after the fact.

**Withhold the duty rates.** They attach after the choice. A model that sees
`Free` next to `8.5%` has a thumb on the scale. It costs nothing to omit the
column, and "the model never saw the rates" is a complete answer to the obvious
objection.

**Render every row, number only the candidates.** Not just superior rows —
coded non-candidate rows carry rate-bearing text too:

```
7318.15.60   Having shanks or threads with a diameter of less than 6 mm
7318.15.80   Having shanks or threads with a diameter of 6 mm or more
```

That is the 6.2% / 8.5% split. Drop those rows and the model cannot see the
distinction that moves the rate.

**Include `Component.context`** — the assembly chain, often a stronger signal
than the line name: `Lock Nut DIN985 M4` sits in a bag named `EVO - DIN985 Lock
Nut Bag - M4`.

## Stage 0c — No tools

**The model emits no function calls.** Not `tools=[]` as an oversight — there is
nothing for a tool to do.

The model never sees or returns an HTS code. It reads a numbered tree and returns
an integer; `candidates[n]` is the record, already carrying `path`, `general`,
`ad_valorem` and `rate_source`. Any `get_hts(code)` would return what the loop
already holds, and the model could not call it anyway — it has no code to pass.

- `search_hts` died with lexical retrieval — there is nothing to search.
- `get_hts` has no caller, on either side.
- `get_children` belongs to a DESCENT mode for the full ~13k-line schedule. Not v1.
- `get_siblings`' one job, the rate-bearing check, is a string comparison in Stage 2.
- `compare_codes` is cut with the second LLM call — see Stage 2.

One LLM call per component. Everything else is Python.

---

## Stage 1 — Flatten and roll up — **done**

`src/bom/flatten.py`. **`component_quantity` is already absolute — do not
propagate ancestor multipliers.** The 7 subassemblies with qty > 1 have children
whose quantities are already multiplied through; multiplying again inflates the
BOM from €1,348.83 to €1,735.99.

```
extended_cost_eur = component_quantity x unit_price_eur
```

Reconciliation assertion (this is what caught the above): leaf extended costs sum
to the root, €1,348.83, to the cent. Aggregate by `component_reference` — 11 refs
recur. Assert consistent `unit_price_eur` per ref; fail loudly rather than
average.

Verified: 216 rows → 162 leaves → **147 unique components**, €1,348.83.

---

## Stage 2 — Classify (`src/agent/loop.py`) — THE AGENT LOOP (M4)

**One LLM call per component.** No tools, no second call. What makes it a loop is
what Python does with the answer.

**Step 1 — Select.** Full candidate tree, rates hidden.

```python
class Selection(BaseModel):
    choice: int | None          # None = abstain, nothing here fits
    evidence: str               # the literal phrase in the path that decided it
    unresolved: list[str]       # attributes the name cannot settle, e.g. ["material"]
    runner_up: int | None       # second plausible option, if genuinely close
    confidence: float
```

**Step 2 — Validate.** `0 <= choice < len(candidates)` — an in-range integer
resolves to a candidate by construction, so that is the whole range check. Then
the one that earns its keep: **`evidence` is a literal substring of the chosen
path.** It catches a rationale the model invented rather than read. A well-formed
choice with fabricated justification is the failure mode that matters here.

**Step 3 — Branch. This is the agentic behavior, and it is all deterministic.**

- **`choice is None` → `out_of_scope`.** Record the chapter the model names
  instead, stop. Most of the BOM takes this path, and a select prompt with no
  abstain option will confidently misclassify every one of them.

- **`unresolved` non-empty → is the ambiguity rate-bearing?** A rate is set at
  the 8-digit line, so two candidates sharing the first 8 digits **cannot**
  differ in rate. A string comparison, not a call:
  - Same first 8 digits → decide, mark `attribute_unknown_rate_invariant`,
    **do not escalate**. `7318.16.00.60` (stainless) and `.85` (other) are both
    Free. Escalating costs money and changes no number. Verified: 17 eight-digit
    groups across the 48 candidates, zero with more than one distinct rate.
  - Different prefix → compare rates; escalate only if they differ.

- **`runner_up` set and the two rates differ → `needs_review`.** Stop. Do not
  re-prompt.

  This is the branch that used to make a second `compare_codes` call. That call
  added no information — the model saw both options in Step 1 and already chose
  between them — and if the second answer disagreed with the first there was no
  principle for picking a winner. Worse, it contradicted the rule directly above
  it: a rate-bearing ambiguity escalates. Asking the same model twice is not
  escalation. A licensed broker settles whether an M4x12 is a machine screw or a
  socket screw; that is a legal question, not a reading-comprehension one.

**Fallback, when confidence is low but the choice is in the right family:** fall
back to **`rate_source`**, not to a sliced prefix and not to `parent`. You lose
origin-mix precision, you keep the rate exactly right. For the 40 inheriting
candidates `rate_source` is the 8-digit legal line; for the 8 that state their
own rate it is the candidate itself and the fallback is correctly a no-op.
(Verified: 8 candidates hang directly off a 4- or 6-digit heading, so "fall back
to the 8-digit parent" has no target for them.)

**Cache by `component_reference`** — full trace, not just the answer. Reruns are
free and the demo is deterministic.

### The headline case: DIN912

**172 pcs, €15.22 — the largest fastener population in the BOM.** Three
defensible homes:

| n | Code | Text | Rate |
|---|---|---|---|
| [19] | `7318.15.40.00` | Machine screws ≥ 9.5 mm long **and** ≥ 3.2 mm diameter (not including cap screws) | **Free** |
| [25] | `7318.15.60.40` | Other > shanks < 6 mm > Socket screws > Other | **6.2%** |
| [30] | `7318.15.80.45` | Other > shanks ≥ 6 mm > Other > Socket screws > Other | **8.5%** |

An M4x12 satisfies the machine-screw conditions literally (4 ≥ 3.2, 12 ≥ 9.5) and
is also literally a socket screw. Both readings are supportable from the text —
and `(not including cap screws)` is exactly the parenthetical that decides it.
**This is the `needs_review` case**, and flagging it is the right answer. Duty on
the family: **€1.15 as socket screws, €0.00 as machine screws.**

The 6 mm threshold is separately rate-bearing and *is* decidable from the part
name, so it stays in the model's hands: M6+ (103 pcs, €8.95) → 8.5%; M4/M5
(69 pcs, €6.27) → 6.2%. The model must parse `M6x16` → 6 mm → the ≥ 6 mm branch.
A dimension extraction that moves the rate 2.3 points, verifiable against a hand
label.

Pair with DIN985 as the negative control: attribute unresolved (stainless?), both
candidates Free, correct behavior is **decide and move on**.

The three decisions, none of which need a tool: abstain, suppress a non-rate-bearing
ambiguity, escalate a rate-bearing one.

---

## Stage 3 — DataWeb (M6)

One call per distinct code, ~15–25 after classification. Import value by country
of origin, most recent full year → `{country: share}`.

**Cache to `out/dataweb_cache.json` and commit the snapshot.** The demo must run
offline; a live API is a liability in an interview. On failure: use the snapshot.
No third fallback tier until a failure mode actually shows up.

## Stage 3b — Effective rate (M7)

```
effective_rate = SUM over origins of ( share_c x rate_c )
rate_c = 0.0                  if c is a free-program country in `special`
       = general_ad_valorem   otherwise
       + SECTION_301_RATE     if c == China and the code is listed
```

**The `special` column has a compound form. Verified: 12 of 61 coded rows read
`Free (A+,AU,B,…,SG) 3.1% (JP)`** — a free tier *and* a named reduced tier. A
binary in/out test charges Japan the general 6.2% when the schedule says 3.1%.
Parse both tiers, or assert the compound form is absent and fail loudly. (The
earlier `parse_special_programs` took only the first group and silently dropped
the second; it was cut, unread, and comes back with this stage.)

`EUR_USD`, `SECTION_301_RATE`, and the DataWeb period land in `config.py` **with
this stage**, and get printed into the audit output. Assumptions you can point at
beat assumptions you have to defend.

---

## Stages 4–5 — Duty and exposure (M7)

```
customs_value_usd  = quantity x unit_price_eur x EUR_USD
duty_usd           = customs_value_usd x effective_rate
duty_share_of_cogs = duty_usd / total_customs_value
```

Also **switchable exposure**: `customs_value x (effective_rate −
best_available_origin_rate)`. Separates "big line, unavoidable duty" from "big
line, paying 8.5% for no reason." Only the second is actionable.

Rank by `duty_usd` desc. State the assumption: BOM unit price proxies customs
value (real transaction value differs — freight, assists, first sale).

---

## Stage 6 — Recommendations (M8)

Conditional LLM fan-out, top 10 only. Two shapes, both grounded:

- **Origin shift** — name the alternate country from the DataWeb mix and the $ delta.
- **Tariff engineering** — where a sibling line carries a materially lower rate
  and the difference is a real product attribute, surface it as a question for a
  licensed broker, not as advice. The DIN912 machine-screw reading is this shape.

Every recommendation cites the HTS code, the compared rate, and the $ delta, or
it is not emitted.

---

## Audit trail (cross-cutting — do not defer)

`out/audit.jsonl`, one object per component: the integer the model returned and
the code it resolved to, the `evidence` phrase and its substring check, which
branch fired and why, `rate_source` (40 of 48 candidates inherit their rate — a
reviewer cannot check the number without it), origin mix and whether it was live
or snapshot, every constant used. With one call and no tools there is no trace to
reconstruct: the prompt, the JSON, and the branch are the whole record.

---

## Evaluation (M5)

~25 hand-labeled lines, **including out-of-scope ones** — that is most of the
real input.

| Arm | |
|---|---|
| A | select, no abstain, no branching |
| B | select + abstain |
| C | full loop (abstain + rate-invariance check + escalation) |

All three arms make **one LLM call per component**. They differ only in the
prompt contract and in what Python does with the answer, which is what makes the
comparison clean: no arm can win by spending more tokens.

Metrics: top-1 accuracy in-scope; out-of-scope precision/recall; total duty error
in EUR; **`needs_review` rate** (C only — a loop that escalates everything is
useless, and one that escalates nothing isn't doing its job).

Expected: A→B is a large jump driven entirely by abstention. B→C moves duty error
and review volume rather than top-1 accuracy — the rate-invariance check should
*suppress* escalations without changing any number, and escalation should catch
the DIN912 family. That is the honest result and more interesting than a uniform
win.

**Why the loop earns its place** — not retrieval repair, the argument that died
with BM25. Three grounds, all deterministic: abstention over a candidate set that
is mostly wrong by construction; suppressing ambiguities that cannot move the
rate; escalating the ones that can. Arm A vs C measures it.

---

## Milestones

| # | Deliverable | Est. |
|---|---|---|
| 1 | `hts/index.py` + assertions | **done** |
| 2 | `bom/flatten.py` + reconcile to 1348.83 | **done** |
| 3 | `hts/render.py` — numbered tree, every row, rates withheld | 0.5h |
| 4 | `agent/loop.py` — select, validate evidence, three branches, cache | 3h |
| 5 | `eval/` — golden set incl. out-of-scope, arms A/B/C | 2.5h |
| 6 | `duty/dataweb.py` + committed snapshot | 2h |
| 7 | `duty/effective_rate.py` (incl. compound `special`) + `exposure.py` | 2h |
| 8 | `recommend/fanout.py` top-N | 1.5h |
| 9 | `run.py`, README with scope statement and eval table | 1.5h |

Milestones 1–5 are the project. 6–9 make it a deliverable.

**Build 3 before 4, and run arm A first.** Watching a no-abstain flat select
confidently classify the SMPS tells you which branch carries the weight. Design
the loop from that, not from imagination.
