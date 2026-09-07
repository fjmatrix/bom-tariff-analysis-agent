"""Cache structured selections by the model and complete classification prompts."""

import hashlib
import json
from pathlib import Path


def fingerprint(system: str, model: str, prompt: str) -> str:
    request = json.dumps([model, system, prompt], ensure_ascii=False)
    return hashlib.sha256(request.encode()).hexdigest()


class SelectionCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: dict[str, dict] = {}

    @classmethod
    def load(cls, path: str | Path) -> "SelectionCache":
        cache = cls(path)
        try:
            entries = json.loads(cache.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
            return cache
        if isinstance(entries, dict):
            cache.entries = entries
        return cache

    def get(self, key: str) -> dict | None:
        selection = self.entries.get(key)
        return selection if isinstance(selection, dict) else None

    def put(self, key: str, selection: dict) -> None:
        self.entries[key] = selection

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.entries, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
