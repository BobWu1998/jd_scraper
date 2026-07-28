"""Local web UI for browsing and triaging pulled jobs.

Reads the SQLite database only -- it never calls TheirStack, so nothing here can
spend credits. Run it with `jd serve`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from markdown_it import MarkdownIt
from pydantic import BaseModel

from .config import load_settings
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

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.exception_handler(sqlite3.Error)
    def _sqlite_error(_request, exc: sqlite3.Error) -> JSONResponse:
        return JSONResponse({"detail": f"database error: {exc}"}, status_code=500)

    return app
