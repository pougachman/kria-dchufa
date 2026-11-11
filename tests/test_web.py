from fastapi.testclient import TestClient

from kria.db.base import normalize_db_url
from kria.web.main import app

client = TestClient(app)


def test_home_renders_hebrew_rtl():
    r = client.get("/")
    assert r.status_code == 200
    assert 'dir="rtl"' in r.text
    assert "קריאה דחופה" in r.text


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_normalize_db_url():
    assert normalize_db_url("postgres://u:p@h:5432/d") == "postgresql+psycopg://u:p@h:5432/d"
    assert normalize_db_url("postgresql://u:p@h/d") == "postgresql+psycopg://u:p@h/d"
    assert normalize_db_url("sqlite:///./x.db") == "sqlite:///./x.db"
