"""Persist classifications by part reference across runs."""

import sqlite3
from contextlib import closing
from pathlib import Path

from src.config import ROOT


class ClassificationCache:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else ROOT / ".cache/classification.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS classifications ("
                "reference TEXT PRIMARY KEY, htsno TEXT NOT NULL, evidence TEXT NOT NULL)"
            )

    def get(self, reference: str) -> dict | None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT reference, htsno, evidence FROM classifications WHERE reference = ?",
                (reference,),
            ).fetchone()
        return dict(row) if row is not None else None

    def put(self, reference: str, htsno: str, evidence: str) -> None:
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "INSERT INTO classifications (reference, htsno, evidence) VALUES (?, ?, ?) "
                "ON CONFLICT(reference) DO UPDATE SET "
                "htsno = excluded.htsno, evidence = excluded.evidence",
                (reference, htsno, evidence),
            )
