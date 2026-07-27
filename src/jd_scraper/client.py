"""HTTP client for the TheirStack jobs API.

Endpoint and auth scheme are unverified -- see filters.BODY_FIELD_NOTES and `jd probe`.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .filters import apply_source_filter, build_request_body
from .models import Job, SearchProfile

SEARCH_PATH = "/v1/jobs/search"
OPENAPI_PATH = "/openapi.json"


class TheirStackError(RuntimeError):
    """An API call failed. Carries the body so filter mistakes are readable."""

    def __init__(self, status_code: int, body: str, *, url: str = "") -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"TheirStack {status_code} for {url or SEARCH_PATH}: {body[:800]}")


class RetryableError(TheirStackError):
    """429 or 5xx -- worth retrying."""


class TheirStackClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.theirstack.com",
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def __enter__(self) -> TheirStackClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @retry(
        retry=retry_if_exception_type(RetryableError),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(path, json=body)
        return self._handle(response, path)

    def _handle(self, response: httpx.Response, path: str) -> dict[str, Any]:
        if response.status_code == 429 or response.status_code >= 500:
            # Honor Retry-After when the server sends one; tenacity's backoff covers
            # the rest.
            raise RetryableError(response.status_code, response.text, url=path)
        if response.status_code >= 400:
            # Do NOT retry other 4xx: a bad filter name should fail loudly rather than
            # loop against a metered API.
            raise TheirStackError(response.status_code, response.text, url=path)
        return response.json()

    def search_page(self, profile: SearchProfile, page: int, page_size: int) -> dict[str, Any]:
        body = build_request_body(profile, page=page, page_size=page_size)
        return self._post(SEARCH_PATH, body)

    def search(
        self,
        profile: SearchProfile,
        *,
        max_results: int | None = None,
        on_page: Callable[[int, int], None] | None = None,
    ) -> tuple[list[Job], dict[str, Any]]:
        """Page through results, stopping at the credit cap.

        Returns (jobs, last_metadata). Source filtering is applied per page, but the
        cap counts jobs *returned by the API*, since that is what gets billed.
        """
        cap = max_results if max_results is not None else profile.limit
        collected: list[Job] = []
        billed = 0
        metadata: dict[str, Any] = {}
        page = 0

        while billed < cap:
            page_size = min(profile.page_size, cap - billed)
            payload = self.search_page(profile, page, page_size)
            metadata = payload.get("metadata", {}) or {}

            raw_jobs = payload.get("data") or []
            if not raw_jobs:
                break

            billed += len(raw_jobs)
            jobs = [Job.model_validate(j) for j in raw_jobs]
            collected.extend(apply_source_filter(jobs, profile))

            if on_page is not None:
                on_page(page, len(raw_jobs))

            # A short page means we have reached the end of the result set.
            if len(raw_jobs) < page_size:
                break
            page += 1

        return collected, metadata

    def fetch_openapi(self) -> dict[str, Any]:
        response = self._client.get(OPENAPI_PATH)
        return self._handle(response, OPENAPI_PATH)

    def raw_search(self, body: dict[str, Any]) -> dict[str, Any]:
        """Send a hand-built body. Used by `jd probe`."""
        return self._post(SEARCH_PATH, body)


def format_body(body: dict[str, Any]) -> str:
    return json.dumps(body, indent=2, sort_keys=True)
