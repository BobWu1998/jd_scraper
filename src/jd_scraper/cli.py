"""Command line interface."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .client import TheirStackClient, TheirStackError, format_body
from .config import load_settings
from .export import write_csv, write_jsonl
from .filters import BODY_FIELD_NOTES, build_request_body
from .models import SearchProfile
from .store import Store

app = typer.Typer(help="Pull LinkedIn job postings via the TheirStack API.", no_args_is_help=True)
console = Console()


def _load_profile(path: str) -> SearchProfile:
    try:
        return SearchProfile.from_yaml(path)
    except Exception as exc:
        console.print(f"[red]Could not load profile {path}:[/red] {exc}")
        raise typer.Exit(2)


@app.command()
def search(
    profile: str = typer.Option(..., "--profile", "-p", help="Path to a profile YAML."),
    max_results: Optional[int] = typer.Option(
        None, "--max-results", "-n", help="Override the profile's result cap."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the request body and exit. No API call, no credits."
    ),
    only_new: bool = typer.Option(
        False, "--only-new", help="Only show postings not seen in a previous run."
    ),
    no_store: bool = typer.Option(False, "--no-store", help="Do not write to the database."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the credit confirmation prompt."),
) -> None:
    """Run a saved search against TheirStack."""
    settings = load_settings()
    prof = _load_profile(profile)
    cap = max_results if max_results is not None else prof.limit

    if dry_run:
        body = build_request_body(prof, page=0, page_size=min(prof.page_size, cap))
        console.print(f"[bold]Request body[/bold] (page 0 of up to {cap} results):")
        console.print_json(format_body(body))
        console.print(f"\n[dim]{BODY_FIELD_NOTES}[/dim]")
        return

    # TheirStack bills per job returned -- confirm before a large pull.
    if cap > settings.jd_confirm_threshold and not yes:
        confirmed = typer.confirm(
            f"This will request up to {cap} jobs, which consumes TheirStack credits. Continue?"
        )
        if not confirmed:
            console.print("Aborted.")
            raise typer.Exit(1)

    try:
        api_key = settings.require_api_key()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)

    run_started = datetime.now(timezone.utc).isoformat()
    body_for_log = build_request_body(prof, page=0, page_size=min(prof.page_size, cap))

    def _progress(page: int, count: int) -> None:
        console.print(f"[dim]page {page}: {count} results[/dim]")

    try:
        with TheirStackClient(
            api_key,
            base_url=settings.theirstack_base_url,
            timeout=settings.jd_timeout_seconds,
        ) as client:
            jobs, metadata = client.search(prof, max_results=cap, on_page=_progress)
    except TheirStackError as exc:
        console.print(f"[red]API call failed:[/red] {exc}")
        console.print(f"\n[dim]{BODY_FIELD_NOTES}[/dim]")
        raise typer.Exit(1)

    if prof.sources.linkedin_only:
        console.print(f"[dim]LinkedIn-only filter kept {len(jobs)} postings.[/dim]")

    total_new = 0
    if not no_store:
        with Store(settings.jd_db_path) as store:
            total, total_new = store.upsert_jobs(jobs, prof.name)
            store.record_run(prof.name, body_for_log, total, total_new)
            rows = store.list_jobs(
                limit=len(jobs) or 1,
                profile=prof.name,
                new_only_since=run_started if only_new else None,
            )
        _render(rows)
        console.print(f"\n[green]{total} stored, {total_new} new.[/green]")
        if metadata.get("total_results") is not None:
            console.print(f"[dim]API reported total_results={metadata['total_results']}[/dim]")
    else:
        table = Table(show_header=True, header_style="bold")
        for col in ("Title", "Company", "Location", "Posted", "URL"):
            table.add_column(col, overflow="fold")
        for job in jobs:
            table.add_row(
                job.job_title or "-",
                job.company or "-",
                job.best_location() or "-",
                job.date_posted or "-",
                job.best_url() or "-",
            )
        console.print(table)
        console.print(f"\n[green]{len(jobs)} results (not stored).[/green]")


@app.command("list")
def list_jobs(
    limit: int = typer.Option(20, "--limit", "-n"),
    since_days: Optional[int] = typer.Option(None, "--since-days", "-s"),
    profile_name: Optional[str] = typer.Option(None, "--profile-name"),
) -> None:
    """Show stored jobs."""
    settings = load_settings()
    with Store(settings.jd_db_path) as store:
        rows = store.list_jobs(limit=limit, since_days=since_days, profile=profile_name)
        total = store.count()
    _render(rows)
    console.print(f"\n[dim]{len(rows)} shown, {total} total in {settings.jd_db_path}[/dim]")


@app.command()
def export(
    out: str = typer.Option(..., "--out", "-o", help="Output file path."),
    fmt: str = typer.Option("csv", "--format", "-f", help="csv or jsonl."),
    limit: int = typer.Option(10_000, "--limit", "-n"),
    since_days: Optional[int] = typer.Option(None, "--since-days", "-s"),
) -> None:
    """Export stored jobs to CSV or JSONL."""
    if fmt not in {"csv", "jsonl"}:
        console.print("[red]--format must be csv or jsonl[/red]")
        raise typer.Exit(2)

    settings = load_settings()
    with Store(settings.jd_db_path) as store:
        rows = store.list_jobs(limit=limit, since_days=since_days)

    written = write_csv(rows, out) if fmt == "csv" else write_jsonl(rows, out)
    console.print(f"[green]Wrote {written} rows to {out}[/green]")


@app.command()
def probe(
    out: str = typer.Option("docs/api-snapshot.json", "--out", "-o"),
) -> None:
    """Capture the real API schema. Run this first, from a networked machine.

    Fetches the OpenAPI spec and runs a minimal 1-result search, writing both to disk.
    The field names in filters.py are guesses -- this is how they get confirmed.
    """
    settings = load_settings()
    try:
        api_key = settings.require_api_key()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)

    snapshot: dict[str, object] = {"captured_at": datetime.now(timezone.utc).isoformat()}

    with TheirStackClient(
        api_key, base_url=settings.theirstack_base_url, timeout=settings.jd_timeout_seconds
    ) as client:
        try:
            snapshot["openapi"] = client.fetch_openapi()
            console.print("[green]Fetched OpenAPI spec.[/green]")
        except Exception as exc:
            snapshot["openapi_error"] = str(exc)
            console.print(f"[yellow]Could not fetch OpenAPI spec:[/yellow] {exc}")

        minimal = {"page": 0, "limit": 1, "posted_at_max_age_days": 7}
        try:
            snapshot["sample_search_request"] = minimal
            snapshot["sample_search_response"] = client.raw_search(minimal)
            console.print("[green]Ran a 1-result sample search.[/green]")
        except Exception as exc:
            snapshot["sample_search_error"] = str(exc)
            console.print(f"[yellow]Sample search failed:[/yellow] {exc}")

    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    console.print(f"[green]Wrote {path}[/green]")
    console.print("[dim]Review it, then correct build_request_body() in filters.py.[/dim]")


def _render(rows) -> None:
    table = Table(show_header=True, header_style="bold")
    for col in ("Title", "Company", "Location", "Posted", "URL"):
        table.add_column(col, overflow="fold")
    for row in rows:
        table.add_row(
            row["job_title"] or "-",
            row["company"] or "-",
            row["location"] or "-",
            row["date_posted"] or "-",
            row["url"] or "-",
        )
    console.print(table)


if __name__ == "__main__":
    app()
