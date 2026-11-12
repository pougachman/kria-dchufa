import datetime as dt
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kria.config import settings
from kria.db.models import Digest, EditorDecision, EditorEvent
from kria.pipeline import digest as dg
from kria.pipeline import screen
from kria.web.main import app, get_session
from tests.test_screen import FakeBatches, good_output, ingested, session  # noqa: F401

PASSWORD = "correct horse battery"


@pytest.fixture
def screened(ingested):  # noqa: F811
    """October with 101 (RCT, highest score), 103 and 106 screened; 106 is not relevant."""
    fake = FakeBatches(
        answers={
            "101": {"text": good_output(practice_changing=5)},
            "103": {"text": good_output(topics=["resuscitation"], design="prospective_cohort")},
            "106": {"text": good_output(relevant=False)},
        }
    )
    screen.collect(ingested, fake, screen.submit_month(ingested, fake, "2026-10"))
    return ingested


@pytest.fixture
def client(screened, monkeypatch):
    monkeypatch.setattr(settings, "editor_password", PASSWORD)
    app.dependency_overrides[get_session] = lambda: screened
    yield TestClient(app)
    app.dependency_overrides.clear()


def login(client):
    r = client.post("/editor/login", data={"password": PASSWORD, "next": "/editor"})
    assert r.status_code == 200  # followed the redirect to /editor
    return client


def article_id(db, pmid):
    from kria.db.models import Article

    return db.scalar(select(Article.id).where(Article.pmid == pmid))


# ---- editor time -------------------------------------------------------------------------------


def test_editor_minutes_caps_idle_gaps():
    t0 = dt.datetime(2026, 11, 2, 9, 0, tzinfo=dt.UTC)
    times = [t0, t0 + dt.timedelta(minutes=2), t0 + dt.timedelta(minutes=3)]
    assert dg.editor_minutes(times) == 3.0
    # A two-hour pause counts as five minutes; then four more minutes of work.
    later = t0 + dt.timedelta(hours=2)
    times += [later, later + dt.timedelta(minutes=4)]
    assert dg.editor_minutes(times) == 3.0 + 5.0 + 4.0
    assert dg.editor_minutes([]) == 0.0 and dg.editor_minutes([t0]) == 0.0
    naive = dt.datetime(2026, 11, 2, 9, 1)  # SQLite returns naive datetimes
    assert dg.editor_minutes([t0, naive]) == 1.0


def test_month_he():
    assert dg.month_he("2026-10") == "אוקטובר 2026"


# ---- decisions and digest logic ----------------------------------------------------------------


def test_ranked_lists_only_relevant_articles_in_rank_order(screened):
    items = dg.ranked(screened, "2026-10")
    assert [i["pmid"] for i in items] == ["101", "103"]
    assert all(i["decision"] is None for i in items)


def test_decide_approve_reject_clear_and_events(screened):
    a101, a103 = article_id(screened, "101"), article_id(screened, "103")
    dg.decide(screened, "2026-10", a101, "approved")
    dg.decide(screened, "2026-10", a103, "rejected")
    by_id = {i["article_id"]: i["decision"] for i in dg.ranked(screened, "2026-10")}
    assert by_id == {a101: "approved", a103: "rejected"}
    row = screened.scalar(select(EditorDecision).where(EditorDecision.article_id == a101))
    assert row.rank == 1 and row.screening_id is not None
    dg.decide(screened, "2026-10", a103, None)
    assert dg.ranked(screened, "2026-10")[1]["decision"] is None
    kinds = screened.scalars(select(EditorEvent.kind).order_by(EditorEvent.id)).all()
    assert kinds == ["approved", "rejected", "cleared"]
    with pytest.raises(ValueError):
        dg.decide(screened, "2026-10", article_id(screened, "106"), "approved")  # not relevant
    with pytest.raises(ValueError):
        dg.decide(screened, "2026-10", a101, "maybe")


def test_digest_contains_only_approved_articles_grouped_by_topic(screened):
    dg.decide(screened, "2026-10", article_id(screened, "101"), "approved")
    dg.decide(screened, "2026-10", article_id(screened, "103"), "rejected")
    digest = dg.content(screened, "2026-10")
    assert [i["pmid"] for i in digest["top"]] == ["101"] and digest["groups"] == []
    assert digest["count"] == 1 and digest["top"][0]["topic_names"][0] == "זיהומים וספסיס"
    subject, html, text = dg.render("2026-10", digest, "https://example.org")
    assert subject == "קריאה דחופה: 1 מאמרים נבחרים, אוקטובר 2026"
    assert "החודש חובה לקרוא" in html
    assert "Fluids in sepsis" in html and "Cooling after arrest" not in html
    assert "https://pubmed.ncbi.nlm.nih.gov/101/" in html and 'dir="rtl"' in html
    assert "Large trial with a mortality outcome." in html  # reason for the top score
    assert "Fluids in sepsis" in text and "Cooling" not in text


class FakeMailer:
    def __init__(self):
        self.sent = []

    def send(self, sender, to, subject, html, text):
        self.sent.append({"from": sender, "to": to, "subject": subject})
        return "email_123"


def test_send_digest_records_what_was_sent(screened):
    with pytest.raises(RuntimeError, match="no approved"):
        dg.send_digest(screened, "2026-10", FakeMailer(), "a@b", ["x@y"], "https://e")
    dg.decide(screened, "2026-10", article_id(screened, "101"), "approved")
    with pytest.raises(RuntimeError, match="DIGEST_RECIPIENTS"):
        dg.send_digest(screened, "2026-10", FakeMailer(), "a@b", [], "https://e")
    mailer = FakeMailer()
    d = dg.send_digest(screened, "2026-10", mailer, "K <a@b>", ["editor@example.org"], "https://e")
    assert mailer.sent == [{"from": "K <a@b>", "to": ["editor@example.org"], "subject": d.subject}]
    assert d.article_ids == [article_id(screened, "101")] and d.provider_id == "email_123"
    assert d.editor_minutes is not None and "Fluids in sepsis" in d.html


def test_resend_client_posts_the_email_and_reports_refusals():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        if seen["body"]["to"] == ["blocked@example.org"]:
            return httpx.Response(403, json={"message": "domain not verified"})
        return httpx.Response(200, json={"id": "re_1"})

    mailer = dg.Resend("re_key", http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert mailer.send("K <a@b>", ["me@example.org"], "S", "<p>h</p>", "t") == "re_1"
    assert seen["auth"] == "Bearer re_key"
    assert seen["body"] == {
        "from": "K <a@b>",
        "to": ["me@example.org"],
        "subject": "S",
        "html": "<p>h</p>",
        "text": "t",
    }
    with pytest.raises(RuntimeError, match="domain not verified"):
        mailer.send("K <a@b>", ["blocked@example.org"], "S", "h", "t")
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        dg.Resend(None)


# ---- web screens -------------------------------------------------------------------------------


def test_editor_pages_need_a_login(client):
    for path in ("/editor", "/editor/2026-10", "/editor/2026-10/digest"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/editor/login")
    r = client.post(
        "/editor/2026-10/decide",
        data={"article_id": 1, "decision": "approved"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and "/editor/login" in r.headers["location"]
    assert client.get("/editor/login").status_code == 200


def test_wrong_password_and_forged_cookie_are_refused(client, monkeypatch):
    monkeypatch.setattr("kria.web.editor.time.sleep", lambda s: None)
    r = client.post("/editor/login", data={"password": "guess"}, follow_redirects=False)
    assert "error=1" in r.headers["location"]
    client.cookies.set("kria_editor", "99999999999.deadbeef")
    assert client.get("/editor", follow_redirects=False).status_code == 303


def test_no_password_configured_means_no_access(client, monkeypatch):
    monkeypatch.setattr(settings, "editor_password", None)
    r = client.post("/editor/login", data={"password": ""}, follow_redirects=False)
    assert r.status_code in (303, 422)
    assert "EDITOR_PASSWORD" in client.get("/editor/login").text


def test_review_screen_shows_ranked_candidates_in_hebrew(client, screened):
    login(client)
    r = client.get("/editor")
    assert "אוקטובר 2026" in r.text and "ממתין לאישור" in r.text
    r = client.get("/editor/2026-10")
    assert r.status_code == 200 and 'dir="rtl"' in r.text
    assert r.text.index("Fluids in sepsis") < r.text.index("Cooling after arrest")
    assert "Correction of Hyponatremia" not in r.text  # 106 was screened as not relevant
    assert "ניסוי אקראי מבוקר (RCT)" in r.text and "זיהומים וספסיס" in r.text
    assert "https://pubmed.ncbi.nlm.nih.gov/101/" in r.text
    assert client.get("/editor/October").status_code == 404


def test_one_click_approve_then_digest_preview_and_send(client, screened, monkeypatch):
    login(client)
    a101 = article_id(screened, "101")
    r = client.post(
        "/editor/2026-10/decide",
        data={"article_id": a101, "decision": "approved", "show": 40},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/editor/2026-10?show=40#a{a101}"
    assert "✓ אושר" in client.get("/editor/2026-10").text

    page = client.get("/editor/2026-10/digest").text
    assert "1 מאמרים מאושרים" in page and "RESEND_API_KEY" in page  # mail not configured yet

    mailer = FakeMailer()
    monkeypatch.setattr(settings, "resend_api_key", "re_key")
    monkeypatch.setattr(settings, "digest_recipients", "editor@example.org")
    monkeypatch.setattr(dg, "Resend", lambda key: mailer)
    page = client.get("/editor/2026-10/digest").text
    assert "editor@example.org" in page and "אישור ושליחה" in page
    r = client.post("/editor/2026-10/digest/send")
    assert "הדייג'סט נשלח" in r.text and len(mailer.sent) == 1
    assert screened.scalar(select(Digest)).recipients == ["editor@example.org"]
    assert "הדייג'סט נשלח" in client.get("/editor").text


def test_failed_send_is_shown_to_the_editor(client, screened, monkeypatch):
    login(client)
    dg.decide(screened, "2026-10", article_id(screened, "101"), "approved")

    class Refusing:
        def send(self, *a):
            raise RuntimeError("Resend refused the email (403): domain not verified")

    monkeypatch.setattr(settings, "resend_api_key", "re_key")
    monkeypatch.setattr(settings, "digest_recipients", "someone@example.org")
    monkeypatch.setattr(dg, "Resend", lambda key: Refusing())
    r = client.post("/editor/2026-10/digest/send")
    assert "השליחה נכשלה" in r.text and "domain not verified" in r.text
    assert screened.scalar(select(Digest)) is None


def test_healthz_reports_m3_settings_without_values(client):
    body = client.get("/healthz").json()
    assert body["editor_password"] is True
    assert isinstance(body["resend_api_key"], bool) and isinstance(body["digest_recipients"], int)
    assert PASSWORD not in json.dumps(body)


def test_top_articles_are_marked_or_the_best_ranked_and_not_repeated(screened):
    a101, a103 = article_id(screened, "101"), article_id(screened, "103")
    dg.decide(screened, "2026-10", a101, "approved")
    dg.decide(screened, "2026-10", a103, "approved")
    monkey_max = dg.FEATURED
    dg.FEATURED = 1  # with one slot, the default top article is the best ranked
    try:
        d = dg.content(screened, "2026-10")
        assert [i["pmid"] for i in d["top"]] == ["101"]
        assert [i["pmid"] for g in d["groups"] for i in g["items"]] == ["103"]
        assert dg.feature(screened, "2026-10", a103, True)  # the editor's choice wins
        d = dg.content(screened, "2026-10")
        assert [i["pmid"] for i in d["top"]] == ["103"]
        assert [i["pmid"] for g in d["groups"] for i in g["items"]] == ["101"]
        assert not dg.feature(screened, "2026-10", a101, True)  # slots full
        assert dg.feature(screened, "2026-10", a103, False)
        dg.decide(screened, "2026-10", a101, "rejected")
        assert not dg.feature(screened, "2026-10", a101, True)  # only approved articles
    finally:
        dg.FEATURED = monkey_max


def test_feature_button_on_the_review_screen(client, screened):
    login(client)
    a101 = article_id(screened, "101")
    dg.decide(screened, "2026-10", a101, "approved")
    assert "☆ סמן כמוביל" in client.get("/editor/2026-10").text
    r = client.post(
        "/editor/2026-10/decide",
        data={"article_id": a101, "decision": "feature"},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/editor/2026-10?show=40#a{a101}"
    assert "★ מוביל" in client.get("/editor/2026-10").text
