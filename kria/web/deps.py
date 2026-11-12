"""Shared web dependencies: the database session and the Jinja templates."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from kria.db.base import SessionLocal

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def get_session():
    with SessionLocal() as session:
        yield session
