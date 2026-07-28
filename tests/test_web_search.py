"""Running a search from the web UI -- and the guards that keep a click cheap."""

import httpx
import pytest
from fastapi.testclient import TestClient

from jd_scraper import runner, web
from jd_scraper.store import Store

RESPONSE = {
    "metadata": {"total_results": 2, "truncated_results": 0},
    "data": [
        {
            "id": 5001,
            "job_title": "ML Engineer",
            "company": "Acme",
            "url": "https://www.linkedin.com/jobs/view/5001",
            "date_posted": "2026-07-27",
            "description": "Build models.",
        },
        {
            "id": 5002,
            "job_title": "Robotics Engineer",
            "company": "Globex",
            "url": "https://www.linkedin.com/jobs/view/5002",
            "date_posted": "2026-07-26",
        },
    ],
}

BASE_FILTERS = {
    "name": "custom",
    "titles": ["machine learning"],
    "posted_within_days": 5,
    "limit": 4,
    "page_size": 4,
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    monkeypatch.setenv("JD_DB_PATH", str(db))
    monkeypatch.setenv("THEIRSTACK_API_KEY", "test-key")
    monkeypatch.setenv("JD_WEB_MAX_RESULTS", "20")

    calls = []

    def fake_client(api_key, **kwargs):
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=RESPONSE)

        from jd_scraper.client import TheirStackClient

        return TheirStackClient(api_key, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(runner, "TheirStackClient", fake_client)
    app = TestClient(web.create_app(db))
    app.calls = calls
    app.db = db
    return app


# --- planning is free ----------------------------------------------------------


def test_plan_never_calls_the_api(client):
    response = client.post("/api/search/plan", json={"profile": BASE_FILTERS})
    assert response.status_code == 200
    assert client.calls == [], "planning must not hit the API"

    plan = response.json()
    assert plan["queries"][0]["body"]["job_title_or"] == ["machine learning"]
    assert plan["cap"] == 4


def test_plan_reports_credit_exposure(client):
    paid = client.post("/api/search/plan", json={"profile": BASE_FILTERS}).json()
    assert paid["max_credits"] == 4
    assert paid["preview"] is False

    free = client.post(
        "/api/search/plan", json={"profile": {**BASE_FILTERS, "preview": True}}
    ).json()
    assert free["max_credits"] == 0
    assert free["preview"] is True


def test_plan_splits_when_include_remote_is_set(client):
    plan = client.post(
        "/api/search/plan",
        json={
            "profile": {
                **BASE_FILTERS,
                "locations": {"countries": ["US"], "patterns": ["Atlanta"]},
                "include_remote": True,
            }
        },
    ).json()
    assert [q["label"] for q in plan["queries"]] == ["location", "remote"]


def test_plan_rejects_a_profile_with_no_date_filter(client):
    response = client.post(
        "/api/search/plan", json={"profile": {"name": "x", "titles": ["ml"]}}
    )
    assert response.status_code == 400
    assert "posted_within_days" in response.json()["detail"]


def test_invalid_filter_gives_a_readable_error(client):
    response = client.post(
        "/api/search/plan", json={"profile": {**BASE_FILTERS, "seniority": ["principal"]}}
    )
    assert response.status_code == 422
    assert "unknown seniority" in response.json()["detail"]


# --- running costs money, so it needs a confirmation ---------------------------


def test_paid_run_without_confirmation_is_refused(client):
    response = client.post("/api/search/run", json={"profile": BASE_FILTERS})
    assert response.status_code == 400
    assert "credits" in response.json()["detail"]
    assert client.calls == [], "a refused run must not reach the API"


def test_paid_run_with_confirmation_proceeds(client):
    response = client.post(
        "/api/search/run", json={"profile": BASE_FILTERS, "confirm": True}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stored"] == 2
    assert body["new"] == 2
    assert body["credits_used"] == 2
    assert len(client.calls) == 1


def test_preview_run_needs_no_confirmation_and_costs_nothing(client):
    response = client.post(
        "/api/search/run", json={"profile": {**BASE_FILTERS, "preview": True}}
    )
    assert response.status_code == 200
    assert response.json()["credits_used"] == 0
    assert response.json()["preview"] is True


def test_web_cap_is_clamped_server_side(client):
    """A malformed request must not be able to buy 10,000 jobs."""
    plan = client.post(
        "/api/search/plan", json={"profile": {**BASE_FILTERS, "limit": 10_000}}
    ).json()
    assert plan["cap"] == 20, "clamped to JD_WEB_MAX_RESULTS"


def test_results_are_stored_and_visible(client):
    client.post("/api/search/run", json={"profile": BASE_FILTERS, "confirm": True})
    jobs = client.get("/api/jobs").json()
    assert {j["id"] for j in jobs} == {"5001", "5002"}
    assert all(j["profile"] == "custom" for j in jobs)


def test_second_run_is_incremental(client):
    client.post("/api/search/run", json={"profile": BASE_FILTERS, "confirm": True})
    second = client.post(
        "/api/search/run", json={"profile": BASE_FILTERS, "confirm": True}
    ).json()

    assert second["excluded_ids"] == 2, "already-stored jobs are excluded server-side"
    assert second["discovered_since"] is not None


def test_run_preserves_existing_triage_state(client):
    client.post("/api/search/run", json={"profile": BASE_FILTERS, "confirm": True})
    client.post("/api/jobs/5001/status", json={"status": "applied"})
    client.post(
        "/api/search/run", json={"profile": BASE_FILTERS, "confirm": True, "full": True}
    )
    assert client.get("/api/jobs/5001").json()["status"] == "applied"


def test_saved_profiles_are_listed(client, tmp_path, monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "p.yml").write_text("name: saved-one\nposted_within_days: 3\n")
    monkeypatch.setenv("JD_PROFILES_DIR", str(profiles))

    fresh = TestClient(web.create_app(client.db))
    data = fresh.get("/api/profiles").json()
    assert [p["name"] for p in data["profiles"]] == ["saved-one"]
    assert data["profiles"][0]["profile"]["posted_within_days"] == 3


def test_missing_api_key_is_a_clear_error(client, monkeypatch, tmp_path):
    monkeypatch.setenv("THEIRSTACK_API_KEY", "")
    monkeypatch.chdir(tmp_path)  # avoid picking up a .env
    fresh = TestClient(web.create_app(client.db))
    response = fresh.post(
        "/api/search/run", json={"profile": {**BASE_FILTERS, "preview": True}}
    )
    assert response.status_code == 400
    assert "THEIRSTACK_API_KEY" in response.json()["detail"]
