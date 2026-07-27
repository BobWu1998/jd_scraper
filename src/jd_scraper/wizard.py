"""Interactive builder for search profiles.

Writes profile YAML only -- it never calls the API, so it costs no credits and stays
correct even when the request-body field names in filters.py are corrected after
`jd probe`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
import yaml

from .models import SearchProfile

SENIORITY_CHOICES = ["junior", "mid_level", "senior", "staff", "c_level"]


def _ask_list(prompt: str, *, example: str = "", current: list[str] | None = None) -> list[str]:
    """Comma-separated input -> list. Empty input means 'no filter'."""
    if example and not current:
        prompt = f"{prompt} (comma separated, e.g. {example})"
    raw = typer.prompt(prompt, default=", ".join(current) if current else "")
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _ask_int(prompt: str, *, default: int | None = None) -> int | None:
    raw = typer.prompt(prompt, default="" if default is None else str(default))
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        typer.echo("  Not a number -- skipping this filter.")
        return None


def _ask_tristate(prompt: str, *, current: bool | None = None) -> bool | None:
    """yes / no / blank, where blank means 'do not filter on this'."""
    default = "" if current is None else ("y" if current else "n")
    raw = str(typer.prompt(f"{prompt} (y/n, blank for either)", default=default)).strip().lower()
    if raw in {"y", "yes", "true"}:
        return True
    if raw in {"n", "no", "false"}:
        return False
    return None


def build_profile_interactively(base: SearchProfile | None = None) -> SearchProfile:
    """Prompt for every filter, seeding defaults from `base` when editing."""
    if base is None:
        typer.echo("Build a search profile. Press Enter to skip any filter.\n")
        base = SearchProfile(name="my-search")
    else:
        typer.echo(f"Editing '{base.name}'. Press Enter to keep the current value.\n")

    name = typer.prompt("Profile name", default=base.name)

    titles = _ask_list(
        "Job titles to match",
        example="Machine Learning Engineer, ML Engineer",
        current=base.titles,
    )
    title_exclude = _ask_list(
        "Job titles to exclude", example="Intern, Manager", current=base.title_exclude
    )

    posted_within_days = _ask_int(
        "Only postings from the last N days",
        default=base.posted_within_days if base.posted_within_days is not None else 7,
    )

    countries = _ask_list("Country codes", example="US, GB", current=base.locations.countries)
    patterns = _ask_list(
        "Location matches", example="San Francisco, New York", current=base.locations.patterns
    )

    remote = _ask_tristate("Remote only?", current=base.remote)

    seniority = _ask_list(
        f"Seniority levels (of: {', '.join(SENIORITY_CHOICES)})",
        example="mid_level, senior",
        current=base.seniority,
    )
    unknown = [s for s in seniority if s not in SENIORITY_CHOICES]
    if unknown:
        typer.echo(f"  Note: {', '.join(unknown)} is not a known level; keeping it anyway.")

    min_salary = _ask_int("Minimum USD salary", default=base.min_salary_usd)

    companies_exclude = _ask_list(
        "Companies to exclude",
        example="Some Staffing Agency",
        current=base.companies_exclude,
    )

    linkedin_only = typer.confirm(
        "Restrict to LinkedIn postings?", default=base.sources.linkedin_only
    )

    limit = _ask_int(
        "Max results per run (TheirStack bills per job returned)", default=base.limit
    ) or base.limit

    data: dict[str, Any] = {
        "name": name,
        "titles": titles,
        "title_exclude": title_exclude,
        "locations": {"countries": countries, "patterns": patterns},
        "seniority": seniority,
        "companies_exclude": companies_exclude,
        "sources": {"linkedin_only": linkedin_only},
        "limit": limit,
        "page_size": min(base.page_size, limit),
        # Not prompted for -- carried through untouched so editing never silently
        # drops filters that were hand-written into the YAML.
        "companies": base.companies,
        "description_contains": base.description_contains,
        "extra": base.extra,
    }
    if posted_within_days is not None:
        data["posted_within_days"] = posted_within_days
    if remote is not None:
        data["remote"] = remote
    if min_salary is not None:
        data["min_salary_usd"] = min_salary

    # Validate before writing so a profile on disk is always loadable.
    return SearchProfile.model_validate(data)


def profile_to_yaml(profile: SearchProfile) -> str:
    data = profile.model_dump(exclude_defaults=False)
    # Drop empty filters so the written file shows only what is actually constraining.
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if key in {"name", "limit", "page_size", "sources", "extra"}:
            cleaned[key] = value
        elif value in (None, [], {}):
            continue
        elif key == "locations":
            inner = {k: v for k, v in value.items() if v}
            if inner:
                cleaned[key] = inner
        else:
            cleaned[key] = value
    return yaml.safe_dump(cleaned, sort_keys=False, allow_unicode=True)


def write_profile(profile: SearchProfile, path: str | Path) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(profile_to_yaml(profile), encoding="utf-8")
    return out
