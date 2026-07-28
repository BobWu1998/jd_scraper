"""Command line interface."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table

from .client import OutOfCreditsError, TheirStackClient, TheirStackError, format_body
from .config import load_settings
from .export import write_csv, write_jsonl
from .filters import MissingMandatoryFilterError, build_request_body, expand_queries
from .models import SearchProfile
from .store import Store
from .wizard import build_profile_interactively, profile_to_yaml, write_profile

app = typer.Typer(help="Pull LinkedIn job postings via the TheirStack API.", no_args_is_help=True)
profile_app = typer.Typer(help="Create and edit search profiles.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")
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
    preview: bool = typer.Option(
        False, "--preview", help="Blurred, credit-free results. Good for testing filters."
    ),
    totals: bool = typer.Option(
        False, "--totals", help="Ask the API for total match counts. Slower."
    ),
    full: bool = typer.Option(
        False,
        "--full",
        help="Ignore the incremental watermark and re-fetch everything. Costs more.",
    ),
) -> None:
    """Run a saved search against TheirStack."""
    settings = load_settings()
    prof = _load_profile(profile)
    cap = max_results if max_results is not None else prof.limit

    discovered_since, exclude_ids = _incremental_window(prof, settings, disabled=full)

    if dry_run:
        variants = expand_queries(prof)
        share = max(1, cap // len(variants))
        for index, (label, variant) in enumerate(variants):
            if label:
                console.print(f"\n[bold cyan]--- {label} search ---[/bold cyan]")
            _print_body(
                variant,
                cap=cap if len(variants) == 1 else share,
                preview=preview,
                totals=totals,
                discovered_since=discovered_since,
                exclude_ids=exclude_ids,
            )
        return

    if discovered_since:
        console.print(
            f"[dim]Incremental: only postings discovered since {discovered_since}"
            f"{f', excluding {len(exclude_ids)} known id(s)' if exclude_ids else ''}.[/dim]"
        )
    elif prof.incremental and not full:
        console.print("[dim]No previous run for this profile -- fetching from scratch.[/dim]")

    # 1 credit per job returned -- confirm before a large pull. Preview mode is free.
    if cap > settings.jd_confirm_threshold and not yes and not preview:
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
    try:
        body_for_log = build_request_body(
            prof,
            page=0,
            page_size=min(prof.page_size, cap),
            preview=preview,
            discovered_since=discovered_since,
            exclude_ids=exclude_ids,
        )
    except MissingMandatoryFilterError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2)

    queries = expand_queries(prof)
    if len(queries) > 1:
        console.print(
            f"[dim]Running {len(queries)} searches and merging: "
            f"{', '.join(label for label, _ in queries)}. "
            f"The API ANDs filters, so OR needs separate requests.[/dim]"
        )

    def _progress(page: int, count: int) -> None:
        console.print(f"[dim]  page {page}: {count} results[/dim]")

    jobs: list = []
    seen_ids: set = set()
    billed = 0
    metadata: dict = {}

    try:
        with TheirStackClient(
            api_key,
            base_url=settings.theirstack_base_url,
            timeout=settings.jd_timeout_seconds,
        ) as client:
            # Split the cap so one variant cannot starve the other.
            share = max(1, cap // len(queries))
            for index, (label, variant) in enumerate(queries):
                # Give any remainder to the last variant.
                budget = cap - (share * index) if index == len(queries) - 1 else share
                if budget < 1:
                    break
                if label:
                    console.print(f"[dim]{label} search (up to {budget}):[/dim]")

                found, meta = client.search(
                    variant,
                    max_results=budget,
                    preview=preview,
                    include_total_results=totals,
                    discovered_since=discovered_since,
                    exclude_ids=exclude_ids,
                    on_page=_progress,
                )
                billed += meta.get("billed_results", 0)
                metadata = {**meta, **metadata}
                for job in found:
                    if job.id is not None and job.id in seen_ids:
                        continue  # matched both searches; keep one copy
                    if job.id is not None:
                        seen_ids.add(job.id)
                    jobs.append(job)
    except OutOfCreditsError as exc:
        console.print(f"[red]Out of credits:[/red] {exc}")
        console.print("[dim]Re-run with --preview for free, blurred results.[/dim]")
        raise typer.Exit(1)
    except TheirStackError as exc:
        console.print(f"[red]API call failed:[/red] {exc}")
        raise typer.Exit(1)

    metadata["billed_results"] = billed
    if preview:
        console.print(f"[dim]Preview mode: {billed} blurred results, no credits used.[/dim]")
    else:
        console.print(f"[dim]{billed} jobs returned = {billed} credits used.[/dim]")

    if prof.sources.linkedin_only:
        discarded = billed - len(jobs)
        note = f"LinkedIn-only filter kept {len(jobs)} of {billed}."
        if discarded > 0:
            note += (
                f" {discarded} non-LinkedIn result(s) still cost credits -- the"
                " server-side url_domain_or filter should normally prevent this."
            )
        console.print(f"[dim]{note}[/dim]")

    truncated = metadata.get("truncated_results") or 0
    if truncated:
        console.print(
            f"[yellow]{truncated} further match(es) were withheld because the account "
            f"lacks credits to fetch them.[/yellow]"
        )

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
def show(
    job_id: str = typer.Argument(..., help="Job id, as shown by `jd list`."),
    links_only: bool = typer.Option(False, "--links-only", help="Skip the description."),
) -> None:
    """Show one job in full: links, salary, and the complete description."""
    settings = load_settings()
    with Store(settings.jd_db_path) as store:
        row = store.get_job(job_id)

    if row is None:
        console.print(f"[red]No job with id {job_id} in {settings.jd_db_path}[/red]")
        raise typer.Exit(1)

    console.print(f"\n[bold]{row['job_title'] or '(no title)'}[/bold]")
    console.print(f"[dim]{row['company'] or '-'} — {row['location'] or '-'}[/dim]")

    facts = [f"Posted: {row['date_posted'] or '-'}"]
    if row["seniority"]:
        facts.append(f"Seniority: {row['seniority']}")
    if _col(row, "salary_string"):
        facts.append(f"Salary: {row['salary_string']}")
    if row["remote"] is not None:
        facts.append("Remote" if row["remote"] else "On-site")
    console.print("[dim]" + " · ".join(facts) + "[/dim]")

    console.print(f"\n[bold cyan]Apply:[/bold cyan] {row['url'] or '-'}")
    if _col(row, "final_url"):
        # The company's own posting, usually a direct ATS application.
        console.print(f"[bold cyan]Company page:[/bold cyan] {row['final_url']}")

    if links_only:
        return

    description = _col(row, "description")
    if not description:
        console.print(
            "\n[yellow]No description stored for this job.[/yellow]\n"
            "[dim]Descriptions are blurred in preview mode and omitted from rows saved "
            "before this column existed. Re-run the search with preview off.[/dim]"
        )
        return

    console.print("\n[bold]Description[/bold]")
    console.print(Markdown(description))


def _col(row, name: str):
    """Read a column that may not exist in an older database."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


@app.command()
def export(
    out: str = typer.Option(..., "--out", "-o", help="Output file path."),
    fmt: str = typer.Option("csv", "--format", "-f", help="csv or jsonl."),
    limit: int = typer.Option(10_000, "--limit", "-n"),
    since_days: Optional[int] = typer.Option(None, "--since-days", "-s"),
    with_description: bool = typer.Option(
        False,
        "--with-description",
        help="Include the full description column in CSV. Long; jsonl always has it.",
    ),
) -> None:
    """Export stored jobs to CSV or JSONL."""
    if fmt not in {"csv", "jsonl"}:
        console.print("[red]--format must be csv or jsonl[/red]")
        raise typer.Exit(2)

    settings = load_settings()
    with Store(settings.jd_db_path) as store:
        rows = store.list_jobs(limit=limit, since_days=since_days)

    written = (
        write_csv(rows, out, with_description=with_description)
        if fmt == "csv"
        else write_jsonl(rows, out)
    )
    console.print(f"[green]Wrote {written} rows to {out}[/green]")


@app.command()
def serve(
    port: int = typer.Option(8000, "--port", "-p"),
    host: str = typer.Option("127.0.0.1", "--host"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Browse and triage stored jobs in a local web UI.

    Reads the database only -- it never calls TheirStack, so it cannot spend credits.
    """
    import uvicorn

    from .web import create_app

    settings = load_settings()
    if not Path(settings.jd_db_path).exists():
        console.print(
            f"[yellow]No database at {settings.jd_db_path} yet.[/yellow] "
            "The UI will open, but run a search first to have anything to show."
        )

    url = f"http://{host}:{port}"
    console.print(f"[green]Serving {settings.jd_db_path} at {url}[/green]")
    console.print("[dim]Read-only against the API — browsing costs no credits.[/dim]")

    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


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


@profile_app.command("new")
def profile_new(
    out: Optional[str] = typer.Option(None, "--out", "-o", help="Where to write the YAML."),
) -> None:
    """Build a new search profile by answering prompts."""
    prof = build_profile_interactively()
    _finish_profile(prof, out or f"profiles/{prof.name}.yml")


@profile_app.command("edit")
def profile_edit(
    path: str = typer.Argument(..., help="Profile YAML to edit."),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="Write elsewhere instead."),
) -> None:
    """Revise an existing profile's criteria, keeping current values as defaults."""
    base = _load_profile(path)
    prof = build_profile_interactively(base)
    _finish_profile(prof, out or path)


@profile_app.command("show")
def profile_show(
    path: str = typer.Argument(..., help="Profile YAML to inspect."),
) -> None:
    """Show a profile and the request body it produces. No API call."""
    prof = _load_profile(path)
    console.print(profile_to_yaml(prof))
    _print_body(prof)


def _finish_profile(prof: SearchProfile, out_path: str) -> None:
    written = write_profile(prof, out_path)
    console.print(f"\n[green]Wrote {written}[/green]")
    _print_body(prof)
    console.print(
        f"\n[dim]Run it with:[/dim] jd search --profile {written} --max-results 5"
        f"\n[dim]Revise it with:[/dim] jd profile edit {written}"
    )


def _incremental_window(
    prof: SearchProfile, settings, *, disabled: bool = False
) -> tuple[str | None, list[int]]:
    """Work out what this profile has already seen.

    Returns (discovered_since, exclude_ids). The timestamp is rewound by the
    profile's overlap so a posting discovered mid-run is not skipped; the id list
    removes the duplicates that overlap would otherwise let back in.
    """
    if disabled or not prof.incremental:
        return None, []

    db_path = Path(settings.jd_db_path)
    if not db_path.exists():
        return None, []

    with Store(db_path) as store:
        last_run = store.last_run_at(prof.name)
        if not last_run:
            return None, []
        try:
            last = datetime.fromisoformat(last_run)
        except ValueError:
            return None, []
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)

        since = last - timedelta(minutes=prof.incremental_overlap_minutes)
        # The API documents discovered_at_* as UTC, so send a bare UTC timestamp.
        stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        return stamp, store.known_job_ids(since=since.isoformat())


def _print_body(
    prof: SearchProfile,
    *,
    cap: int | None = None,
    preview: bool = False,
    totals: bool = False,
    discovered_since: str | None = None,
    exclude_ids: list[int] | None = None,
) -> None:
    """Preview the request body, which also validates the profile is searchable."""
    cap = cap if cap is not None else prof.limit
    try:
        body = build_request_body(
            prof,
            page=0,
            page_size=min(prof.page_size, cap),
            preview=preview or None,
            include_total_results=totals or None,
            discovered_since=discovered_since,
            exclude_ids=exclude_ids,
        )
    except MissingMandatoryFilterError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    except Exception as exc:
        console.print(f"[yellow]This profile is not searchable yet:[/yellow] {exc}")
        return
    console.print(f"\n[bold]Request body[/bold] (page 0 of up to {cap} results):")
    console.print_json(format_body(body))


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
