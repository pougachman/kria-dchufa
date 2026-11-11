import copy
import datetime as dt

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from kria.db.base import Base
from kria.db.models import Article, QueryVersion, RunHit, Topic
from kria.pipeline.ingest import (
    build_sources,
    date_clause,
    exclusion_reason,
    ingest_month,
    load_data,
    months_before,
    parse_month,
)

FILTERS = load_data()["filters"]


def test_date_clause_covers_whole_month():
    assert date_clause("2028-02") == '("2028/02/01"[crdt] : "2028/02/29"[crdt])'
    assert date_clause("2026-10") == '("2026/10/01"[crdt] : "2026/10/31"[crdt])'
    with pytest.raises(ValueError):
        parse_month("October")


def test_real_sources_cover_all_topics_and_pools():
    data = load_data()
    sources = build_sources(data)
    assert len(sources) == 33 + 2
    assert "pool:core-em" in sources and "pool:general" in sources
    for src in sources.values():
        assert src["journals"]
        assert src["query"].count("[ta]") == len(src["journals"])
    assert '"Ann Emerg Med"[ta]' in sources["pool:core-em"]["query"]


def test_months_before():
    assert months_before(dt.date(2026, 10, 1), 6) == dt.date(2026, 4, 1)
    assert months_before(dt.date(2026, 3, 1), 6) == dt.date(2025, 9, 1)


def test_old_backfilled_articles_are_left_out():
    oct_2026 = dt.date(2026, 10, 1)
    recent = rec(latest_pub_date=dt.date(2026, 4, 1))
    old = rec(latest_pub_date=dt.date(2026, 3, 31))
    assert exclusion_reason(recent, FILTERS, oct_2026) is None
    assert exclusion_reason(old, FILTERS, oct_2026) == "old_publication"
    assert exclusion_reason(rec(latest_pub_date=None), FILTERS, oct_2026) is None  # unknown: kept


def rec(**kw):
    base = {
        "pub_types": ["Journal Article"],
        "flags": [],
        "title": "A cohort study of chest pain",
        "abstract": "Background: ...",
        "languages": ["eng"],
    }
    return base | kw


@pytest.mark.parametrize(
    "record, reason",
    [
        (rec(), None),
        (rec(pub_types=["Letter"]), "type:Letter"),
        (rec(pub_types=["Letter", "Randomized Controlled Trial"]), None),
        (
            rec(pub_types=["Randomized Controlled Trial", "Retracted Publication"]),
            "type:Retracted Publication",
        ),
        (rec(pub_types=["Preprint"]), "type:Preprint"),
        (rec(flags=["RetractionIn"]), "retracted"),
        (rec(languages=["fre"]), "language"),
        (rec(abstract=None), None),  # no abstract alone is not a reason to drop
        (rec(abstract=None, title="In reply."), "title:letter_or_correction"),
        (rec(abstract=None, title="Authors' Reply: Exome"), "title:letter_or_correction"),
        (rec(abstract=None, title="Correction to: Early fluids"), "title:letter_or_correction"),
        (rec(abstract=None, title="Correction of Hyponatremia in the ED"), None),
        (rec(title="In reply to the critics: a randomized trial"), None),  # has an abstract
        (rec(title=""), "no_title"),
    ],
)
def test_exclusion_reason(record, reason):
    assert exclusion_reason(record, FILTERS) == reason


# ---- end to end with a fake PubMed ------------------------------------------------------------

ARTICLE = """<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article>
<Journal><JournalIssue><PubDate><Year>2026</Year></PubDate></JournalIssue><Title>{ta}</Title></Journal>
<ArticleTitle>{title}</ArticleTitle>{abstract}<Language>eng</Language>
<PublicationTypeList><PublicationType>{pt}</PublicationType></PublicationTypeList></Article>
<MedlineJournalInfo><MedlineTA>{ta}</MedlineTA></MedlineJournalInfo></MedlineCitation>
<PubmedData><ArticleIdList><ArticleId IdType="doi">{doi}</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle>"""

RECORDS = {
    "101": ("Ann Emerg Med", "Fluids in sepsis", True, "Randomized Controlled Trial", "10.1/a"),
    "102": ("Resuscitation", "A letter", False, "Letter", "10.1/b"),
    "103": ("Resuscitation", "Cooling after arrest", True, "Journal Article", "10.1/c"),
    "104": ("Resuscitation", "Cooling after arrest (duplicate)", True, "Journal Article", "10.1/c"),
    "105": ("N Engl J Med", "In reply.", False, "Journal Article", "10.1/d"),
    "106": ("N Engl J Med", "Correction of Hyponatremia", False, "Journal Article", "10.1/e"),
}


class FakePubMed:
    def __init__(self):
        self.terms = []

    def search(self, db, term):
        assert db == "pubmed" and "[crdt]" in term
        self.terms.append(term)
        ids = [p for p, r in RECORDS.items() if f'"{r[0]}"[ta]' in term]
        return sorted(ids, key=int), term

    def fetch_pubmed_xml(self, pmids):
        body = "".join(
            ARTICLE.format(
                pmid=p,
                ta=RECORDS[p][0],
                title=RECORDS[p][1],
                pt=RECORDS[p][3],
                doi=RECORDS[p][4],
                abstract="<Abstract><AbstractText>Text.</AbstractText></Abstract>"
                if RECORDS[p][2]
                else "",
            )
            for p in pmids
        )
        yield f"<PubmedArticleSet>{body}</PubmedArticleSet>".encode()


def small_data():
    data = load_data()
    data = copy.deepcopy(data)
    data["taxonomy"]["topics"] = [
        {"id": "resuscitation", "he": "החייאה", "group": "clinical", "journals": ["Resuscitation"]},
        {
            "id": "internal-medicine",
            "he": "פנימית",
            "group": "clinical",
            "suggested": True,
            "journals": ["Annals of Internal Medicine"],
        },
    ]
    data["sources"]["core_em_journals"] = {"core": ["Annals of Emergency Medicine"]}
    data["sources"]["general_journals"] = {"core": ["New England Journal of Medicine"]}
    return data


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


def test_ingest_month_end_to_end_and_rerun_is_identical(session):
    data = small_data()
    run1 = ingest_month(session, FakePubMed(), "2026-10", data)
    rep = run1.report
    assert run1.status == "ok"
    assert rep["total_unique"] == 6
    assert rep["kept"] == 3  # 101, 103, 106
    assert rep["excluded"] == {
        "type:Letter": 1,
        "duplicate_doi": 1,
        "title:letter_or_correction": 1,
    }
    assert rep["sources"]["topic:resuscitation"] == rep["sources"]["topic:resuscitation"] | {
        "found": 3,
        "kept": 1,
    }
    assert rep["sources"]["topic:internal-medicine"]["zero_hit_journals"] == ["Ann Intern Med"]
    assert rep["journal_mismatches"] == []
    assert rep["comparison"] is None

    dup = session.scalar(select(Article).where(Article.pmid == "104"))
    canonical = session.scalar(select(Article).where(Article.pmid == "103"))
    assert dup.duplicate_of_id == canonical.id
    no_abstract = session.scalar(select(Article).where(Article.pmid == "106"))
    assert no_abstract.excluded_reason is None and "no_abstract" in no_abstract.flags
    assert session.scalar(select(func.count()).select_from(RunHit)) == 6
    assert session.get(Topic, "internal-medicine").suggested is True

    run2 = ingest_month(session, FakePubMed(), "2026-10", data)
    assert run2.report["comparison"] == {
        "previous_run_id": run1.id,
        "identical": True,
        "changed": {},
    }
    assert session.scalar(select(func.count()).select_from(Article)) == 6
    assert session.scalar(select(func.count()).select_from(QueryVersion)) == 4


def test_changing_a_topic_creates_a_new_query_version_only_for_it(session):
    data = small_data()
    ingest_month(session, FakePubMed(), "2026-10", data)
    data["taxonomy"]["topics"][0]["journals"].append("Resuscitation Plus")
    ingest_month(session, FakePubMed(), "2026-10", data)
    rows = session.execute(
        select(QueryVersion.source_key, func.max(QueryVersion.version)).group_by(
            QueryVersion.source_key
        )
    ).all()
    versions = {key: v for key, v in rows}
    assert versions == {
        "pool:core-em": 1,
        "pool:general": 1,
        "topic:resuscitation": 2,
        "topic:internal-medicine": 1,
    }


def test_failed_run_is_recorded(session):
    class Broken(FakePubMed):
        def search(self, db, term):
            raise RuntimeError("PubMed down")

    with pytest.raises(RuntimeError):
        ingest_month(session, Broken(), "2026-10", small_data())
    from kria.db.models import IngestRun

    run = session.scalar(select(IngestRun))
    assert run.status == "failed" and "PubMed down" in run.error
