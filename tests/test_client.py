"""Client behaviour against a mock transport.

These prove internal consistency only. The fixture was hand-written from an assumed
schema, so a green suite does NOT prove agreement with the real TheirStack API --
only `jd probe` establishes that.
"""

import httpx
import pytest

from jd_scraper.client import RetryableError, TheirStackClient, TheirStackError
from jd_scraper.models import SearchProfile


def _client(handler) -> TheirStackClient:
    return TheirStackClient("test-key", transport=httpx.MockTransport(handler))


def test_sends_bearer_auth_and_stops_at_end(sample_response, profile_dict):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=sample_response)

    profile = SearchProfile.model_validate(profile_dict)
    with _client(handler) as client:
        jobs, metadata = client.search(profile, max_results=10)

    assert seen["auth"] == "Bearer test-key"
    # 3 results on a page of 5 means a short page -> stop after one request.
    assert metadata["total_results"] == 3
    assert metadata["billed_results"] == 3, "the cap counts what the API billed for"
    # linkedin_only defaults to True, so the greenhouse posting is dropped.
    assert [j.id for j in jobs] == [1111, 3333]


def test_paginates_until_cap(profile_dict):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        calls.append(body)
        page_size = body["limit"]
        data = [
            {"id": f"job-{body['page']}-{i}", "url": "https://linkedin.com/jobs/view/1"}
            for i in range(page_size)
        ]
        return httpx.Response(200, json={"metadata": {}, "data": data})

    profile_dict.update({"limit": 12, "page_size": 5})
    profile = SearchProfile.model_validate(profile_dict)
    with _client(handler) as client:
        jobs, _ = client.search(profile)

    assert [c["page"] for c in calls] == [0, 1, 2]
    # Final page is trimmed so the cap is never exceeded: 5 + 5 + 2.
    assert [c["limit"] for c in calls] == [5, 5, 2]
    assert len(jobs) == 12


def test_empty_page_ends_pagination(profile_dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"metadata": {}, "data": []})

    with _client(handler) as client:
        jobs, _ = client.search(SearchProfile.model_validate(profile_dict))
    assert jobs == []


def test_retries_429_then_succeeds(sample_response, profile_dict):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, text="rate limited")
        return httpx.Response(200, json=sample_response)

    with _client(handler) as client:
        jobs, _ = client.search(SearchProfile.model_validate(profile_dict))

    assert attempts["n"] == 2
    assert len(jobs) == 2


def test_400_fails_immediately_without_retry(profile_dict):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, text='{"detail":"unknown field job_title_or"}')

    with _client(handler) as client:
        with pytest.raises(TheirStackError) as exc:
            client.search(SearchProfile.model_validate(profile_dict))

    assert attempts["n"] == 1, "a bad filter must not be retried against a metered API"
    assert not isinstance(exc.value, RetryableError)
    assert "unknown field" in exc.value.body


def test_server_error_is_retryable(profile_dict):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(503, text="upstream down")

    with _client(handler) as client:
        with pytest.raises(RetryableError):
            client.search(SearchProfile.model_validate(profile_dict))

    assert attempts["n"] == 4, "should exhaust the configured retry attempts"
