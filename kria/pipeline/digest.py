"""The national editor's review and the monthly digest (milestone M3).

The editor sees the ranked shortlist (kria/pipeline/rank.py) and approves or rejects each
article with one click. The digest is exactly the approved articles, grouped by topic, and is
emailed through Resend. Nothing the editor did not approve goes into it.

Editor time is measured from the editor's own actions (page views and decisions): the gaps
between consecutive actions are added up, and a gap longer than IDLE_CAP counts as IDLE_CAP,
so a page left open overnight does not count as work.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kria.db.models import Article, Digest, EditorDecision, EditorEvent
from kria.pipeline import rank, screen
from kria.pipeline.batches import as_utc

IDLE_CAP = dt.timedelta(minutes=5)
FEATURED = 3
TEMPLATES = Path(__file__).resolve().parents[1] / "web" / "templates"
MONTHS_HE = [
    "ינואר",
    "פברואר",
    "מרץ",
    "אפריל",
    "מאי",
    "יוני",
    "יולי",
    "אוגוסט",
    "ספטמבר",
    "אוקטובר",
    "נובמבר",
    "דצמבר",
]


def month_he(month: str) -> str:
    year, m = month.split("-")
    return f"{MONTHS_HE[int(m) - 1]} {year}"


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ---- candidates and decisions ------------------------------------------------------------------


def ranked(session: Session, month: str) -> list[dict]:
    """Every relevant screened article of the month, ranked, with the editor's decision."""
    items = rank.shortlist(screen.latest_screenings(session, month), size=None)
    rows = session.scalars(select(EditorDecision).where(EditorDecision.month == month)).all()
    decisions = {d.article_id: d.decision for d in rows}
    featured = {d.article_id for d in rows if d.featured and d.decision == "approved"}
    ids = (
        {
            a.pmid: a.id
            for a in session.scalars(
                select(Article).where(Article.pmid.in_([i["pmid"] for i in items]))
            )
        }
        if items
        else {}
    )
    for item in items:
        item["article_id"] = ids[item["pmid"]]
        item["decision"] = decisions.get(item["article_id"])
        item["featured"] = item["article_id"] in featured
    return items


def decide(session: Session, month: str, article_id: int, decision: str | None) -> None:
    """Approve, reject, or clear (decision None) one article for the month's digest."""
    if decision not in ("approved", "rejected", None):
        raise ValueError(f"unknown decision {decision!r}")
    item = next((i for i in ranked(session, month) if i["article_id"] == article_id), None)
    if item is None:
        raise ValueError(f"article {article_id} is not a candidate for {month}")
    row = session.scalar(
        select(EditorDecision).where(
            EditorDecision.month == month, EditorDecision.article_id == article_id
        )
    )
    if decision is None:
        if row is not None:
            session.delete(row)
    else:
        row = row or EditorDecision(month=month, article_id=article_id, featured=False)
        row.decision, row.rank = decision, item["rank"]
        if decision != "approved":
            row.featured = False
        row.screening_id, row.decided_at = item["screening_id"], now()
        session.add(row)
    log_event(session, month, decision or "cleared", article_id)
    session.commit()


def feature(session: Session, month: str, article_id: int, on: bool) -> bool:
    """Mark an approved article as one of the digest's top articles. False if not allowed."""
    row = session.scalar(
        select(EditorDecision).where(
            EditorDecision.month == month, EditorDecision.article_id == article_id
        )
    )
    if row is None or row.decision != "approved":
        return False
    if on and not row.featured:
        count = session.scalar(
            select(func.count()).where(
                EditorDecision.month == month,
                EditorDecision.featured.is_(True),
                EditorDecision.decision == "approved",
            )
        )
        if count >= FEATURED:
            return False
    row.featured = on
    log_event(session, month, "featured" if on else "unfeatured", article_id)
    session.commit()
    return True


def log_event(session: Session, month: str, kind: str, article_id: int | None = None) -> None:
    session.add(EditorEvent(month=month, kind=kind, article_id=article_id, at=now()))


def editor_minutes(times: list[dt.datetime], idle_cap: dt.timedelta = IDLE_CAP) -> float:
    """Minutes of editor work from the times of the editor's actions."""
    times = sorted(as_utc(t) for t in times)
    gaps = (min(b - a, idle_cap) for a, b in zip(times, times[1:], strict=False))
    total = sum(gaps, dt.timedelta())
    return round(total.total_seconds() / 60, 1)


def month_minutes(session: Session, month: str) -> float:
    times = session.scalars(select(EditorEvent.at).where(EditorEvent.month == month)).all()
    return editor_minutes(list(times))


# ---- the digest --------------------------------------------------------------------------------


def content(session: Session, month: str) -> dict:
    """What the digest contains: up to 3 top articles, then the rest grouped by topic.

    Top articles are the ones the editor marked (★); if none are marked, the three best-ranked
    approved articles. They are not repeated in the topic groups.
    """
    taxonomy = screen.load_taxonomy()
    order = {t["id"]: i for i, t in enumerate(taxonomy["topics"])}
    names = {t["id"]: t["he"] for t in taxonomy["topics"]}
    approved = [i for i in ranked(session, month) if i["decision"] == "approved"]
    from kria.appraisal.service import approved_for
    from kria.config import settings

    appraisals = approved_for(session, [i["article_id"] for i in approved])
    for item in approved:
        a = appraisals.get(item["article_id"])
        item["appraisal_url"] = f"{settings.public_url}/a/{a.public_token}" if a else None
    top = [i for i in approved if i["featured"]][:FEATURED] or approved[:FEATURED]
    for item in approved:
        item["topic_names"] = [names.get(t, t) for t in item["topics"]]
    top_ids = {i["article_id"] for i in top}
    groups: dict[str, list[dict]] = {}
    for item in approved:
        if item["article_id"] in top_ids:
            continue
        groups.setdefault(item["topics"][0], []).append(item)
    return {
        "top": top,
        "groups": [
            {"topic": tid, "name": names.get(tid, tid), "items": groups[tid]}
            for tid in sorted(groups, key=lambda t: order.get(t, len(order)))
        ],
        "count": len(approved),
        "article_ids": [i["article_id"] for i in approved],
    }


def render(month: str, digest: dict, public_url: str) -> tuple[str, str, str]:
    """Subject, HTML and plain text of the digest email."""
    from kria.web.labels import DESIGN_HE, SCORE_HE

    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    groups, count = digest["groups"], digest["count"]
    subject = f"קריאה דחופה: {count} מאמרים נבחרים, {month_he(month)}"
    preheader = "החודש חובה לקרוא: " + " · ".join(i["title"] for i in digest["top"])
    context = {
        "month_he": month_he(month),
        "top": digest["top"],
        "groups": groups,
        "count": count,
        "preheader": preheader[:180],
        "designs": DESIGN_HE,
        "scores": SCORE_HE,
        "public_url": public_url,
        "best_reason": best_reason,
    }
    html = env.get_template("email_digest.html").render(**context)
    lines = [
        subject,
        f"{count} מאמרים שאושרו על ידי העורך הלאומי",
        "",
        "== החודש חובה לקרוא ==",
        "",
    ]
    for item in digest["top"]:
        lines += [
            item["title"],
            f"{item['journal']} | {DESIGN_HE.get(item['design'], item['design'])}",
            item["summary"],
            f"Why read it: {best_reason(item)}",
            f"https://pubmed.ncbi.nlm.nih.gov/{item['pmid']}/",
            "",
        ]
    for g in groups:
        lines += [f"== {g['name']} ==", ""]
        for item in g["items"]:
            lines += [
                item["title"],
                f"{item['journal']} | {DESIGN_HE.get(item['design'], item['design'])}",
                item["summary"],
                f"https://pubmed.ncbi.nlm.nih.gov/{item['pmid']}/",
                "",
            ]
    return subject, html, "\n".join(lines)


def best_reason(item: dict) -> str:
    """The reason given for the article's highest score (ties: practice-changing first)."""
    key = max(item["scores"], key=lambda k: item["scores"][k])
    return item["reasons"][key]


class Resend:
    """Sends email through the Resend API (https://resend.com/docs/api-reference)."""

    URL = "https://api.resend.com/emails"

    def __init__(self, api_key: str | None, http: httpx.Client | None = None):
        if not api_key:
            raise RuntimeError("RESEND_API_KEY is not set (Railway: the site's Variables tab)")
        self.api_key = api_key
        self.http = http or httpx.Client(timeout=30)

    def send(self, sender: str, to: list[str], subject: str, html: str, text: str) -> str:
        r = self.http.post(
            self.URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"from": sender, "to": to, "subject": subject, "html": html, "text": text},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Resend refused the email ({r.status_code}): {r.text[:300]}")
        return r.json().get("id", "")


def send_digest(
    session: Session, month: str, mailer, sender: str, recipients: list[str], public_url: str
) -> Digest:
    if not recipients:
        raise RuntimeError("DIGEST_RECIPIENTS is not set (Railway: the site's Variables tab)")
    digest = content(session, month)
    if not digest["count"]:
        raise RuntimeError("no approved articles yet")
    subject, html, text = render(month, digest, public_url)
    provider_id = mailer.send(sender, recipients, subject, html, text)
    log_event(session, month, "digest_sent")
    session.flush()
    sent = Digest(
        month=month,
        article_ids=digest["article_ids"],
        subject=subject,
        html=html,
        text=text,
        recipients=recipients,
        provider_id=provider_id,
        editor_minutes=month_minutes(session, month),
        sent_at=now(),
    )
    session.add(sent)
    session.commit()
    return sent
