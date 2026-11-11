import datetime as dt
from pathlib import Path

from kria.pipeline.pubmed_xml import parse

FIXTURE = Path(__file__).parent / "fixtures" / "pubmed_sample.xml"


def records():
    return {r["pmid"]: r for r in parse(FIXTURE.read_bytes())}


def test_full_record_fields():
    r = records()["90000001"]
    assert r["title"] == "Early Balanced Crystalloids in Sepsis: A Randomized Trial."
    assert r["abstract"] == "STUDY OBJECTIVE: To test fluids.\nRESULTS: Mortality was 20% vs 25%."
    assert r["doi"] == "10.9999/synth.0001"  # ArticleIdList wins and DOIs are lowercased
    assert r["pmcid"] == "PMC9000001"
    assert r["journal_ta"] == "Ann Emerg Med" and r["nlm_id"] == "8002646"
    assert r["pub_types"] == ["Randomized Controlled Trial", "Journal Article"]
    assert r["pub_date"] == "2026 Nov"
    assert r["latest_pub_date"] == dt.date(2026, 11, 30)  # month only: rounded up
    assert r["entrez_date"] == dt.date(2026, 10, 7)
    assert r["authors"] == ["Cohen D", "Synthetic Trial Group"]  # invalid author skipped
    assert r["mesh"] == ["Sepsis"] and r["keywords"] == ["fluids"]
    assert r["languages"] == ["eng"]
    assert r["flags"] == ["ahead_of_print"]


def test_sparse_record_is_flagged_not_guessed():
    r = records()["90000002"]
    assert r["abstract"] is None
    assert r["doi"] == "10.9999/synth.0002"  # falls back to ELocationID
    assert r["pmcid"] is None
    assert r["pub_date"] == "2026 Sep-Oct"
    assert r["latest_pub_date"] == dt.date(2026, 10, 31)
    assert r["entrez_date"] == dt.date(2026, 10, 31)  # month given as a name
    assert r["languages"] == ["fre"]
    assert r["flags"] == ["RetractionIn", "no_abstract"]


def test_latest_pub_date_handles_partial_and_electronic_dates():
    import xml.etree.ElementTree as ET

    from kria.pipeline.pubmed_xml import _latest_pub_date

    def latest(pubdate, extra=""):
        art = ET.fromstring(
            f"<Article><Journal><JournalIssue><PubDate>{pubdate}</PubDate></JournalIssue>"
            f"</Journal>{extra}</Article>"
        )
        return _latest_pub_date(art)

    assert latest("<Year>2026</Year>") == dt.date(2026, 12, 31)
    assert latest("<MedlineDate>2025 Dec-2026 Jan</MedlineDate>") == dt.date(2026, 1, 31)
    assert latest("<MedlineDate>Winter 2026</MedlineDate>") == dt.date(2026, 12, 31)
    epub = (
        "<ArticleDate DateType='Electronic'>"
        "<Year>2026</Year><Month>10</Month><Day>06</Day></ArticleDate>"
    )
    assert latest("<Year>2024</Year><Month>12</Month>", epub) == dt.date(2026, 10, 6)
    assert latest("<Season>Spring</Season>") is None
