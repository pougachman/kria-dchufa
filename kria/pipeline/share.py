"""Ready-to-send WhatsApp messages for residents (the editor sends them; nothing is automatic).

Yoav, 2026-11-14: residents get the monthly digest and the monthly appraisal in the program's
WhatsApp group. WhatsApp's business API cannot post to groups and unofficial bots break its
terms, so the editor screen prepares the text and opens WhatsApp with it; the editor picks the
group and presses send. WhatsApp formatting: *bold*, _italic_; the first link gets a preview.
"""

from __future__ import annotations

import secrets
from urllib.parse import quote

from sqlalchemy.orm import Session

from kria.db.models import Appraisal, Article, PublishedMonth
from kria.pipeline import digest as dg

NUMBERS = ["1️⃣", "2️⃣", "3️⃣"]


def month_page(session: Session, month: str, create: bool = True) -> PublishedMonth | None:
    page = session.get(PublishedMonth, month)
    if page is None and create:
        page = PublishedMonth(
            month=month, public_token=secrets.token_urlsafe(18), created_at=dg.now()
        )
        session.add(page)
        session.commit()
    return page


def digest_message(month: str, content: dict, page_url: str) -> str:
    lines = [
        f"*קריאה דחופה · {dg.month_he(month)}*",
        f"{content['count']} מאמרים נבחרים לרפואה דחופה, שאושרו על ידי העורך.",
        "",
        "📚 כל המאמרים של החודש, לפי נושא:",
        page_url,
        "",
        "*החודש חובה לקרוא:*",
    ]
    for num, item in zip(NUMBERS, content["top"], strict=False):
        link = item.get("appraisal_url") or f"https://pubmed.ncbi.nlm.nih.gov/{item['pmid']}/"
        lines += [
            "",
            f"{num} *{item['title'].rstrip('.')}* ({item['journal']})",
            f"_{dg.best_reason(item)}_",
            link,
        ]
    return "\n".join(lines)


def appraisal_message(appraisal: Appraisal, article: Article, url: str) -> str:
    report = appraisal.report or {}
    year = (article.pub_date or "")[:4]
    source = f"{article.journal_ta}{' ' + year if year else ''}"
    lines = [
        "*ניתוח ביקורתי החודש · קריאה דחופה*",
        "",
        f"*{article.title.rstrip('.')}* ({source})",
        "",
        f"*Bottom line:* {report.get('bottom_line', '').strip()}",
        "",
        "לניתוח המלא (כ-4 דקות קריאה):",
        url,
    ]
    if appraisal.limited:
        lines.insert(3, "_ניתוח מוגבל: נכתב מהתקציר בלבד_")
    return "\n".join(lines)


def whatsapp_link(text: str) -> str:
    """Opens WhatsApp (app on a phone, WhatsApp Web on a computer) with the text ready."""
    return "https://wa.me/?text=" + quote(text)
