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
    "final_url",
    "location",
    "country_code",
    "remote",
    "date_posted",
    "seniority",
    "salary_string",
    "min_salary_usd",
    "max_salary_usd",
    "easy_apply",
    "profile",
    "first_seen_at",
]

# Descriptions run to thousands of characters, so they are opt-in -- they would
# otherwise make the CSV unreadable in a spreadsheet.
DESCRIPTION_COLUMN = "description"


def write_csv(
    rows: Sequence[sqlite3.Row],
    out_path: str | Path,
    *,
    with_description: bool = False,
) -> int:
    columns = [*COLUMNS, DESCRIPTION_COLUMN] if with_description else COLUMNS
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _cell(row, c) for c in columns})
    return len(rows)


def _cell(row: sqlite3.Row, column: str):
    try:
        return row[column]
    except (IndexError, KeyError):
        # Column added after this database was created and not yet backfilled.
        return None


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
