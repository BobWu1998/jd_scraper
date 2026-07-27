"""HTTP client for POST /v1/jobs/search."""

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
    """An API call failed. Carries the parsed error envelope when there is one."""

    def __init__(self, status_code: int, body: str, *, url: str = "") -> None:
        self.status_code = status_code
        self.body = body
        self.url = url or SEARCH_PATH
        self.title, self.code, self.description = self._parse(body)
        super().__init__(self._message())

    @staticmethod
    def _parse(body: str) -> tuple[str | None, str | None, str | None]:
        try:
            error = (json.loads(body) or {}).get("error") or {}
        except (ValueError, AttributeError):
            return None, None, None
        return error.get("title"), error.get("code"), error.get("description")

    def _message(self) -> str:
        parts = [f"TheirStack {self.status_code} for {self.url}"]
        if self.title:
            parts.append(f"{self.code + ': ' if self.code else ''}{self.title}")
            if self.description:
                parts.append(self.description)
        else:
            parts.append(self.body[:800])
        return " -- ".join(parts)


class RetryableError(TheirStackError):
    """429 or 5xx -- worth retrying."""


class OutOfCreditsError(TheirStackError):
    """402 -- the account cannot pay for this request."""


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
        return self._handle(self._client.post(path, json=body), path)

    def _handle(self, response: httpx.Response, path: str) -> dict[str, Any]:
        status = response.status_code
        if status == 429 or status >= 500:
            raise RetryableError(status, response.text, url=path)
        if status == 402:
            raise OutOfCreditsError(status, response.text, url=path)
        if status >= 400:
            # 400/422 mean a malformed filter. Retrying would burn credits without
            # ever succeeding, so fail loudly instead.
            raise TheirStackError(status, response.text, url=path)
        return response.json()

    def search_page(
        self,
        profile: SearchProfile,
        page: int,
        page_size: int,
        *,
        include_total_results: bool | None = None,
        preview: bool | None = None,
    ) -> dict[str, Any]:
        body = build_request_body(
            profile,
            page=page,
            page_size=page_size,
            include_total_results=include_total_results,
            preview=preview,
        )
        return self._post(SEARCH_PATH, body)

    def search(
        self,
        profile: SearchProfile,
        *,
        max_results: int | None = None,
        preview: bool | None = None,
        include_total_results: bool | None = None,
        on_page: Callable[[int, int], None] | None = None,
    ) -> tuple[list[Job], dict[str, Any]]:
        """Page through results, stopping at the credit cap.

        Returns (jobs, metadata). The cap counts jobs returned by the API, since that
        is the billed quantity -- not the smaller number left after source filtering.
        """
        cap = max_results if max_results is not None else profile.limit
        collected: list[Job] = []
        billed = 0
        metadata: dict[str, Any] = {}
        page = 0

        while billed < cap:
            page_size = min(profile.page_size, cap - billed)
            payload = self.search_page(
                profile,
                page,
                page_size,
                # Totals are expensive; ask only on the first page.
                include_total_results=include_total_results if page == 0 else False,
                preview=preview,
            )
            page_metadata = payload.get("metadata") or {}
            metadata = {**page_metadata, **metadata} if page else page_metadata

            raw_jobs = payload.get("data") or []
            if not raw_jobs:
                break

            billed += len(raw_jobs)
            jobs = [Job.model_validate(j) for j in raw_jobs]
            collected.extend(apply_source_filter(jobs, profile))

            if on_page is not None:
                on_page(page, len(raw_jobs))

            if len(raw_jobs) < page_size:
                break
            page += 1

        metadata["billed_results"] = billed
        return collected, metadata

    def fetch_openapi(self) -> dict[str, Any]:
        return self._handle(self._client.get(OPENAPI_PATH), OPENAPI_PATH)

    def raw_search(self, body: dict[str, Any]) -> dict[str, Any]:
        """Send a hand-built body. Used by `jd probe`."""
        return self._post(SEARCH_PATH, body)


def format_body(body: dict[str, Any]) -> str:
    return json.dumps(body, indent=2, sort_keys=True)
