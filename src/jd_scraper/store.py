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

from .filters import domain_of
from .models import Job

STATUSES = ("new", "interested", "applied", "interviewing", "rejected", "dismissed")
DEFAULT_STATUS = "new"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  status TEXT DEFAULT 'new',
  notes TEXT,
  status_updated_at TEXT,
  job_title TEXT,
  company TEXT,
  url TEXT,
  final_url TEXT,
  description TEXT,
  salary_string TEXT,
  easy_apply INTEGER,
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
        self._migrate()
        self.conn.commit()

    # Columns added after the first release. Existing databases get them via
    # ALTER TABLE, then backfilled from the stored raw payload -- which is exactly
    # why the full response is kept, so widening the schema never costs credits.
    # (column, declaration, json path in `raw` or None for locally-owned columns)
    _ADDED_COLUMNS = (
        ("final_url", "TEXT", "$.final_url"),
        ("description", "TEXT", "$.description"),
        ("salary_string", "TEXT", "$.salary_string"),
        ("easy_apply", "INTEGER", "$.easy_apply"),
        # Your own triage state. Never overwritten by a search, since a re-fetch
        # of an already-applied job must not reset it to "new".
        ("status", "TEXT", None),
        ("notes", "TEXT", None),
        ("status_updated_at", "TEXT", None),
    )

    def _migrate(self) -> None:
        existing = {row[1] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        added = [c for c in self._ADDED_COLUMNS if c[0] not in existing]
        for name, decl, _ in added:
            self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {decl}")
        if added:
            self.backfill_from_raw([c[0] for c in added if c[2]])
        self.conn.execute(
            f"UPDATE jobs SET status = '{DEFAULT_STATUS}' WHERE status IS NULL"
        )

    def backfill_from_raw(self, columns: list[str] | None = None) -> int:
        """Populate columns from the stored raw JSON. Returns rows touched.

        Needs SQLite's JSON1 extension; if it is unavailable the rows simply stay
        null and future searches fill them in, so this is best-effort.
        """
        wanted = {c[0]: c[2] for c in self._ADDED_COLUMNS}
        targets = columns or list(wanted)
        touched = 0
        for name in targets:
            path = wanted.get(name)
            if not path:
                continue
            try:
                cursor = self.conn.execute(
                    f"UPDATE jobs SET {name} = json_extract(raw, ?) "
                    f"WHERE {name} IS NULL AND raw IS NOT NULL",
                    (path,),
                )
                touched += cursor.rowcount or 0
            except sqlite3.OperationalError:
                return touched
        self.conn.commit()
        return touched

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
                  id, job_title, company, url, final_url, description, salary_string,
                  easy_apply, source, location, country_code, remote,
                  date_posted, discovered_at, min_salary_usd, max_salary_usd, seniority,
                  profile, first_seen_at, raw, status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                -- status/notes are deliberately absent from the UPDATE below:
                -- re-fetching a job you already applied to must not reset it.
                ON CONFLICT(id) DO UPDATE SET
                  job_title = excluded.job_title,
                  company = excluded.company,
                  url = excluded.url,
                  final_url = excluded.final_url,
                  description = excluded.description,
                  salary_string = excluded.salary_string,
                  easy_apply = excluded.easy_apply,
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
                    job.company_name(),
                    job.best_url(),
                    job.final_url,
                    job.description,
                    job.salary_string,
                    int(job.easy_apply) if job.easy_apply is not None else None,
                    domain_of(job.best_url()),
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
                    DEFAULT_STATUS,
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

    def set_status(
        self, job_id: str, status: str | None = None, notes: str | None = None
    ) -> sqlite3.Row | None:
        """Update triage state. Either field may be omitted to leave it alone."""
        if status is not None and status not in STATUSES:
            raise ValueError(f"unknown status {status!r}; valid: {list(STATUSES)}")

        sets, params = [], []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if notes is not None:
            sets.append("notes = ?")
            params.append(notes)
        if not sets:
            return self.get_job(job_id)

        sets.append("status_updated_at = ?")
        params.extend([_now(), str(job_id)])
        self.conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
        self.conn.commit()
        return self.get_job(job_id)

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT COALESCE(status, ?) AS s, COUNT(*) FROM jobs GROUP BY s",
            (DEFAULT_STATUS,),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def profiles(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT profile FROM jobs WHERE profile IS NOT NULL ORDER BY profile"
        ).fetchall()
        return [r[0] for r in rows]

    def list_jobs(
        self,
        *,
        limit: int = 50,
        since_days: int | None = None,
        profile: str | None = None,
        new_only_since: str | None = None,
        query: str | None = None,
        status: str | None = None,
        remote: bool | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []

        if query:
            like = f"%{query}%"
            clauses.append(
                "(job_title LIKE ? OR company LIKE ? OR location LIKE ? "
                "OR description LIKE ?)"
            )
            params.extend([like, like, like, like])
        if status:
            clauses.append("COALESCE(status, ?) = ?")
            params.extend([DEFAULT_STATUS, status])
        if remote is not None:
            clauses.append("remote = ?")
            params.append(int(remote))

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

    def get_job(self, job_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (str(job_id),)
        ).fetchone()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # --- incremental fetching -------------------------------------------------

    def last_run_at(self, profile_name: str) -> str | None:
        """When this profile last ran, or None if it never has."""
        row = self.conn.execute(
            "SELECT MAX(started_at) FROM runs WHERE profile = ?", (profile_name,)
        ).fetchone()
        return row[0] if row and row[0] else None

    def known_job_ids(self, *, since: str | None = None, limit: int = 500) -> list[int]:
        """IDs already stored, newest first.

        Bounded by `limit` because these go into the request body as `job_id_not`,
        and an unbounded list would grow the payload without end. Non-numeric ids
        are skipped -- the API's job_id_not takes integers.
        """
        if since:
            rows = self.conn.execute(
                "SELECT id FROM jobs WHERE first_seen_at >= ? "
                "ORDER BY first_seen_at DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT id FROM jobs ORDER BY first_seen_at DESC LIMIT ?", (limit,)
            ).fetchall()

        ids: list[int] = []
        for row in rows:
            try:
                ids.append(int(row[0]))
            except (TypeError, ValueError):
                continue
        return ids
