"""Descriptions and application links: stored as columns, backfilled from raw."""

import csv
import json
import sqlite3

from jd_scraper.export import write_csv, write_jsonl
from jd_scraper.models import Job
from jd_scraper.store import Store

FULL_JOB = Job(
    id=1111,
    job_title="ML Engineer",
    url="https://www.linkedin.com/jobs/view/1111",
    final_url="https://acme.example/careers/1111",
    description="## About the role\n\nYou will train models.",
    salary_string="$180K - $240K per year",
    easy_apply=True,
)


def test_description_and_links_are_stored(tmp_path):
    with Store(tmp_path / "jobs.db") as store:
        store.upsert_jobs([FULL_JOB], "p")
        row = store.get_job("1111")

    assert row["description"] == "## About the role\n\nYou will train models."
    assert row["final_url"] == "https://acme.example/careers/1111"
    assert row["url"] == "https://www.linkedin.com/jobs/view/1111"
    assert row["salary_string"] == "$180K - $240K per year"
    assert row["easy_apply"] == 1


def test_get_job_returns_none_when_missing(tmp_path):
    with Store(tmp_path / "jobs.db") as store:
        assert store.get_job("does-not-exist") is None


def test_description_refreshes_on_reupsert(tmp_path):
    """A row first saved blurred must pick up the real text on a later run."""
    with Store(tmp_path / "jobs.db") as store:
        store.upsert_jobs([Job(id=1111, description="[blurred]")], "p")
        store.upsert_jobs([FULL_JOB], "p")
        assert store.get_job("1111")["description"].startswith("## About the role")


# --- migrating a database created before these columns existed -----------------

OLD_SCHEMA = """
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, job_title TEXT, company TEXT, url TEXT, source TEXT,
  location TEXT, country_code TEXT, remote INTEGER, date_posted TEXT,
  discovered_at TEXT, min_salary_usd REAL, max_salary_usd REAL, seniority TEXT,
  profile TEXT, first_seen_at TEXT, raw TEXT
);
CREATE TABLE runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT, started_at TEXT,
  request_body TEXT, results INTEGER, new_results INTEGER
);
"""


def _legacy_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO jobs (id, job_title, url, raw, first_seen_at) VALUES (?,?,?,?,?)",
        (
            "9999",
            "Old Job",
            "https://linkedin.com/jobs/view/9999",
            json.dumps(
                {
                    "id": 9999,
                    "description": "Legacy description text",
                    "final_url": "https://old.example/apply",
                    "salary_string": "$100K",
                }
            ),
            "2026-07-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()


def test_opening_a_legacy_db_adds_the_columns(tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db)

    with Store(db) as store:
        columns = {r[1] for r in store.conn.execute("PRAGMA table_info(jobs)")}

    assert {"description", "final_url", "salary_string", "easy_apply"} <= columns


def test_legacy_rows_are_backfilled_from_raw(tmp_path):
    """The whole point of keeping raw: widen the schema without re-fetching."""
    db = tmp_path / "legacy.db"
    _legacy_db(db)

    with Store(db) as store:
        row = store.get_job("9999")

    assert row["description"] == "Legacy description text"
    assert row["final_url"] == "https://old.example/apply"
    assert row["salary_string"] == "$100K"


def test_migration_is_idempotent(tmp_path):
    db = tmp_path / "legacy.db"
    _legacy_db(db)

    with Store(db) as store:
        pass
    with Store(db) as store:  # second open must not fail on duplicate columns
        assert store.get_job("9999")["description"] == "Legacy description text"


# --- export --------------------------------------------------------------------


def test_csv_includes_links_but_not_description_by_default(tmp_path):
    with Store(tmp_path / "jobs.db") as store:
        store.upsert_jobs([FULL_JOB], "p")
        rows = store.list_jobs(limit=10)

    out = tmp_path / "jobs.csv"
    write_csv(rows, out)
    header = next(csv.reader(out.open()))

    assert "final_url" in header
    assert "salary_string" in header
    assert "description" not in header


def test_csv_can_include_description(tmp_path):
    with Store(tmp_path / "jobs.db") as store:
        store.upsert_jobs([FULL_JOB], "p")
        rows = store.list_jobs(limit=10)

    out = tmp_path / "jobs.csv"
    write_csv(rows, out, with_description=True)
    record = next(csv.DictReader(out.open()))

    assert record["description"].startswith("## About the role")
    assert record["final_url"] == "https://acme.example/careers/1111"


def test_jsonl_always_carries_the_full_payload(tmp_path):
    with Store(tmp_path / "jobs.db") as store:
        store.upsert_jobs([FULL_JOB], "p")
        rows = store.list_jobs(limit=10)

    out = tmp_path / "jobs.jsonl"
    write_jsonl(rows, out)
    record = json.loads(out.read_text().splitlines()[0])

    assert record["description"].startswith("## About the role")
    assert record["final_url"] == "https://acme.example/careers/1111"
