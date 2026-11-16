# ruff: noqa: F811  (pytest fixtures are imported by name and unpacked in tests)
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kria.config import settings
from kria.db.models import Curator, Program, ProgramDecision
from kria.pipeline import digest as dg
from kria.pipeline import programs as pg
from kria.web import auth
from kria.web.main import app, get_session
from tests.test_editor import PASSWORD, article_id, screened  # noqa: F401
from tests.test_screen import FakeBatches, good_output, ingested, session  # noqa: F401

NEW_PASSWORD = "a-good-new-password"


# ---- passwords and cookies ----------------------------------------------------------------------


def test_password_hashing_and_temporary_passwords():
    h = auth.hash_password("correct horse")
    assert h.startswith("scrypt$") and "correct horse" not in h
    assert auth.verify_password("correct horse", h)
    assert not auth.verify_password("wrong", h)
    assert not auth.verify_password("x", "garbage")
    assert auth.hash_password("same") != auth.hash_password("same")  # random salt
    t = auth.temporary_password()
    assert len(t) == 19 and t.count("-") == 3
    assert auth.password_problem("short") and auth.password_problem("long-enough-pw") is None


# ---- programs, modes, privacy -------------------------------------------------------------------


@pytest.fixture
def two_programs(screened):  # noqa: F811
    """Program A (curated) and program B (automatic, resuscitation only)."""
    a = pg.create_program(screened, "Program A")
    b = pg.create_program(screened, "Program B")
    pg.set_settings(screened, a, "curated", [])
    pg.set_settings(screened, b, "automatic", ["resuscitation"])
    return screened, a, b


def test_create_program_and_curator_validation(screened):  # noqa: F811
    p = pg.create_program(screened, "  Shamir   EM ")
    assert p.name == "Shamir EM" and p.mode == "automatic" and p.topics == []
    with pytest.raises(ValueError, match="כבר קיימת"):
        pg.create_program(screened, "Shamir EM")
    c, password = pg.create_curator(screened, p, "Dr.Cohen", 'ד"ר כהן')
    assert c.username == "dr.cohen" and c.must_change_password
    assert auth.verify_password(password, c.password_hash)
    with pytest.raises(ValueError, match="תפוס"):
        pg.create_curator(screened, p, "dr.cohen", "")
    with pytest.raises(ValueError):
        pg.create_curator(screened, p, "שם בעברית", "")
    with pytest.raises(ValueError):
        pg.set_settings(screened, p, "automatic", ["astrology"])


def test_automatic_mode_filters_the_national_digest_by_topic(two_programs):
    session, a, b = two_programs
    dg.decide(session, "2026-10", article_id(session, "101"), "approved")  # sepsis
    dg.decide(session, "2026-10", article_id(session, "103"), "approved")  # resuscitation
    assert [i["pmid"] for i in pg.content(session, b, "2026-10")["top"]] == ["103"]
    pg.set_settings(session, b, "automatic", [])  # no topics = everything
    assert pg.content(session, b, "2026-10")["count"] == 2


def test_curated_programs_keep_their_own_private_decisions(two_programs):
    session, a, b = two_programs
    pg.set_settings(session, b, "curated", [])
    a101 = article_id(session, "101")
    pg.decide(session, a, "2026-10", a101, "approved")
    pg.rate(session, a, "2026-10", a101, 5)
    assert pg.feature(session, a, "2026-10", a101, True)
    items_a = {i["pmid"]: i for i in pg.candidates(session, a, "2026-10")}
    items_b = {i["pmid"]: i for i in pg.candidates(session, b, "2026-10")}
    assert items_a["101"]["decision"] == "approved" and items_a["101"]["rating"] == 5
    assert items_b["101"]["decision"] is None and items_b["101"]["rating"] is None
    # national approvals are visible as a hint but are not the program's decision
    dg.decide(session, "2026-10", article_id(session, "103"), "approved")
    items_a = {i["pmid"]: i for i in pg.candidates(session, a, "2026-10")}
    assert items_a["103"]["national_decision"] == "approved" and items_a["103"]["decision"] is None
    assert [i["pmid"] for i in pg.content(session, a, "2026-10")["top"]] == ["101"]
    # the national digest is not changed by a program
    assert dg.content(session, "2026-10")["article_ids"] == [article_id(session, "103")]


def test_curated_rules(two_programs):
    session, a, b = two_programs
    a101 = article_id(session, "101")
    with pytest.raises(ValueError, match="אוצרות"):
        pg.decide(session, b, "2026-10", a101, "approved")  # b is automatic
    with pytest.raises(ValueError, match="1 ל-5"):
        pg.rate(session, a, "2026-10", a101, 9)
    with pytest.raises(ValueError, match="מועמדים"):
        pg.decide(session, a, "2026-10", article_id(session, "106"), "approved")  # not relevant
    assert not pg.feature(session, a, "2026-10", a101, True)  # not approved yet


# ---- web: editor manages, curator works ---------------------------------------------------------


@pytest.fixture
def web(two_programs, monkeypatch):
    session, a, b = two_programs
    monkeypatch.setattr(settings, "editor_password", PASSWORD)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app), session, a, b
    app.dependency_overrides.clear()
    if hasattr(app.state, "pubmed"):
        del app.state.pubmed


def editor_creates_curator(client, program, username):
    client.post("/editor/login", data={"password": PASSWORD})
    r = client.post(
        f"/editor/programs/{program.id}/curators",
        data={"username": username, "display_name": "Dr Test"},
    )
    assert r.status_code == 200 and "סיסמה זמנית" in r.text
    import re

    temp = re.search(r"([a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4})", r.text).group(1)
    client.post("/editor/logout")
    client.cookies.clear()
    return temp


def curator_logs_in(client, username, temp):
    r = client.post(
        "/program/login", data={"username": username, "password": temp}, follow_redirects=False
    )
    assert r.headers["location"] == "/program/password"
    r = client.post(
        "/program/password", data={"current": temp, "new": NEW_PASSWORD, "again": NEW_PASSWORD}
    )
    assert r.status_code == 200
    return r


def test_editor_creates_programs_and_curators(web):
    client, session, a, b = web
    assert client.get("/editor/programs", follow_redirects=False).status_code == 303
    client.post("/editor/login", data={"password": PASSWORD})
    page = client.get("/editor/programs").text
    assert "Program A" in page and "אוצרות מלאה" in page
    client.post("/editor/programs", data={"name": "Program C"})
    assert session.scalar(select(Program).where(Program.name == "Program C"))
    r = client.post("/editor/programs", data={"name": "Program C"})
    assert "כבר קיימת" in r.text


def test_first_login_forces_a_password_change(web):
    client, session, a, b = web
    temp = editor_creates_curator(client, a, "dr.a")
    r = client.post(
        "/program/login", data={"username": "dr.a", "password": temp}, follow_redirects=False
    )
    assert r.headers["location"] == "/program/password"
    assert client.get("/program/2026-10", follow_redirects=False).headers["location"] == (
        "/program/password"
    )
    r = client.post("/program/password", data={"current": temp, "new": "short", "again": "short"})
    assert "10 תווים" in r.text
    r = client.post(
        "/program/password", data={"current": temp, "new": NEW_PASSWORD, "again": NEW_PASSWORD}
    )
    assert "Program A" in r.text
    session.expire_all()
    c = session.scalar(select(Curator).where(Curator.username == "dr.a"))
    assert not c.must_change_password and auth.verify_password(NEW_PASSWORD, c.password_hash)


def test_wrong_password_inactive_and_forged_cookie(web, monkeypatch):
    client, session, a, b = web
    monkeypatch.setattr("kria.web.program.time.sleep", lambda s: None)
    temp = editor_creates_curator(client, a, "dr.a")
    r = client.post(
        "/program/login", data={"username": "dr.a", "password": "nope"}, follow_redirects=False
    )
    assert "error=1" in r.headers["location"]
    client.cookies.set(auth.COOKIE, "1.99999999999.deadbeef")
    assert client.get("/program", follow_redirects=False).status_code == 303
    curator_logs_in(client, "dr.a", temp)
    assert client.get("/program").status_code == 200
    c = session.scalar(select(Curator).where(Curator.username == "dr.a"))
    c.active = False
    session.commit()
    assert client.get("/program", follow_redirects=False).status_code == 303  # signed out


def test_curator_sees_only_their_program_and_rates(web):
    client, session, a, b = web
    temp_a = editor_creates_curator(client, a, "dr.a")
    temp_b = editor_creates_curator(client, b, "dr.b")
    a101 = article_id(session, "101")

    curator_logs_in(client, "dr.a", temp_a)
    page = client.get("/program/2026-10").text
    assert "Program A" in page and "Fluids in sepsis" in page and "אוצרות מלאה" in page
    client.post("/program/2026-10/decide", data={"article_id": a101, "action": "approved"})
    client.post("/program/2026-10/decide", data={"article_id": a101, "action": "rate-4"})
    client.post("/program/2026-10/decide", data={"article_id": a101, "action": "feature"})
    d = session.scalar(select(ProgramDecision))
    assert (d.program_id, d.decision, d.rating, d.featured) == (a.id, "approved", 4, True)
    page = client.get("/program/2026-10").text
    assert "שליחה לקבוצת הוואטסאפ" in page and "Program A" in page
    token = pg.month_page(session, a, "2026-10").public_token
    client.post("/program/logout")
    client.cookies.clear()

    curator_logs_in(client, "dr.b", temp_b)
    page = client.get("/program/2026-10").text
    assert "Program B" in page and "Program A" not in page
    assert "העורך הלאומי עוד לא אישר" in page  # B is automatic and nothing is approved yet
    assert client.get("/program/settings").status_code == 200
    client.cookies.clear()

    public = client.get(f"/p/{token}")
    assert public.status_code == 200 and "Fluids in sepsis" in public.text
    assert "Cooling after arrest" not in public.text
    assert client.get("/p/unknown").status_code == 404


def test_curator_changes_settings_and_adds_an_article(web):
    client, session, a, b = web
    from tests.test_appraisal import ARTICLE_XML, FakeEU

    temp = editor_creates_curator(client, b, "dr.b")
    curator_logs_in(client, "dr.b", temp)
    client.post("/program/settings", data={"mode": "curated", "topics": ["trauma", "pocus"]})
    session.refresh(b)
    assert b.mode == "curated" and b.topics == ["pocus", "trauma"]
    app.state.pubmed = FakeEU
    assert ARTICLE_XML
    r = client.post("/program/2026-10/add", data={"identifier": "777"}, follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]
    added = [i for i in pg.candidates(session, b, "2026-10") if i["added"]]
    assert len(added) == 1 and added[0]["pmid"] == "777" and added[0]["decision"] == "approved"
    content = pg.content(session, b, "2026-10")
    assert "777" in [i["pmid"] for i in content["top"]]
    html = dg.render("2026-10", content, "https://x")[1]  # a hand-added article renders
    assert "Early fluids in septic shock" in html
    r = client.post("/program/2026-10/add", data={"identifier": "not an id"})
    assert "נא להזין PMID" in r.text


def test_password_change_signs_out_other_sessions(web):
    client, session, a, b = web
    temp = editor_creates_curator(client, a, "dr.a")
    curator_logs_in(client, "dr.a", temp)
    old_cookie = client.cookies.get(auth.COOKIE)
    other = TestClient(app)
    other.cookies.set(auth.COOKIE, old_cookie)
    assert other.get("/program").status_code == 200
    client.post(
        "/program/password",
        data={
            "current": NEW_PASSWORD,
            "new": "another-new-password",
            "again": "another-new-password",
        },
    )
    assert other.get("/program", follow_redirects=False).status_code == 303
    assert client.get("/program").status_code == 200  # the changing session got a new cookie


def test_good_output_still_used():
    assert "relevant" in good_output() and FakeBatches
