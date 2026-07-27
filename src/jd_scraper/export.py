"""CSV / JSONL export of stored jobs."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Sequence

COLUMNS = [
    "id",
    "job_title",
    "company",
    "url",
    "location",
    "country_code",
    "remote",
    "date_posted",
    "seniority",
    "min_salary_usd",
    "max_salary_usd",
    "profile",
    "first_seen_at",
]


def write_csv(rows: Sequence[sqlite3.Row], out_path: str | Path) -> int:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row[c] for c in COLUMNS})
    return len(rows)


def write_jsonl(rows: Sequence[sqlite3.Row], out_path: str | Path) -> int:
    """Writes the full raw payload per job, not just the flattened columns."""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            raw = row["raw"]
            record = json.loads(raw) if raw else {c: row[c] for c in COLUMNS}
            fh.write(json.dumps(record, default=str) + "\n")
    return len(rows)
