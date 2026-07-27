"""Profile (input) and Job (output) models.

The Job model is deliberately permissive: TheirStack's exact response schema has not
been verified against a live call, so unknown fields are preserved rather than dropped
and every field is optional. Nothing the API returns should be able to crash a run.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Locations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    countries: list[str] = Field(default_factory=list)
    """ISO-2 country codes, e.g. ["US", "GB"]."""

    patterns: list[str] = Field(default_factory=list)
    """Free-text location matches, e.g. ["San Francisco", "New York"]."""


class Sources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    linkedin_only: bool = True


class SearchProfile(BaseModel):
    """A saved search, loaded from YAML."""

    model_config = ConfigDict(extra="forbid")

    name: str

    titles: list[str] = Field(default_factory=list)
    title_exclude: list[str] = Field(default_factory=list)

    posted_within_days: int | None = None

    locations: Locations = Field(default_factory=Locations)
    remote: bool | None = None

    seniority: list[str] = Field(default_factory=list)
    min_salary_usd: int | None = None
    max_salary_usd: int | None = None

    companies: list[str] = Field(default_factory=list)
    companies_exclude: list[str] = Field(default_factory=list)

    description_contains: list[str] = Field(default_factory=list)

    sources: Sources = Field(default_factory=Sources)

    limit: int = 100
    """Hard ceiling on total results fetched for this profile. Guards credit spend."""

    page_size: int = 25
    """Results per API request."""

    extra: dict[str, Any] = Field(default_factory=dict)
    """Raw pass-through, merged last into the request body.

    Escape hatch for any TheirStack filter this tool does not model yet.
    """

    @model_validator(mode="after")
    def _check_limits(self) -> SearchProfile:
        if self.limit < 1:
            raise ValueError("limit must be at least 1")
        if self.page_size < 1:
            raise ValueError("page_size must be at least 1")
        return self

    @classmethod
    def from_yaml(cls, path: str) -> SearchProfile:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a YAML mapping at the top level")
        return cls.model_validate(data)


class Job(BaseModel):
    """A single posting.

    Every field is optional and unknown fields are kept -- see module docstring.
    """

    model_config = ConfigDict(extra="allow")

    id: str | int | None = None
    job_title: str | None = None
    company: str | None = None

    url: str | None = None
    final_url: str | None = None
    source_url: str | None = None
    source: str | None = None

    location: str | None = None
    short_location: str | None = None
    long_location: str | None = None
    country_code: str | None = None
    remote: bool | None = None
    hybrid: bool | None = None

    date_posted: str | None = None
    discovered_at: str | None = None

    seniority: str | None = None
    salary_string: str | None = None
    min_annual_salary_usd: float | None = None
    max_annual_salary_usd: float | None = None

    description: str | None = None

    def best_url(self) -> str | None:
        return self.url or self.final_url or self.source_url

    def best_location(self) -> str | None:
        return self.location or self.short_location or self.long_location
