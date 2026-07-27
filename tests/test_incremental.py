"""Incremental fetching: never pay twice for a posting already in the database."""

from datetime import datetime, timedelta, timezone

from jd_scraper.filters import build_request_body
from jd_scraper.models import Job, SearchProfile
from jd_scraper.store import Store


def _jobs(sample_response):
    return [Job.model_validate(j) for j in sample_response["data"]]


def test_body_carries_watermark_and_exclusions(profile_dict):
    body = build_request_body(
        SearchProfile.model_validate(profile_dict),
        discovered_since="2026-07-26T12:00:00",
        exclude_ids=[1111, 3333],
    )
    assert body["discovered_at_gte"] == "2026-07-26T12:00:00"
    assert body["job_id_not"] == [1111, 3333]


def test_body_omits_them_on_a_first_run(profile_dict):
    body = build_request_body(SearchProfile.model_validate(profile_dict))
    assert "discovered_at_gte" not in body
    assert "job_id_not" not in body


def test_last_run_at_is_none_before_any_run(tmp_path):
    with Store(tmp_path / "jobs.db") as store:
        assert store.last_run_at("atlanta-ml") is None


def test_last_run_at_tracks_the_newest_run_per_profile(tmp_path):
    with Store(tmp_path / "jobs.db") as store:
        store.record_run("atlanta-ml", {}, 5, 5)
        store.record_run("other-profile", {}, 1, 1)
        first = store.last_run_at("atlanta-ml")

        store.record_run("atlanta-ml", {}, 3, 0)
        second = store.last_run_at("atlanta-ml")

        assert second >= first
        assert store.last_run_at("other-profile") != second


def test_known_job_ids_are_integers(tmp_path, sample_response):
    with Store(tmp_path / "jobs.db") as store:
        store.upsert_jobs(_jobs(sample_response), "atlanta-ml")
        ids = store.known_job_ids()

    assert set(ids) == {1111, 2222, 3333}
    assert all(isinstance(i, int) for i in ids), "job_id_not takes integers"


def test_known_job_ids_skip_non_numeric(tmp_path):
    """A non-integer id must not poison the exclusion list."""
    with Store(tmp_path / "jobs.db") as store:
        store.upsert_jobs([Job(id="not-a-number"), Job(id=42)], "p")
        assert store.known_job_ids() == [42]


def test_known_job_ids_are_bounded(tmp_path):
    """The list goes into the request body, so it cannot grow without limit."""
    with Store(tmp_path / "jobs.db") as store:
        store.upsert_jobs([Job(id=i) for i in range(50)], "p")
        assert len(store.known_job_ids(limit=10)) == 10


def test_known_job_ids_respect_the_since_window(tmp_path, sample_response):
    with Store(tmp_path / "jobs.db") as store:
        store.upsert_jobs(_jobs(sample_response), "atlanta-ml")

        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        assert store.known_job_ids(since=future) == []

        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert len(store.known_job_ids(since=past)) == 3


def test_incremental_can_be_turned_off_per_profile(profile_dict):
    profile_dict["incremental"] = False
    assert SearchProfile.model_validate(profile_dict).incremental is False


def test_incremental_defaults_on(profile_dict):
    assert SearchProfile.model_validate(profile_dict).incremental is True
