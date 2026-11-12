"""Web application entry point."""

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from kria.config import settings
from kria.db.base import engine

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="קריאה דחופה")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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
    }
