"""Local web UI for browsing and triaging pulled jobs.

Browsing and triage read the SQLite database only and cost nothing. The search
endpoints do call TheirStack, so they are guarded: preview mode is the default, a
paid run is refused without explicit confirmation, and `jd_web_max_results` caps
any search started here regardless of what the request asks for.

Run it with `jd serve`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from markdown_it import MarkdownIt
from pydantic import BaseModel, ValidationError

from .client import OutOfCreditsError, TheirStackError
from .config import load_settings
from .filters import MissingMandatoryFilterError
from .models import SearchProfile
from .runner import plan_search, run_search
from .store import DEFAULT_STATUS, STATUSES, Store

STATIC_DIR = Path(__file__).parent / "static"
_md = MarkdownIt("commonmark").enable("table")

LIST_FIELDS = (
    "id",
    "job_title",
    "company",
    "location",
    "remote",
    "date_posted",
    "seniority",
    "salary_string",
    "url",
    "final_url",
    "profile",
    "status",
    "first_seen_at",
)


class StatusUpdate(BaseModel):
    status: str | None = None
    notes: str | None = None


class SearchRequest(BaseModel):
    """Filters posted from the UI, shaped like a SearchProfile."""

    profile: dict[str, Any]
    max_results: int | None = None
    full: bool = False
    confirm: bool = False
    """Required before a run that actually spends credits."""


def _clamp(requested: int | None, profile_limit: int, settings) -> int:
    """Never let a web request exceed the configured ceiling."""
    cap = requested if requested is not None else profile_limit
    return max(1, min(cap, settings.jd_web_max_results))


def _readable_errors(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        location = ".".join(str(p) for p in error["loc"]) or "profile"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def _row_to_dict(row: sqlite3.Row, fields: tuple[str, ...] | None = None) -> dict[str, Any]:
    keys = fields or row.keys()
    out: dict[str, Any] = {}
    for key in keys:
        try:
            out[key] = row[key]
        except (IndexError, KeyError):
            out[key] = None
    out["status"] = out.get("status") or DEFAULT_STATUS
    return out


def create_app(db_path: str | Path | None = None) -> FastAPI:
    settings = load_settings()
    path = Path(db_path) if db_path else Path(settings.jd_db_path)

    app = FastAPI(title="jd_scraper", docs_url=None, redoc_url=None)

    def store() -> Store:
        return Store(path)

    @app.get("/api/meta")
    def meta() -> dict[str, Any]:
        if not path.exists():
            return {"db": str(path), "exists": False, "total": 0, "statuses": list(STATUSES)}
        with store() as s:
            return {
                "db": str(path),
                "exists": True,
                "total": s.count(),
                "statuses": list(STATUSES),
                "counts": s.status_counts(),
                "profiles": s.profiles(),
            }

    @app.get("/api/jobs")
    def jobs(
        q: str | None = None,
        status: str | None = None,
        profile: str | None = None,
        remote: bool | None = None,
        since_days: int | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        with store() as s:
            rows = s.list_jobs(
                limit=limit,
                query=q,
                status=status,
                profile=profile,
                remote=remote,
                since_days=since_days,
            )
        return [_row_to_dict(r, LIST_FIELDS) for r in rows]

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> dict[str, Any]:
        if not path.exists():
            raise HTTPException(404, "no database yet")
        with store() as s:
            row = s.get_job(job_id)
        if row is None:
            raise HTTPException(404, f"no job {job_id}")

        data = _row_to_dict(row)
        data.pop("raw", None)  # large, and the columns already carry what the UI needs
        description = data.get("description")
        data["description_html"] = _md.render(description) if description else ""
        return data

    @app.post("/api/jobs/{job_id}/status")
    def update_status(job_id: str, update: StatusUpdate) -> dict[str, Any]:
        if not path.exists():
            raise HTTPException(404, "no database yet")
        with store() as s:
            try:
                row = s.set_status(job_id, update.status, update.notes)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        if row is None:
            raise HTTPException(404, f"no job {job_id}")
        return _row_to_dict(row, LIST_FIELDS)

    # --- running a new search ------------------------------------------------

    @app.get("/api/profiles")
    def saved_profiles() -> dict[str, Any]:
        """Saved profile YAMLs, so the form can start from an existing search."""
        directory = Path(settings.jd_profiles_dir)
        out = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.yml")):
                try:
                    profile = SearchProfile.from_yaml(str(path))
                except Exception as exc:
                    out.append({"file": path.name, "error": str(exc)})
                    continue
                out.append(
                    {
                        "file": path.name,
                        "name": profile.name,
                        "profile": profile.model_dump(mode="json"),
                    }
                )
        return {"dir": str(directory), "profiles": out}

    def _build_profile(payload: dict[str, Any]) -> SearchProfile:
        try:
            return SearchProfile.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(422, _readable_errors(exc))

    @app.post("/api/search/plan")
    def plan(request: SearchRequest) -> dict[str, Any]:
        """Show exactly what a run would send. Never calls the API."""
        profile = _build_profile(request.profile)
        try:
            return plan_search(
                profile,
                settings,
                max_results=_clamp(request.max_results, profile.limit, settings),
                full=request.full,
            )
        except MissingMandatoryFilterError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/search/run")
    def run(request: SearchRequest) -> dict[str, Any]:
        profile = _build_profile(request.profile)
        cap = _clamp(request.max_results, profile.limit, settings)

        # Spending money needs an explicit, informed click -- never a default.
        if not profile.preview and not request.confirm:
            raise HTTPException(
                400,
                f"This would return up to {cap} jobs and cost up to {cap} credits. "
                "Confirm the run, or enable preview mode for blurred results at no cost.",
            )

        try:
            outcome = run_search(profile, settings, max_results=cap, full=request.full)
        except MissingMandatoryFilterError as exc:
            raise HTTPException(400, str(exc))
        except OutOfCreditsError as exc:
            raise HTTPException(402, str(exc))
        except TheirStackError as exc:
            raise HTTPException(502, str(exc))
        except RuntimeError as exc:  # missing API key
            raise HTTPException(400, str(exc))

        return {
            "billed": outcome.billed,
            "credits_used": 0 if outcome.preview else outcome.billed,
            "preview": outcome.preview,
            "stored": outcome.stored,
            "new": outcome.new,
            "truncated": outcome.truncated,
            "queries": outcome.queries,
            "discovered_since": outcome.discovered_since,
            "excluded_ids": outcome.excluded_ids,
            "kept": len(outcome.jobs),
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.exception_handler(sqlite3.Error)
    def _sqlite_error(_request, exc: sqlite3.Error) -> JSONResponse:
        return JSONResponse({"detail": f"database error: {exc}"}, status_code=500)

    return app
