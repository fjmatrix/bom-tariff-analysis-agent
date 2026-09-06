"""The one prompt this system sends.

Two halves: the system prompt is ~1,150 tokens and byte-identical across all 147
calls, the component message is three short lines and changes every call.
`loop.py` sends the first as `instructions`, which holds it at the head of every
request. There is no breakpoint to mark -- the provider caches a repeated prefix
on its own or it does not, and nothing here can force it. Keeping the halves
split is what makes the prefix identical call to call; that is the whole of what
this file controls.

The contract the model is held to is stated here and enforced in `loop.py`.
`evidence` is the load-bearing one: it must be a phrase the model *copied* out
of the tree, which is what makes a fabricated rationale detectable by string
comparison rather than by a second opinion.
"""

from __future__ import annotations

from src.bom.flatten import Component
from src.hts.render import CandidateTree

SYSTEM = """\
You classify purchased components against the US Harmonized Tariff Schedule.

The tree below is the ENTIRE schedule available to you. Indentation is
hierarchy: a line's full legal meaning is its own text preceded by every line
above it at a shallower indent. Unnumbered lines are grouping text -- they carry
words that discriminate, but nothing can be classified under them. Only the
numbered lines are selectable.

{tree}

Return the number of the single line that best classifies the component.

- choice: the number, or null if nothing in this tree fits. This tree covers a
  narrow slice of the schedule, so most components will not belong in it.
  Abstaining is the correct answer far more often than not; a confident wrong
  line is the expensive mistake, not a null.
- abstain_chapter: when you abstain, the HTS chapter or heading you believe the
  component belongs to. Empty string when you chose a line.
- evidence: a phrase copied VERBATIM from the line you chose, or from a line
  above it in its own indentation chain. Copy the characters exactly. This is
  checked against the schedule text; a paraphrase fails the check.
- unresolved: attributes the tree distinguishes on that the component
  description does not settle -- e.g. "material" when the tree splits stainless
  from other and the part name says neither. Empty list if none.
- runner_up: the number of a second line that is genuinely defensible on the
  text, or null. Do not name one to hedge; a runner-up is read as a real
  competing reading.
- confidence: 0.0 to 1.0, for the choice itself.

Duty rates are not shown and are not part of this decision."""


def system_prompt(tree: CandidateTree) -> str:
    return SYSTEM.format(tree=tree.text)


def component_prompt(component: Component) -> str:
    """Name plus assembly chain.

    The chain is often the stronger signal: 'Lock Nut DIN985 M4' sits in a bag
    named 'EVO - DIN985 Lock Nut Bag - M4'. Quantity and price are not
    classification evidence and are left out.
    """
    return (
        f"reference: {component.reference}\n"
        f"name:      {component.name}\n"
        f"assembly:  {component.context}"
    )
