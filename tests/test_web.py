"""The local web UI. Read-only against the API -- it must never spend credits."""

import pytest
from fastapi.testclient import TestClient

from jd_scraper.models import Job
from jd_scraper.store import Store
from jd_scraper.web import create_app

JOBS = [
    Job(
        id=1111,
        job_title="Machine Learning Engineer",
        company="Acme AI",
        location="Atlanta, GA",
        remote=True,
        date_posted="2026-07-27",
        seniority="mid_level",
        url="https://www.linkedin.com/jobs/view/1111",
        final_url="https://acme.example/careers/1111",
        description="## Role\n\nTrain models with **PyTorch**.",
    ),
    Job(
        id=2222,
        job_title="Robotics Engineer",
        company="Globex",
        location="Remote, US",
        remote=False,
        date_posted="2026-07-26",
        url="https://www.linkedin.com/jobs/view/2222",
    ),
]


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "jobs.db"
    with Store(db) as store:
        store.upsert_jobs(JOBS, "atlanta-ml")
    return TestClient(create_app(db))


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "jd_scraper" in response.text


def test_meta_reports_totals_and_statuses(client):
    meta = client.get("/api/meta").json()
    assert meta["exists"] is True
    assert meta["total"] == 2
    assert "applied" in meta["statuses"]
    assert meta["profiles"] == ["atlanta-ml"]


def test_lists_jobs(client):
    jobs = client.get("/api/jobs").json()
    assert {j["id"] for j in jobs} == {"1111", "2222"}
    assert all(j["status"] == "new" for j in jobs)


def test_search_matches_description_not_just_title(client):
    jobs = client.get("/api/jobs", params={"q": "PyTorch"}).json()
    assert [j["id"] for j in jobs] == ["1111"]


def test_filter_by_remote(client):
    assert [j["id"] for j in client.get("/api/jobs", params={"remote": True}).json()] == ["1111"]
    assert [j["id"] for j in client.get("/api/jobs", params={"remote": False}).json()] == ["2222"]


def test_detail_renders_markdown(client):
    job = client.get("/api/jobs/1111").json()
    assert "<h2>Role</h2>" in job["description_html"]
    assert "<strong>PyTorch</strong>" in job["description_html"]
    assert job["final_url"] == "https://acme.example/careers/1111"
    assert "raw" not in job, "the raw blob should not be shipped to the browser"


def test_detail_without_description_is_empty_not_broken(client):
    assert client.get("/api/jobs/2222").json()["description_html"] == ""


def test_missing_job_is_404(client):
    assert client.get("/api/jobs/does-not-exist").status_code == 404


def test_status_and_notes_round_trip(client):
    response = client.post(
        "/api/jobs/1111/status", json={"status": "applied", "notes": "referred by Sam"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "applied"

    detail = client.get("/api/jobs/1111").json()
    assert detail["status"] == "applied"
    assert detail["notes"] == "referred by Sam"


def test_filter_by_status(client):
    client.post("/api/jobs/1111/status", json={"status": "applied"})
    jobs = client.get("/api/jobs", params={"status": "applied"}).json()
    assert [j["id"] for j in jobs] == ["1111"]


def test_invalid_status_is_rejected(client):
    assert client.post("/api/jobs/1111/status", json={"status": "banana"}).status_code == 400


def test_notes_alone_do_not_clobber_status(client):
    client.post("/api/jobs/1111/status", json={"status": "applied"})
    client.post("/api/jobs/1111/status", json={"notes": "just a note"})
    detail = client.get("/api/jobs/1111").json()
    assert detail["status"] == "applied"
    assert detail["notes"] == "just a note"


def test_refetching_a_job_preserves_triage_state(tmp_path):
    """A search must never reset a job you already applied to."""
    db = tmp_path / "jobs.db"
    with Store(db) as store:
        store.upsert_jobs(JOBS, "atlanta-ml")
        store.set_status("1111", "applied", "my notes")

        store.upsert_jobs(JOBS, "atlanta-ml")  # same jobs come back in a later run
        row = store.get_job("1111")

    assert row["status"] == "applied"
    assert row["notes"] == "my notes"


def test_works_with_no_database(tmp_path):
    client = TestClient(create_app(tmp_path / "absent.db"))
    assert client.get("/api/meta").json()["exists"] is False
    assert client.get("/api/jobs").json() == []
