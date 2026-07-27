"""Translate a SearchProfile into a TheirStack request body.

Field names here are verified against the published API reference for
POST /v1/jobs/search (job search endpoint).

Two API rules drive the design:

1. At least one of MANDATORY_FILTERS must be present or the request fails. That is
   validated locally, before the request leaves the machine.
2. The endpoint bills 1 credit per job *returned*, so filtering happens server-side
   wherever possible -- notably `url_domain_or` for LinkedIn-only searches, which
   avoids paying for postings we would otherwise discard on arrival.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .models import Job, Locations, SearchProfile

LINKEDIN_DOMAIN = "linkedin.com"

# The API rejects a search unless at least one of these is set, for performance reasons.
MANDATORY_FILTERS = (
    "posted_at_max_age_days",
    "posted_at_gte",
    "posted_at_lte",
    "company_domain_or",
    "company_linkedin_url_or",
    "company_name_or",
)

SENIORITY_CHOICES = ("c_level", "staff", "senior", "junior", "mid_level")

ORDER_BY_DEFAULT = [
    {"field": "date_posted", "desc": True},
    {"field": "discovered_at", "desc": True},
]


class MissingMandatoryFilterError(ValueError):
    """The profile lacks every filter the API requires."""


def build_request_body(
    profile: SearchProfile,
    *,
    page: int = 0,
    page_size: int | None = None,
    include_total_results: bool | None = None,
    preview: bool | None = None,
    discovered_since: str | None = None,
    exclude_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Map a profile onto a request body for one page of results.

    `discovered_since` and `exclude_ids` drive incremental fetching: they push
    already-seen postings out of the result set server-side, so credits are not
    spent re-fetching rows already in the database.
    """
    body: dict[str, Any] = {
        "page": page,
        "limit": page_size if page_size is not None else profile.page_size,
        "order_by": ORDER_BY_DEFAULT,
    }

    # --- job title -------------------------------------------------------------
    # job_title_or is keyword-based, not exact: every word in a pattern must appear
    # in the title, in any order, case-insensitively. "machine learning engineer"
    # therefore also matches "Senior Machine Learning Engineer, Platform".
    if profile.titles:
        body["job_title_or"] = list(profile.titles)
    if profile.title_exclude:
        body["job_title_not"] = list(profile.title_exclude)
    if profile.title_pattern:
        body["job_title_pattern_or"] = list(profile.title_pattern)

    # --- posting date ----------------------------------------------------------
    if profile.posted_within_days is not None:
        body["posted_at_max_age_days"] = profile.posted_within_days
    if profile.posted_after:
        body["posted_at_gte"] = profile.posted_after
    if profile.posted_before:
        body["posted_at_lte"] = profile.posted_before

    # --- location --------------------------------------------------------------
    if profile.locations.countries:
        body["job_country_code_or"] = list(profile.locations.countries)
    if profile.locations.patterns:
        # Deprecated upstream in favour of job_location_or with geoname IDs, but it
        # needs no catalog lookup so it stays the ergonomic default.
        body["job_location_pattern_or"] = list(profile.locations.patterns)
    if profile.locations.ids:
        body["job_location_or"] = [{"id": i} for i in profile.locations.ids]

    if profile.remote is not None:
        body["remote"] = profile.remote

    # --- role attributes -------------------------------------------------------
    if profile.seniority:
        body["job_seniority_or"] = list(profile.seniority)
    if profile.employment_types:
        body["employment_statuses_or"] = list(profile.employment_types)
    if profile.min_salary_usd is not None:
        body["min_salary_usd"] = profile.min_salary_usd
    if profile.max_salary_usd is not None:
        body["max_salary_usd"] = profile.max_salary_usd

    # --- company ---------------------------------------------------------------
    if profile.companies:
        body["company_name_or"] = list(profile.companies)
    if profile.companies_exclude:
        body["company_name_not"] = list(profile.companies_exclude)
    if profile.exclude_recruiting_agencies:
        body["company_type"] = "direct_employer"

    # --- description -----------------------------------------------------------
    if profile.description_contains:
        # Whole-word match, so "quality" does not hit "inequality".
        body["job_description_contains_or"] = list(profile.description_contains)
    if profile.description_pattern:
        body["job_description_pattern_or"] = list(profile.description_pattern)
    if profile.description_exclude:
        body["job_description_contains_not"] = list(profile.description_exclude)

    # --- source ----------------------------------------------------------------
    domains = profile.source_domains()
    if domains:
        body["url_domain_or"] = domains

    # --- incremental fetching --------------------------------------------------
    # discovered_at is when TheirStack found the posting, which is the right clock
    # for "what is new since I last looked" -- date_posted can predate discovery.
    if discovered_since:
        body["discovered_at_gte"] = discovered_since
    if exclude_ids:
        body["job_id_not"] = list(exclude_ids)

    # --- request modes ---------------------------------------------------------
    if include_total_results is None:
        include_total_results = profile.include_total_results
    if include_total_results:
        # Costs a full-dataset read, so it is off unless explicitly asked for.
        body["include_total_results"] = True

    if preview is None:
        preview = profile.preview
    if preview:
        # Blurred results are free -- no credits consumed.
        body["blur_company_data"] = True

    # Pass-through wins over everything above.
    body.update(profile.extra)

    _assert_mandatory_filter(body)
    return body


def _assert_mandatory_filter(body: dict[str, Any]) -> None:
    if any(body.get(field) not in (None, [], "") for field in MANDATORY_FILTERS):
        return
    raise MissingMandatoryFilterError(
        "TheirStack requires at least one of these filters, and this profile sets none:\n"
        "  posted_within_days  (-> posted_at_max_age_days)\n"
        "  posted_after        (-> posted_at_gte)\n"
        "  posted_before       (-> posted_at_lte)\n"
        "  companies           (-> company_name_or)\n"
        "Add one -- posted_within_days: 7 is the usual choice. A title or location "
        "filter alone is not enough; the API rejects it for performance reasons."
    )


def expand_queries(profile: SearchProfile) -> list[tuple[str, SearchProfile]]:
    """Split a profile into the searches needed to express it.

    The API ANDs every filter, so an OR across two different filter dimensions --
    "in Atlanta OR remote anywhere" -- cannot be a single request: location is
    matched on the job's location text, remote is a separate boolean. When
    `include_remote` is set we issue one location-scoped search and one remote
    search, and the caller unions the results.

    Returns (label, profile) pairs. A single-element list is the normal case.
    """
    if not profile.include_remote:
        return [("", profile)]

    has_location = bool(profile.locations.patterns or profile.locations.ids)
    if not has_location or profile.remote is not None:
        # Nothing to widen: either there is no location filter to OR against, or
        # `remote` was set explicitly and the caller means the narrower AND.
        return [("", profile)]

    located = profile.model_copy(deep=True)
    located.include_remote = False

    remote = profile.model_copy(deep=True)
    remote.include_remote = False
    remote.remote = True
    # Drop the location text filter -- keeping it would AND right back down to
    # "remote jobs whose location says Atlanta", which is the bug this fixes.
    # Country is kept so a US search stays a US search.
    remote.locations = Locations(countries=list(profile.locations.countries))

    return [("location", located), ("remote", remote)]


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url if "//" in url else f"//{url}").netloc.lower()
    return host[4:] if host.startswith("www.") else host or None


def is_linkedin(job: Job) -> bool:
    """True if the posting came from LinkedIn.

    The response has no `source` field, so this reads the URL fields. It backstops
    the server-side `url_domain_or` filter rather than replacing it.
    """
    for url in (job.url, job.source_url, job.final_url):
        host = domain_of(url)
        if host and (host == LINKEDIN_DOMAIN or host.endswith(f".{LINKEDIN_DOMAIN}")):
            return True
    return False


def apply_source_filter(jobs: list[Job], profile: SearchProfile) -> list[Job]:
    """Client-side safety net. The server-side filter should make this a no-op."""
    if not profile.sources.linkedin_only:
        return jobs
    return [j for j in jobs if is_linkedin(j)]
