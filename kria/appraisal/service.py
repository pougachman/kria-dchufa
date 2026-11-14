"""Requesting, feeding and approving appraisals (used by the editor screens)."""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from kria.appraisal import engine
from kria.appraisal.fulltext import (
    MAX_PDF_BYTES,
    OpenAccess,
    is_pdf,
    matches_article,
    units_from_pdf,
)
from kria.db.models import Appraisal, Article

DOI_RE = re.compile(r"(10\.\d{4,9}/\S+)", re.IGNORECASE)


def parse_identifier(raw: str) -> tuple[str, str]:
    """("pmid", "12345678") or ("doi", "10.1056/nejmoa...") from what the editor typed."""
    raw = raw.strip()
    m = DOI_RE.search(raw)
    if m:
        return "doi", m.group(1).rstrip(".,;)").lower()
    digits = re.sub(r"\D", "", raw)
    if digits and len(digits) <= 9 and re.fullmatch(r"\s*(pmid:?\s*)?\d+\s*", raw, re.I):
        return "pmid", digits
    raise ValueError("נא להזין PMID (מספר) או DOI (מתחיל ב-10.)")


def find_article(session: Session, eu, raw: str) -> Article:
    """The article for a PMID or DOI, fetched from PubMed if it is not stored yet."""
    from kria.pipeline import pubmed_xml
    from kria.pipeline.ingest import ARTICLE_FIELDS

    kind, value = parse_identifier(raw)
    q = Article.pmid == value if kind == "pmid" else Article.doi == value
    article = session.scalar(select(Article).where(q))
    if article is not None:
        return article
    if kind == "doi":
        ids, _ = eu.search("pubmed", f'"{value}"[doi]')
        if not ids:
            raise ValueError(f"לא נמצא ב-PubMed מאמר עם ה-DOI {value}")
        value = ids[0]
        article = session.scalar(select(Article).where(Article.pmid == value))
        if article is not None:
            return article
    records = [r for xml in eu.fetch_pubmed_xml([value]) for r in pubmed_xml.parse(xml)]
    rec = next((r for r in records if r["pmid"] == value), None)
    if rec is None:
        raise ValueError(f"לא נמצא ב-PubMed מאמר עם PMID {value}")
    article = Article(pmid=rec["pmid"], excluded_reason=None)
    for f in ARTICLE_FIELDS:
        setattr(article, f, rec[f])
    session.add(article)
    session.flush()
    return article


def request(session: Session, article: Article, oa: OpenAccess) -> Appraisal:
    """Create an appraisal and look for an open-access full text."""
    appraisal = Appraisal(
        article_id=article.id,
        status="needs_pdf",
        public_token=engine.new_token(),
        requested_at=engine.now(),
        steps={},
    )
    session.add(appraisal)
    session.flush()
    _lookup(session, appraisal, article, oa)
    session.commit()
    return appraisal


def retry_lookup(session: Session, appraisal: Appraisal, oa: OpenAccess) -> bool:
    """Search for an open-access full text again (for an appraisal still waiting for one)."""
    if appraisal.status != "needs_pdf":
        raise ValueError("החיפוש החוזר מיועד לניתוח שממתין לטקסט מלא")
    article = session.get(Article, appraisal.article_id)
    _lookup(session, appraisal, article, oa)
    session.commit()
    return appraisal.status == "queued"


def _lookup(session: Session, appraisal: Appraisal, article: Article, oa: OpenAccess) -> None:
    try:
        ft, log = oa.find(article.pmcid, article.doi)
    except Exception as exc:  # a lookup failure must not block a manual upload
        ft, log = None, {"error": repr(exc)[:300]}
    appraisal.lookup = log
    if ft is not None:
        if ft.kind == "pmc_xml":
            content, name = ft.raw, "pmc.xml"  # re-parsed at run time
        else:
            content, name = ft.pdf, "open-access.pdf"
        engine.store_file(session, appraisal, content, name, uploader="open-access")
        appraisal.source, appraisal.source_url, appraisal.status = ft.kind, ft.url, "queued"


def attach_upload(
    session: Session, appraisal: Appraisal, content: bytes, filename: str, uploader: str
) -> str:
    """Check an uploaded PDF against the article and keep it for this appraisal."""
    if not is_pdf(content):
        raise ValueError("הקובץ אינו PDF")
    if len(content) > MAX_PDF_BYTES:
        raise ValueError("הקובץ גדול מדי (יותר מ-40MB)")
    article = session.get(Article, appraisal.article_id)
    try:
        units = units_from_pdf(content)
    except Exception as exc:
        raise ValueError(f"לא ניתן לקרוא את ה-PDF ({exc.__class__.__name__})") from None
    first = " ".join(u.text for u in units[:3])
    ok, reason = matches_article(first, article.doi, article.title)
    if not ok:
        raise ValueError(f"נראה שהקובץ אינו המאמר הזה: {reason}")
    engine.delete_files(session, appraisal.id)
    engine.store_file(session, appraisal, content, filename or "upload.pdf", uploader=uploader)
    appraisal.source, appraisal.source_url, appraisal.status = "upload", None, "queued"
    session.commit()
    return reason


def use_abstract(session: Session, appraisal: Appraisal) -> None:
    engine.delete_files(session, appraisal.id)
    appraisal.source, appraisal.status, appraisal.limited = "abstract", "queued", True
    session.commit()


def approve(session: Session, appraisal: Appraisal) -> int:
    """Approve: publish at /a/<token> and delete the full-text file now."""
    if appraisal.status != "review":
        raise ValueError("אפשר לאשר רק ניתוח שהושלם וממתין לבדיקה")
    deleted = engine.delete_files(session, appraisal.id)
    appraisal.status, appraisal.approved_at = "approved", engine.now()
    # An earlier approved version of the same article is replaced; its link keeps working and
    # points to this one.
    for old in session.scalars(
        select(Appraisal).where(
            Appraisal.article_id == appraisal.article_id,
            Appraisal.status == "approved",
            Appraisal.id != appraisal.id,
        )
    ):
        old.status = "superseded"
    session.commit()
    return deleted


def latest_approved(session: Session, article_id: int) -> Appraisal | None:
    return session.scalar(
        select(Appraisal)
        .where(Appraisal.article_id == article_id, Appraisal.status == "approved")
        .order_by(Appraisal.id.desc())
    )


def reject(session: Session, appraisal: Appraisal) -> None:
    engine.delete_files(session, appraisal.id)
    appraisal.status = "rejected"
    session.commit()


def approved_for(session: Session, article_ids: list[int]) -> dict[int, Appraisal]:
    if not article_ids:
        return {}
    rows = session.scalars(
        select(Appraisal)
        .where(Appraisal.article_id.in_(article_ids), Appraisal.status == "approved")
        .order_by(Appraisal.id)
    )
    return {a.article_id: a for a in rows}
