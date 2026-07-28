"""Shared search execution for the CLI and the web UI.

Both entry points funnel through `run_search` so the credit guards -- incremental
watermarking, the result cap, query-variant budgeting -- cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .client import TheirStackClient
from .config import Settings
from .filters import build_request_body, expand_queries
from .models import Job, SearchProfile
from .store import Store


@dataclass
class SearchOutcome:
    jobs: list[Job] = field(default_factory=list)
    billed: int = 0
    """Jobs the API returned. This is what was charged -- 1 credit each."""
    stored: int = 0
    new: int = 0
    truncated: int = 0
    """Matches withheld because the account lacked credits to fetch them."""
    discovered_since: str | None = None
    excluded_ids: int = 0
    queries: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    preview: bool = False


def incremental_window(
    profile: SearchProfile, settings: Settings, *, disabled: bool = False
) -> tuple[str | None, list[int]]:
    """What this profile has already seen: (discovered_since, ids to exclude).

    The timestamp is rewound by the profile's overlap so a posting indexed while
    the previous run was in flight is not skipped; the id list removes the
    duplicates that overlap would otherwise let back in.
    """
    if disabled or not profile.incremental:
        return None, []

    db_path = Path(settings.jd_db_path)
    if not db_path.exists():
        return None, []

    with Store(db_path) as store:
        last_run = store.last_run_at(profile.name)
        if not last_run:
            return None, []
        try:
            last = datetime.fromisoformat(last_run)
        except ValueError:
            return None, []
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)

        since = last - timedelta(minutes=profile.incremental_overlap_minutes)
        stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        return stamp, store.known_job_ids(since=since.isoformat())


def plan_search(
    profile: SearchProfile,
    settings: Settings,
    *,
    max_results: int | None = None,
    preview: bool | None = None,
    totals: bool = False,
    full: bool = False,
) -> dict[str, Any]:
    """Everything a run *would* do, without calling the API. Costs nothing."""
    cap = max_results if max_results is not None else profile.limit
    discovered_since, exclude_ids = incremental_window(profile, settings, disabled=full)
    variants = expand_queries(profile)
    share = max(1, cap // len(variants))

    bodies = []
    for index, (label, variant) in enumerate(variants):
        budget = cap - (share * index) if index == len(variants) - 1 else share
        bodies.append(
            {
                "label": label or "search",
                "budget": budget,
                "body": build_request_body(
                    variant,
                    page=0,
                    page_size=min(variant.page_size, max(budget, 1)),
                    preview=preview if preview is not None else None,
                    include_total_results=totals or None,
                    discovered_since=discovered_since,
                    exclude_ids=exclude_ids,
                ),
            }
        )

    effective_preview = profile.preview if preview is None else preview
    return {
        "cap": cap,
        "preview": effective_preview,
        "max_credits": 0 if effective_preview else cap,
        "discovered_since": discovered_since,
        "excluded_ids": len(exclude_ids),
        "queries": bodies,
    }


def run_search(
    profile: SearchProfile,
    settings: Settings,
    *,
    max_results: int | None = None,
    preview: bool | None = None,
    totals: bool = False,
    full: bool = False,
    store_results: bool = True,
    on_event: Callable[[str], None] | None = None,
) -> SearchOutcome:
    """Execute a search and (optionally) persist it.

    Splits into query variants when the profile ORs location with remote, budgets
    the cap across them so neither starves the other, and deduplicates jobs that
    match more than one variant.
    """
    api_key = settings.require_api_key()
    cap = max_results if max_results is not None else profile.limit
    discovered_since, exclude_ids = incremental_window(profile, settings, disabled=full)
    variants = expand_queries(profile)

    def emit(message: str) -> None:
        if on_event:
            on_event(message)

    outcome = SearchOutcome(
        discovered_since=discovered_since,
        excluded_ids=len(exclude_ids),
        preview=profile.preview if preview is None else preview,
    )
    seen: set = set()

    with TheirStackClient(
        api_key,
        base_url=settings.theirstack_base_url,
        timeout=settings.jd_timeout_seconds,
    ) as client:
        share = max(1, cap // len(variants))
        for index, (label, variant) in enumerate(variants):
            budget = cap - (share * index) if index == len(variants) - 1 else share
            if budget < 1:
                break
            outcome.queries.append(label or "search")
            emit(f"{label or 'search'}: up to {budget} results")

            found, meta = client.search(
                variant,
                max_results=budget,
                preview=preview,
                include_total_results=totals if index == 0 else False,
                discovered_since=discovered_since,
                exclude_ids=exclude_ids,
                on_page=lambda page, count: emit(f"  page {page}: {count} results"),
            )
            outcome.billed += meta.get("billed_results", 0)
            outcome.metadata = {**meta, **outcome.metadata}
            for job in found:
                if job.id is not None:
                    if job.id in seen:
                        continue
                    seen.add(job.id)
                outcome.jobs.append(job)

    outcome.truncated = outcome.metadata.get("truncated_results") or 0

    if store_results:
        body_for_log = build_request_body(
            profile,
            page=0,
            page_size=min(profile.page_size, cap),
            preview=preview,
            discovered_since=discovered_since,
            exclude_ids=exclude_ids,
        )
        with Store(settings.jd_db_path) as store:
            outcome.stored, outcome.new = store.upsert_jobs(outcome.jobs, profile.name)
            store.record_run(profile.name, body_for_log, outcome.stored, outcome.new)

    return outcome
