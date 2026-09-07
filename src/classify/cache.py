"""Disk cache of the model's answer, keyed by component_reference.

What is cached is the *selection*, not the classification. The branch logic in
`loop.py` re-runs on every replay, so changing how Python treats an answer --
now validation and code resolution -- costs nothing and calls nothing.
Changing the prompt does not get that treatment: the model
returns an integer whose meaning is fixed by the tree it read, so a cached
`choice: 25` from a different tree points at a different code.

`fingerprint` is that guard. It covers the system prompt (which contains the
tree) and the model id, so swapping htsdata.json, reordering candidates or
switching models invalidates rather than silently mis-resolves.

The classifier also includes the component prompt in the stored fingerprint,
so an uploaded BOM with a changed description or assembly context is refreshed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def fingerprint(system: str, model: str) -> str:
    return hashlib.sha256(f"{model}\n{system}".encode()).hexdigest()[:16]


class SelectionCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict[str, dict] = {}

    @classmethod
    def load(cls, path: str | Path) -> "SelectionCache":
        cache = cls(path)
        if cache.path.exists():
            cache.entries = json.loads(cache.path.read_text())
        return cache

    def get(self, reference: str, fingerprint: str) -> dict | None:
        entry = self.entries.get(reference)
        if entry is None or entry["fingerprint"] != fingerprint:
            return None
        return entry["selection"]

    def put(self, reference: str, fingerprint: str, selection: dict) -> None:
        self.entries[reference] = {"fingerprint": fingerprint, "selection": selection}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as fh:
            json.dump(self.entries, fh, indent=2, sort_keys=True)
