import datetime as dt

from fastapi.testclient import TestClient

from kria.db.models import Article, Screening
from kria.pipeline import monthly, rank, screen
from kria.web.main import app, get_session
from tests.test_ingest import FakePubMed, small_data
from tests.test_screen import (  # noqa: F401
    FakeBatches,
    good_output,
    ingested,
    memory_engine,
    session,
)


def pair(pmid, pc, be, jc, design="rct", journal="Ann Emerg Med", relevant=True, topics=None):
    a = Article(pmid=str(pmid), title=f"Article {pmid}", journal_ta=journal, flags=[])
    s = Screening(
        status="ok",
        relevant=relevant,
        design=design,
        topics=topics or ["trauma"],
        practice_changing=pc,
        board_exam=be,
        journal_club=jc,
    )
    return a, s


TIERS = {"Ann Emerg Med": 0, "CJEM": 1}


def order(pairs, **kw):
    return [item["pmid"] for item in rank.shortlist(pairs, tiers=TIERS, **kw)]


def test_highest_dimension_wins():
    pairs = [pair(1, 3, 3, 3), pair(2, 1, 1, 5), pair(3, 4, 1, 1)]
    assert order(pairs) == ["2", "3", "1"]


def test_ties_broken_by_design_then_journal_then_total_then_pmid():
    pairs = [
        pair(10, 4, 1, 1, design="retrospective_cohort"),
        pair(11, 4, 1, 1, design="rct", journal="Some Other J"),
        pair(12, 4, 1, 1, design="rct", journal="CJEM"),
        pair(13, 4, 2, 1, design="rct", journal="CJEM"),
        pair(9, 4, 1, 1, design="rct", journal="CJEM"),
        pair(14, 4, 1, 1, design="case_report_or_series"),
    ]
    assert order(pairs) == ["13", "9", "12", "11", "10", "14"]


def test_irrelevant_excluded_and_topic_filter_and_size():
    pairs = [
        pair(1, 5, 5, 5, relevant=False),
        pair(2, 2, 2, 2, topics=["pocus"]),
        pair(3, 3, 3, 3, topics=["trauma", "pocus"]),
        pair(4, 4, 4, 4, topics=["trauma"]),
    ]
    assert order(pairs) == ["4", "3", "2"]
    assert order(pairs, topics={"pocus"}) == ["3", "2"]
    assert order(pairs, size=1) == ["4"]
    first = rank.shortlist(pairs, tiers=TIERS)[0]
    assert first["rank"] == 1 and first["score"] == 4


def test_real_journal_tiers():
    tiers = rank.journal_tiers()
    assert tiers["Ann Emerg Med"] == 0 and tiers["N Engl J Med"] == 0
    assert tiers["CJEM"] == 1


# ---- monthly job -------------------------------------------------------------------------------


def test_previous_month():
    assert monthly.previous_month(dt.date(2026, 11, 1)) == "2026-10"
    assert monthly.previous_month(dt.date(2027, 1, 15)) == "2026-12"


def test_monthly_job_ingests_submits_waits_collects_then_stops(session, monkeypatch):  # noqa: F811
    from kria.pipeline import ingest

    real_load = ingest.load_data
    monkeypatch.setattr(ingest, "load_data", lambda *a, **k: small_data())
    fake = FakeBatches(ended=False)

    msg = monthly.step(session, "2026-10", FakePubMed, lambda: fake)
    assert msg.startswith("ingested 2026-10: 3 of 6 kept; submitted 3 articles")
    assert monthly.step(session, "2026-10", FakePubMed, lambda: fake).endswith("still running")
    fake.ended = True
    assert "3 ok, 0 failed" in monthly.step(session, "2026-10", FakePubMed, lambda: fake)
    assert "nothing to do" in monthly.step(session, "2026-10", FakePubMed, lambda: fake)
    monkeypatch.setattr(ingest, "load_data", real_load)


# ---- status page -------------------------------------------------------------------------------


def test_status_page_shows_counts_in_hebrew_without_article_content(ingested):  # noqa: F811
    fake = FakeBatches()
    run = screen.submit_month(ingested, fake, "2026-10")
    screen.collect(ingested, fake, run)
    app.dependency_overrides[get_session] = lambda: ingested
    try:
        r = TestClient(app).get("/status")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200
    assert 'dir="rtl"' in r.text and "מצב הצנרת החודשית" in r.text
    assert "2026-10" in r.text and "הושלמה" in r.text
    assert "claude-haiku-5-5 · screening.v1" in r.text
    assert "Fluids in sepsis" not in r.text  # no article titles on this page
    assert good_output() not in r.text


def test_status_page_without_database_tables():
    from sqlalchemy.orm import Session

    empty = Session(memory_engine())
    app.dependency_overrides[get_session] = lambda: empty
    try:
        r = TestClient(app).get("/status")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200 and "מסד הנתונים עדיין לא מוכן" in r.text
