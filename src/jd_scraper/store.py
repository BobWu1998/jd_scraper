"""SQLite persistence with dedup on job id.

The full API payload for each job is kept in `raw`, so if a field mapping turns out to
be wrong, the data can be re-parsed locally instead of re-fetched at credit cost.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  job_title TEXT,
  company TEXT,
  url TEXT,
  source TEXT,
  location TEXT,
  country_code TEXT,
  remote INTEGER,
  date_posted TEXT,
  discovered_at TEXT,
  min_salary_usd REAL,
  max_salary_usd REAL,
  seniority TEXT,
  profile TEXT,
  first_seen_at TEXT,
  raw TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_date_posted ON jobs(date_posted);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen_at);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  profile TEXT,
  started_at TEXT,
  request_body TEXT,
  results INTEGER,
  new_results INTEGER
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def upsert_jobs(self, jobs: Iterable[Job], profile_name: str) -> tuple[int, int]:
        """Insert or update. Returns (total_seen, newly_inserted).

        first_seen_at is preserved on conflict so "new since last run" stays truthful.
        """
        total = 0
        new = 0
        now = _now()

        for job in jobs:
            if job.id is None:
                continue
            total += 1
            job_id = str(job.id)

            existed = self.conn.execute(
                "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()

            self.conn.execute(
                """
                INSERT INTO jobs (
                  id, job_title, company, url, source, location, country_code, remote,
                  date_posted, discovered_at, min_salary_usd, max_salary_usd, seniority,
                  profile, first_seen_at, raw
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                  job_title = excluded.job_title,
                  company = excluded.company,
                  url = excluded.url,
                  source = excluded.source,
                  location = excluded.location,
                  country_code = excluded.country_code,
                  remote = excluded.remote,
                  date_posted = excluded.date_posted,
                  discovered_at = excluded.discovered_at,
                  min_salary_usd = excluded.min_salary_usd,
                  max_salary_usd = excluded.max_salary_usd,
                  seniority = excluded.seniority,
                  raw = excluded.raw
                """,
                (
                    job_id,
                    job.job_title,
                    job.company,
                    job.best_url(),
                    job.source,
                    job.best_location(),
                    job.country_code,
                    int(job.remote) if job.remote is not None else None,
                    job.date_posted,
                    job.discovered_at,
                    job.min_annual_salary_usd,
                    job.max_annual_salary_usd,
                    job.seniority,
                    profile_name,
                    now,
                    json.dumps(job.model_dump(mode="json"), default=str),
                ),
            )
            if existed is None:
                new += 1

        self.conn.commit()
        return total, new

    def record_run(
        self,
        profile_name: str,
        request_body: dict[str, Any],
        results: int,
        new_results: int,
    ) -> None:
        self.conn.execute(
            "INSERT INTO runs (profile, started_at, request_body, results, new_results) "
            "VALUES (?,?,?,?,?)",
            (profile_name, _now(), json.dumps(request_body, sort_keys=True), results, new_results),
        )
        self.conn.commit()

    def list_jobs(
        self,
        *,
        limit: int = 50,
        since_days: int | None = None,
        profile: str | None = None,
        new_only_since: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []

        if since_days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
            clauses.append("(date_posted >= ? OR first_seen_at >= ?)")
            params.extend([cutoff, cutoff])
        if profile:
            clauses.append("profile = ?")
            params.append(profile)
        if new_only_since:
            clauses.append("first_seen_at >= ?")
            params.append(new_only_since)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self.conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY date_posted DESC, first_seen_at DESC LIMIT ?",
            params,
        ).fetchall()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
