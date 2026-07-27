"""Translate a SearchProfile into a TheirStack request body, and enforce LinkedIn-only.

IMPORTANT: the request-body field names below are unverified -- they were written
without access to TheirStack's live API or OpenAPI spec. Run `jd probe` against the
real API and correct BODY_FIELD_NOTES / build_request_body from its output.

The `extra` pass-through on SearchProfile exists precisely so a wrong guess here does
not block you: any filter can be supplied raw from the profile YAML.
"""

from __future__ import annotations

from typing import Any

from .models import Job, SearchProfile

LINKEDIN_URL_MARKER = "linkedin.com"
LINKEDIN_SOURCE_MARKER = "linkedin"

BODY_FIELD_NOTES = """\
Assumed TheirStack /v1/jobs/search body fields (UNVERIFIED):
  job_title_or, job_title_not          <- titles, title_exclude
  posted_at_max_age_days               <- posted_within_days
  job_country_code_or                  <- locations.countries
  job_location_pattern_or              <- locations.patterns
  remote                               <- remote
  job_seniority_or                     <- seniority
  min_salary_usd / max_salary_usd      <- min/max_salary_usd
  company_name_or / company_name_not   <- companies / companies_exclude
  job_description_pattern_or           <- description_contains
  page, limit, order_by
"""


class EmptyFilterError(ValueError):
    """Raised when a profile would produce a search with no filters at all.

    TheirStack rejects unfiltered searches, and an accidental match-everything query
    against a per-result-billed API is worth catching before it leaves the machine.
    """


def build_request_body(
    profile: SearchProfile,
    *,
    page: int = 0,
    page_size: int | None = None,
) -> dict[str, Any]:
    """Map a profile onto a request body for one page of results."""
    body: dict[str, Any] = {
        "page": page,
        "limit": page_size if page_size is not None else profile.page_size,
        "order_by": [{"field": "date_posted", "desc": True}],
    }

    if profile.titles:
        body["job_title_or"] = list(profile.titles)
    if profile.title_exclude:
        body["job_title_not"] = list(profile.title_exclude)

    if profile.posted_within_days is not None:
        body["posted_at_max_age_days"] = profile.posted_within_days

    if profile.locations.countries:
        body["job_country_code_or"] = list(profile.locations.countries)
    if profile.locations.patterns:
        body["job_location_pattern_or"] = list(profile.locations.patterns)

    if profile.remote is not None:
        body["remote"] = profile.remote

    if profile.seniority:
        body["job_seniority_or"] = list(profile.seniority)

    if profile.min_salary_usd is not None:
        body["min_salary_usd"] = profile.min_salary_usd
    if profile.max_salary_usd is not None:
        body["max_salary_usd"] = profile.max_salary_usd

    if profile.companies:
        body["company_name_or"] = list(profile.companies)
    if profile.companies_exclude:
        body["company_name_not"] = list(profile.companies_exclude)

    if profile.description_contains:
        body["job_description_pattern_or"] = list(profile.description_contains)

    _assert_has_filters(body)

    # Pass-through wins over everything above, so a wrong guess can always be overridden.
    body.update(profile.extra)
    return body


def _assert_has_filters(body: dict[str, Any]) -> None:
    scaffolding = {"page", "limit", "order_by"}
    if not (set(body) - scaffolding):
        raise EmptyFilterError(
            "This profile has no filters -- it would request every job in the database. "
            "Set at least one of: titles, posted_within_days, locations, companies, "
            "seniority, or a salary bound."
        )


def is_linkedin(job: Job) -> bool:
    """True if the posting looks LinkedIn-sourced.

    Checked across every url-ish field because which one carries the origin is not
    verified. This client-side check is what actually enforces linkedin_only -- any
    request-side source filter is a best-effort optimisation on top.
    """
    urls = [job.url, job.final_url, job.source_url]
    if any(LINKEDIN_URL_MARKER in u.lower() for u in urls if u):
        return True
    # A `source`/board field is likely a bare name ("linkedin"), not a domain.
    return bool(job.source) and LINKEDIN_SOURCE_MARKER in job.source.lower()


def apply_source_filter(jobs: list[Job], profile: SearchProfile) -> list[Job]:
    if not profile.sources.linkedin_only:
        return jobs
    return [j for j in jobs if is_linkedin(j)]
