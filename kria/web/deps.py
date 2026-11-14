"""Shared web dependencies: the database session and the Jinja templates."""

from pathlib import Path

from fastapi.templating import Jinja2Templates

from kria.db.base import SessionLocal

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _pct(value, digits: int = 1) -> str:
    """0.256 -> "25.6%"; None -> "n/a"."""
    if value is None:
        return "n/a"
    return f"{value * 100:.{digits}f}%"


def _num(value, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int | float) and float(value).is_integer() and abs(value) > 1:
        return str(int(round(value)))
    return f"{value:.{digits}f}"


def _nnt(value) -> str:
    """NNT/NNH rounded up to the next whole patient, as is conventional."""
    import math

    return "n/a" if value is None else str(math.ceil(value - 1e-9))


templates.env.filters.update(pct=_pct, num=_num, nnt=_nnt)


def get_session():
    with SessionLocal() as session:
        yield session
