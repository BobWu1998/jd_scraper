"""Profile (input) and Job (output) models.

Job fields mirror the documented response schema for POST /v1/jobs/search, but the
model stays permissive: unknown fields are preserved and everything is optional, so a
schema change upstream degrades the output rather than crashing the run.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

SENIORITY_CHOICES = ("c_level", "staff", "senior", "junior", "mid_level")

EMPLOYMENT_TYPES = (
    "full_time",
    "part_time",
    "temporary",
    "internship",
    "contract",
    "freelance",
    "co_founder",
    "apprenticeship",
    "seasonal",
    "volunteer",
    "other",
)


class Locations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    countries: list[str] = Field(default_factory=list)
    """ISO-2 country codes, e.g. ["US", "GB"]."""

    patterns: list[str] = Field(default_factory=list)
    """Case-insensitive regex matched against the job location."""

    ids: list[int] = Field(default_factory=list)
    """Geoname location IDs from the API's locations catalog. More precise than
    patterns, at the cost of a catalog lookup."""


class Sources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    linkedin_only: bool = True
    domains: list[str] = Field(default_factory=list)
    """Extra URL domains to accept, e.g. ["greenhouse.io"]. Combined with
    linkedin_only rather than replacing it."""


class SearchProfile(BaseModel):
    """A saved search, loaded from YAML."""

    model_config = ConfigDict(extra="forbid")

    name: str

    titles: list[str] = Field(default_factory=list)
    """Keyword patterns. All words in a pattern must appear in the title, in any
    order, case-insensitively."""
    title_exclude: list[str] = Field(default_factory=list)
    title_pattern: list[str] = Field(default_factory=list)
    """Regex alternative to `titles`, for when keyword matching is too loose."""

    posted_within_days: int | None = None
    posted_after: str | None = None
    """ISO date, yyyy-mm-dd."""
    posted_before: str | None = None

    locations: Locations = Field(default_factory=Locations)
    remote: bool | None = None

    seniority: list[str] = Field(default_factory=list)
    employment_types: list[str] = Field(default_factory=list)
    min_salary_usd: int | None = None
    max_salary_usd: int | None = None

    companies: list[str] = Field(default_factory=list)
    """Exact, case-sensitive company names."""
    companies_exclude: list[str] = Field(default_factory=list)
    exclude_recruiting_agencies: bool = False

    description_contains: list[str] = Field(default_factory=list)
    """Whole-word match: "quality" will not match "inequality"."""
    description_exclude: list[str] = Field(default_factory=list)
    description_pattern: list[str] = Field(default_factory=list)
    """Regex. Prefix with (?i) for case-insensitive."""

    sources: Sources = Field(default_factory=Sources)

    limit: int = 100
    """Hard ceiling on results fetched per run. 1 credit is billed per job returned."""

    page_size: int = 25

    include_total_results: bool = False
    """Ask the API for total counts. Significantly slower -- it reads the whole
    dataset -- so enable it for a first request, not for every page."""

    preview: bool = False
    """Blurred, credit-free results. Unavailable when filtering by company."""

    extra: dict[str, Any] = Field(default_factory=dict)
    """Raw pass-through, merged last into the request body."""

    @model_validator(mode="after")
    def _validate(self) -> SearchProfile:
        if self.limit < 1:
            raise ValueError("limit must be at least 1")
        if self.page_size < 1:
            raise ValueError("page_size must be at least 1")

        unknown = [s for s in self.seniority if s not in SENIORITY_CHOICES]
        if unknown:
            raise ValueError(
                f"unknown seniority {unknown}; valid values are {list(SENIORITY_CHOICES)}"
            )
        unknown = [e for e in self.employment_types if e not in EMPLOYMENT_TYPES]
        if unknown:
            raise ValueError(
                f"unknown employment type {unknown}; valid values are {list(EMPLOYMENT_TYPES)}"
            )

        if self.preview and self.companies:
            # The API rejects blurred mode alongside company identifier filters.
            raise ValueError(
                "preview mode cannot be combined with a `companies` filter -- the API "
                "does not allow blurred results when filtering by company identifier."
            )
        return self

    def source_domains(self) -> list[str]:
        """URL domains to restrict the search to, for `url_domain_or`."""
        domains: list[str] = []
        if self.sources.linkedin_only:
            domains.append("linkedin.com")
        for domain in self.sources.domains:
            if domain not in domains:
                domains.append(domain)
        return domains

    @classmethod
    def from_yaml(cls, path: str) -> SearchProfile:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a YAML mapping at the top level")
        return cls.model_validate(data)


class Job(BaseModel):
    """A single posting. Permissive by design -- see module docstring."""

    model_config = ConfigDict(extra="allow")

    id: int | str | None = None
    job_title: str | None = None

    company: str | None = None
    """Deprecated upstream; prefer company_name(), which reads company_object."""
    company_object: dict[str, Any] | None = None
    company_domain: str | None = None

    url: str | None = None
    final_url: str | None = None
    source_url: str | None = None

    location: str | None = None
    short_location: str | None = None
    long_location: str | None = None
    country: str | None = None
    country_code: str | None = None
    cities: list[str] | None = None
    remote: bool | None = None
    hybrid: bool | None = None

    date_posted: str | None = None
    date_reposted: str | None = None
    discovered_at: str | None = None
    closed_at: str | None = None

    seniority: str | None = None
    employment_statuses: list[str] | None = None
    easy_apply: bool | None = None

    salary_string: str | None = None
    salary_currency: str | None = None
    min_annual_salary: float | None = None
    max_annual_salary: float | None = None
    min_annual_salary_usd: float | None = None
    max_annual_salary_usd: float | None = None

    description: str | None = None
    technology_slugs: list[str] | None = None
    has_blurred_data: bool | None = None

    def best_url(self) -> str | None:
        return self.url or self.source_url or self.final_url

    def best_location(self) -> str | None:
        return self.short_location or self.location or self.long_location

    def company_name(self) -> str | None:
        if self.company_object and self.company_object.get("name"):
            return str(self.company_object["name"])
        return self.company
