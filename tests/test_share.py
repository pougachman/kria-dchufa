from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kria.config import settings
from kria.db.models import Article, PublishedMonth
from kria.pipeline import digest as dg
from kria.pipeline import share as sh
from kria.web.main import app, get_session
from tests.test_editor import PASSWORD, article_id, client, login, screened  # noqa: F401
from tests.test_screen import ingested, session  # noqa: F401


def item(pmid, title, url=None):
    return {
        "pmid": pmid,
        "title": title + ".",
        "journal": "Ann Emerg Med",
        "scores": {"practice_changing": 5, "board_exam": 2, "journal_club": 1},
        "reasons": {"practice_changing": f"Reason {pmid}.", "board_exam": "", "journal_club": ""},
        "appraisal_url": url,
    }


def test_digest_message():
    content = {"count": 21, "top": [item("1", "First"), item("2", "Second", "https://x/a/t")]}
    text = sh.digest_message("2026-10", content, "https://x/d/abc")
    lines = text.splitlines()
    assert lines[0] == "*קריאה דחופה · אוקטובר 2026*"
    assert "21 מאמרים" in lines[1]
    assert text.index("https://x/d/abc") < text.index("First")  # the page link comes first
    assert "1️⃣ *First* (Ann Emerg Med)" in text and "_Reason 1._" in text
    assert "https://pubmed.ncbi.nlm.nih.gov/1/" in text
    assert "https://x/a/t" in text and "pubmed.ncbi.nlm.nih.gov/2/" not in text  # appraisal wins


def test_appraisal_message_and_link():
    a = type("A", (), {"report": {"bottom_line": " Fluids may help. "}, "limited": True})()
    art = type(
        "Art", (), {"title": "Trial.", "journal_ta": "N Engl J Med", "pub_date": "2023 Feb"}
    )()
    text = sh.appraisal_message(a, art, "https://x/a/t")
    assert "*Trial* (N Engl J Med 2023)" in text
    assert "*Bottom line:* Fluids may help." in text and text.endswith("https://x/a/t")
    assert "ניתוח מוגבל" in text
    link = sh.whatsapp_link("שלום *עולם*\nhttps://x")
    assert (
        link.startswith("https://wa.me/?text=") and unquote(link[20:]) == "שלום *עולם*\nhttps://x"
    )


def test_month_page_is_created_once(screened):  # noqa: F811
    page = sh.month_page(screened, "2026-10")
    assert sh.month_page(screened, "2026-10").public_token == page.public_token
    assert sh.month_page(screened, "2026-11", create=False) is None


def test_digest_screen_offers_whatsapp_and_public_page(client, screened):  # noqa: F811
    login(client)
    assert "שליחה לקבוצת הוואטסאפ" not in client.get("/editor/2026-10/digest").text
    dg.decide(screened, "2026-10", article_id(screened, "101"), "approved")
    page = client.get("/editor/2026-10/digest").text
    assert "שליחה לקבוצת הוואטסאפ" in page and "https://wa.me/?text=" in page
    token = screened.scalar(select(PublishedMonth.public_token))
    assert f"/d/{token}" in page
    client.cookies.clear()  # residents are not logged in
    public = client.get(f"/d/{token}")
    assert public.status_code == 200 and "Fluids in sepsis" in public.text
    assert "Cooling after arrest" not in public.text  # not approved
    assert client.get("/d/unknown").status_code == 404


def test_public_digest_404_when_nothing_is_approved(client, screened):  # noqa: F811
    page = sh.month_page(screened, "2026-10")
    assert client.get(f"/d/{page.public_token}").status_code == 404


@pytest.fixture
def approved_appraisal(session, monkeypatch):  # noqa: F811
    from kria.appraisal import engine
    from tests.test_appraisal import FakeLLM, new_appraisal

    a = new_appraisal(session, source="abstract")
    session.commit()
    engine.run(session, a.id, FakeLLM())
    a.status = "approved"
    session.commit()
    monkeypatch.setattr(settings, "editor_password", PASSWORD)
    app.dependency_overrides[get_session] = lambda: session
    yield a
    app.dependency_overrides.clear()


def test_approved_appraisal_offers_whatsapp(approved_appraisal, session):  # noqa: F811
    c = TestClient(app)
    c.post("/editor/login", data={"password": PASSWORD})
    page = c.get(f"/editor/appraisals/{approved_appraisal.id}").text
    assert "שליחה לקבוצת הוואטסאפ" in page and "Early fluids may reduce death." in page
    assert f"/a/{approved_appraisal.public_token}" in page
    assert session.scalar(select(Article)) is not None
