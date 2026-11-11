"""Parse PubMed efetch XML into plain dictionaries.

Only fields that PubMed itself provides are read. Nothing is inferred: a missing abstract stays
missing and is flagged, so screening and appraisal can say "not reported" instead of guessing.
Element names follow the PubMed DTD: https://dtd.nlm.nih.gov/ncbi/pubmed/
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
import xml.etree.ElementTree as ET

MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
MONTH_RE = re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", re.IGNORECASE)
MAX_AUTHORS = 50


def _text(el: ET.Element | None) -> str:
    """All text inside an element, including nested markup such as <i> or <sup>."""
    if el is None:
        return ""
    return " ".join("".join(el.itertext()).split())


def _date(el: ET.Element | None) -> dt.date | None:
    if el is None:
        return None
    try:
        y = int(_text(el.find("Year")))
        m_raw = _text(el.find("Month")) or "1"
        m = int(m_raw) if m_raw.isdigit() else MONTHS.get(m_raw[:3].lower(), 1)
        d = int(_text(el.find("Day")) or 1)
        return dt.date(y, m, d)
    except ValueError:
        return None


def _pub_date(journal_issue: ET.Element | None) -> str | None:
    if journal_issue is None:
        return None
    pd = journal_issue.find("PubDate")
    if pd is None:
        return None
    medline = _text(pd.find("MedlineDate"))
    if medline:
        return medline
    parts = [_text(pd.find(t)) for t in ("Year", "Month", "Day")]
    return " ".join(p for p in parts if p) or None


def _month_number(raw: str) -> int | None:
    if raw.isdigit():
        return int(raw)
    return MONTHS.get(raw[:3].lower())


def _end_of(year: int, month: int | None, day: int | None) -> dt.date:
    """Latest date consistent with a partial date: "2026" -> Dec 31, "2026 Oct" -> Oct 31."""
    month = month or 12
    return dt.date(year, month, day or calendar.monthrange(year, month)[1])


def _latest_pub_date(article: ET.Element) -> dt.date | None:
    """Latest of the electronic and print publication dates, rounding partial dates up.

    Used to recognise old articles that a journal deposits in bulk when PubMed starts indexing
    it. Rounding up means an article is never treated as older than it might be.
    """
    dates = [d for d in (_date(a) for a in article.findall("ArticleDate")) if d]
    pd = article.find("Journal/JournalIssue/PubDate")
    if pd is not None:
        try:
            year = _text(pd.find("Year"))
            if year.isdigit():
                day = _text(pd.find("Day"))
                dates.append(
                    _end_of(
                        int(year),
                        _month_number(_text(pd.find("Month"))),
                        int(day) if day.isdigit() else None,
                    )
                )
            else:
                # "2026 Sep-Oct" -> Oct 31, "2025 Dec-2026 Jan" -> Jan 31, "Winter 2026" -> Dec 31
                medline = _text(pd.find("MedlineDate"))
                years = [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", medline)]
                if years:
                    last = max(years)
                    tail = medline[medline.rfind(str(last)) :]
                    months = [MONTHS[m.lower()] for m in MONTH_RE.findall(tail)]
                    dates.append(_end_of(last, max(months) if months else None, None))
        except ValueError:
            pass
    return max(dates) if dates else None


def _abstract(article: ET.Element) -> str | None:
    parts = []
    for at in article.findall("Abstract/AbstractText"):
        body = _text(at)
        if not body:
            continue
        label = at.get("Label")
        parts.append(f"{label}: {body}" if label else body)
    return "\n".join(parts) or None


def _authors(article: ET.Element) -> list[str]:
    names = []
    for au in article.findall("AuthorList/Author"):
        if au.get("ValidYN", "Y") != "Y":
            continue
        collective = _text(au.find("CollectiveName"))
        if collective:
            names.append(collective)
            continue
        last, initials = _text(au.find("LastName")), _text(au.find("Initials"))
        if last:
            names.append(f"{last} {initials}".strip())
    return names[:MAX_AUTHORS]


def parse_article(pa: ET.Element) -> dict:
    mc = pa.find("MedlineCitation")
    art = mc.find("Article")
    data = pa.find("PubmedData")
    ids = (
        {}
        if data is None
        else {a.get("IdType"): _text(a) for a in data.findall("ArticleIdList/ArticleId")}
    )
    doi = ids.get("doi") or next(
        (_text(e) for e in art.findall("ELocationID") if e.get("EIdType") == "doi"), None
    )
    history = (
        {}
        if data is None
        else {h.get("PubStatus"): h for h in data.findall("History/PubMedPubDate")}
    )
    pub_types = [_text(p) for p in art.findall("PublicationTypeList/PublicationType")]
    flags = []
    if data is not None and _text(data.find("PublicationStatus")) == "aheadofprint":
        flags.append("ahead_of_print")
    for cc in mc.findall("CommentsCorrectionsList/CommentsCorrections"):
        ref = cc.get("RefType")
        if ref in ("RetractionIn", "ErratumIn", "ExpressionOfConcernIn", "UpdateIn"):
            flags.append(ref)
    abstract = _abstract(art)
    if not abstract:
        flags.append("no_abstract")
    title = _text(art.find("ArticleTitle")) or _text(art.find("VernacularTitle"))
    return {
        "pmid": _text(mc.find("PMID")),
        "doi": doi.lower() if doi else None,
        "pmcid": ids.get("pmc") or None,
        "title": title,
        "abstract": abstract,
        "journal": _text(art.find("Journal/Title")),
        "journal_ta": _text(mc.find("MedlineJournalInfo/MedlineTA")),
        "nlm_id": _text(mc.find("MedlineJournalInfo/NlmUniqueID")) or None,
        "pub_types": pub_types,
        "pub_date": _pub_date(art.find("Journal/JournalIssue")),
        "latest_pub_date": _latest_pub_date(art),
        "entrez_date": _date(history.get("entrez")),
        "languages": [_text(lang) for lang in art.findall("Language")],
        "authors": _authors(art),
        "mesh": [_text(m) for m in mc.findall("MeshHeadingList/MeshHeading/DescriptorName")],
        "keywords": [_text(k) for k in mc.findall("KeywordList/Keyword") if _text(k)],
        "flags": sorted(set(flags)),
    }


def parse(xml: bytes) -> list[dict]:
    """Every PubmedArticle in an efetch response. Book records are skipped."""
    root = ET.fromstring(xml)
    return [parse_article(pa) for pa in root.findall("PubmedArticle")]
