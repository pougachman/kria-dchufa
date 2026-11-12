"""Web application entry point."""

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from kria.config import anthropic_key_looks_valid, settings
from kria.db.base import engine
from kria.db.models import IngestRun, ScreeningRun
from kria.pipeline.batches import as_utc
from kria.web.deps import BASE_DIR, get_session, templates
from kria.web.editor import router as editor_router

app = FastAPI(title="קריאה דחופה")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.include_router(editor_router)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "home.html", {})


@app.get("/healthz")
def healthz():
    """Health check for Railway and for setup.

    Reports whether the database answers, which kind it is (so a missing DATABASE_URL, which
    falls back to a local SQLite file, is visible), the applied migration, and whether the API
    keys are configured. Key values are never shown.
    """
    schema = None
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            try:
                schema = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except Exception:
                schema = None
        database = "ok"
    except Exception:
        database = "unavailable"
    return {
        "status": "ok",
        "database": database,
        "database_kind": engine.dialect.name,
        "schema": schema,
        "ncbi_api_key": bool(settings.ncbi_api_key),
        "anthropic_api_key": bool(settings.anthropic_api_key),
        "anthropic_api_key_format_ok": anthropic_key_looks_valid(settings.anthropic_api_key),
        "editor_password": bool(settings.editor_password),
        "resend_api_key": bool(settings.resend_api_key),
        "digest_recipients": len(settings.recipients),
    }


def pipeline_status(session: Session) -> list[dict]:
    """Per month: the latest ingest run and the screening runs. Counts and costs only."""
    months: dict[str, dict] = {}
    for run in session.scalars(select(IngestRun).order_by(IngestRun.id)):
        m = months.setdefault(run.month, {"month": run.month, "screening": []})
        report = run.report or {}
        m["ingest"] = {
            "status": run.status,
            "error": (run.error or "")[:300] or None,
            "finished_at": run.finished_at,
            "total": report.get("total_unique"),
            "kept": report.get("kept"),
        }
    for run in session.scalars(select(ScreeningRun).order_by(ScreeningRun.id)):
        m = months.setdefault(run.month, {"month": run.month, "screening": []})
        totals = (run.report or {}).get("month_totals", {})
        minutes = None
        if run.ended_at and run.submitted_at:
            seconds = (as_utc(run.ended_at) - as_utc(run.submitted_at)).total_seconds()
            minutes = round(seconds / 60, 1)
        m["screening"].append(
            {
                "id": run.id,
                "status": run.status,
                "model": run.model,
                "prompt_version": run.prompt_version,
                "requested": run.requested,
                "succeeded": run.succeeded,
                "failed": run.failed,
                "cost_usd": run.cost_usd,
                "minutes": minutes,
                "relevant": totals.get("relevant"),
                "high_yield": totals.get("high_yield_4_plus"),
                "not_yet_screened": totals.get("not_yet_screened"),
                "error": (run.error or "")[:300] or None,
            }
        )
    return sorted(months.values(), key=lambda m: m["month"], reverse=True)


@app.get("/status", response_class=HTMLResponse)
def status(request: Request, session: Session = Depends(get_session)):  # noqa: B008
    """Pipeline progress in Hebrew, for the national editor. No article content is shown."""
    try:
        months, error = pipeline_status(session), None
    except SQLAlchemyError:
        months, error = [], "מסד הנתונים עדיין לא מוכן (לא הורצו מיגרציות)."
    return templates.TemplateResponse(
        request,
        "status.html",
        {
            "months": months,
            "error": error,
            "anthropic_key": bool(settings.anthropic_api_key),
            "ncbi_key": bool(settings.ncbi_api_key),
        },
    )
