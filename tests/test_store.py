from jd_scraper.models import Job
from jd_scraper.store import Store


def _jobs(sample_response):
    return [Job.model_validate(j) for j in sample_response["data"]]


def test_insert_then_repeat_run_finds_nothing_new(tmp_path, sample_response):
    db = tmp_path / "jobs.db"
    jobs = _jobs(sample_response)

    with Store(db) as store:
        total, new = store.upsert_jobs(jobs, "test-profile")
        assert (total, new) == (3, 3)

    with Store(db) as store:
        total, new = store.upsert_jobs(jobs, "test-profile")
        assert (total, new) == (3, 0)
        assert store.count() == 3


def test_first_seen_at_survives_reupsert(tmp_path, sample_response):
    db = tmp_path / "jobs.db"
    jobs = _jobs(sample_response)

    with Store(db) as store:
        store.upsert_jobs(jobs, "test-profile")
        original = store.conn.execute(
            "SELECT first_seen_at FROM jobs WHERE id = 'job-1'"
        ).fetchone()[0]

        jobs[0].job_title = "Updated Title"
        store.upsert_jobs(jobs, "test-profile")

        row = store.conn.execute("SELECT * FROM jobs WHERE id = 'job-1'").fetchone()
        assert row["first_seen_at"] == original, "first_seen_at must not be overwritten"
        assert row["job_title"] == "Updated Title", "other fields should refresh"


def test_new_only_since_isolates_second_batch(tmp_path, sample_response):
    db = tmp_path / "jobs.db"
    jobs = _jobs(sample_response)

    with Store(db) as store:
        store.upsert_jobs(jobs[:2], "test-profile")
        marker = store.conn.execute("SELECT MAX(first_seen_at) FROM jobs").fetchone()[0]

        store.upsert_jobs(jobs, "test-profile")
        rows = store.list_jobs(limit=50, new_only_since=marker)
        ids = {r["id"] for r in rows}
        assert "job-3" in ids


def test_raw_payload_is_retained(tmp_path, sample_response):
    db = tmp_path / "jobs.db"
    with Store(db) as store:
        store.upsert_jobs(_jobs(sample_response), "test-profile")
        raw = store.conn.execute("SELECT raw FROM jobs WHERE id = 'job-1'").fetchone()[0]
    assert "an_unexpected_field" in raw, "unmapped API fields must survive on disk"


def test_jobs_without_id_are_skipped(tmp_path):
    with Store(tmp_path / "jobs.db") as store:
        total, new = store.upsert_jobs([Job(job_title="No id")], "test-profile")
        assert (total, new) == (0, 0)


def test_record_run(tmp_path, sample_response):
    with Store(tmp_path / "jobs.db") as store:
        store.upsert_jobs(_jobs(sample_response), "p")
        store.record_run("p", {"job_title_or": ["ML"]}, 3, 3)
        row = store.conn.execute("SELECT * FROM runs").fetchone()
        assert row["profile"] == "p"
        assert row["results"] == 3
        assert "job_title_or" in row["request_body"]
