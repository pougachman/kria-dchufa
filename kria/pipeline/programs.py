"""Residency programs (milestone M5): what each program's digest contains.

Two modes (Yoav, 2026-11-16):
- automatic: the national digest (what the national editor approved), filtered by the
  program's topics (no topics chosen = all topics). The national top articles stay on top
  when they match.
- curated: the program's curator works on the full ranked candidate list of the month (the
  same list the national editor sees, before national approval): approve, reject, rate 1-5,
  mark up to 3 top articles, and add articles by PMID or DOI. These decisions and ratings are
  private to the program.
"""

from __future__ import annotations

import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kria.db.models import Article, Curator, Program, ProgramDecision, ProgramMonth, Screening
from kria.pipeline import digest as dg

FEATURED = 3
MODES = ("automatic", "curated")


def now():
    return dg.now()


# ---- programs and curators ---------------------------------------------------------------------


def create_program(session: Session, name: str, mode: str = "automatic") -> Program:
    name = " ".join(name.split())
    if not name:
        raise ValueError("נא להזין שם לתוכנית")
    if mode not in MODES:
        raise ValueError("מצב לא מוכר")
    if session.scalar(select(Program).where(Program.name == name)):
        raise ValueError("כבר קיימת תוכנית בשם הזה")
    program = Program(name=name, mode=mode, topics=[], active=True, created_at=now())
    session.add(program)
    session.commit()
    return program


def create_curator(
    session: Session, program: Program, username: str, display_name: str
) -> tuple[Curator, str]:
    """A curator with a temporary password, returned once to show to the editor."""
    from kria.web.auth import hash_password, temporary_password

    username = username.strip().lower()
    if not username or not username.replace(".", "").replace("-", "").replace("_", "").isalnum():
        raise ValueError("שם משתמש: אותיות באנגלית, ספרות, נקודה, מקף או קו תחתון")
    if session.scalar(select(Curator).where(Curator.username == username)):
        raise ValueError("שם המשתמש כבר תפוס")
    password = temporary_password()
    curator = Curator(
        program_id=program.id,
        username=username,
        display_name=" ".join(display_name.split()) or username,
        password_hash=hash_password(password),
        must_change_password=True,
        active=True,
        created_at=now(),
    )
    session.add(curator)
    session.commit()
    return curator, password


def reset_password(session: Session, curator: Curator) -> str:
    from kria.web.auth import hash_password, temporary_password

    password = temporary_password()
    curator.password_hash, curator.must_change_password = hash_password(password), True
    session.commit()
    return password


def set_settings(session: Session, program: Program, mode: str, topics: list[str]) -> None:
    valid = {t["id"] for t in dg.screen.load_taxonomy()["topics"]}
    if mode not in MODES:
        raise ValueError("מצב לא מוכר")
    unknown = [t for t in topics if t not in valid]
    if unknown:
        raise ValueError("נושא לא מוכר")
    program.mode, program.topics = mode, sorted(set(topics))
    session.commit()


# ---- the program's month ------------------------------------------------------------------------


def _matches(item: dict, topics: list[str]) -> bool:
    return not topics or bool(set(item.get("topics") or []) & set(topics))


def _decisions(session: Session, program: Program, month: str) -> dict[int, ProgramDecision]:
    rows = session.scalars(
        select(ProgramDecision).where(
            ProgramDecision.program_id == program.id, ProgramDecision.month == month
        )
    )
    return {d.article_id: d for d in rows}


def _added_item(session: Session, article: Article) -> dict:
    """An article the curator added by hand: shown without model scores."""
    s = session.scalar(
        select(Screening)
        .where(Screening.article_id == article.id, Screening.status == "ok")
        .order_by(Screening.id.desc())
    )
    return {
        "rank": None,
        "article_id": article.id,
        "pmid": article.pmid,
        "doi": article.doi,
        "title": article.title,
        "journal": article.journal_ta,
        "score": s.high_yield_score if s else None,
        "scores": {}
        if s is None
        else {
            "practice_changing": s.practice_changing,
            "board_exam": s.board_exam,
            "journal_club": s.journal_club,
        },
        "reasons": {}
        if s is None
        else {
            "practice_changing": s.practice_changing_reason,
            "board_exam": s.board_exam_reason,
            "journal_club": s.journal_club_reason,
        },
        "ed": None,
        "design": s.design if s else None,
        "topics": list(s.topics) if s else [],
        "summary": s.summary if s else (article.abstract or "")[:400],
        "no_abstract": "no_abstract" in (article.flags or []),
        "screening_id": s.id if s else None,
        "note": "Added by the program's curator.",
    }


def candidates(session: Session, program: Program, month: str) -> list[dict]:
    """Curated mode: the month's ranked candidates with this program's decisions and ratings,
    plus the articles the program added by hand (at the end)."""
    items = dg.ranked(session, month)
    decisions = _decisions(session, program, month)
    listed = {i["article_id"] for i in items}
    for d in decisions.values():
        if d.added and d.article_id not in listed:
            items.append(_added_item(session, session.get(Article, d.article_id)))
    for item in items:
        d = decisions.get(item["article_id"])
        item["national_decision"] = item.get("decision")
        item["decision"] = d.decision if d else None
        item["rating"] = d.rating if d else None
        item["featured"] = bool(d and d.featured and d.decision == "approved")
        item["added"] = bool(d and d.added)
    return items


def content(session: Session, program: Program, month: str) -> dict:
    """The program's digest for the month."""
    if program.mode == "curated":
        approved = [i for i in candidates(session, program, month) if i["decision"] == "approved"]
    else:
        approved = [
            i
            for i in dg.ranked(session, month)
            if i["decision"] == "approved" and _matches(i, program.topics)
        ]
    return dg.assemble(session, approved)


def _row(session, program, month, article_id, curator) -> ProgramDecision:
    row = session.scalar(
        select(ProgramDecision).where(
            ProgramDecision.program_id == program.id,
            ProgramDecision.month == month,
            ProgramDecision.article_id == article_id,
        )
    )
    if row is None:
        row = ProgramDecision(
            program_id=program.id, month=month, article_id=article_id, featured=False, added=False
        )
        session.add(row)
    row.curator_id = curator.id if curator else None
    row.decided_at = now()
    return row


def _is_candidate(session, program, month, article_id) -> bool:
    return any(i["article_id"] == article_id for i in candidates(session, program, month))


def decide(session, program, month, article_id, decision, curator=None) -> None:
    if program.mode != "curated":
        raise ValueError("בחירת מאמרים זמינה רק במצב אוצרות מלאה")
    if decision not in ("approved", "rejected", None):
        raise ValueError("החלטה לא מוכרת")
    if not _is_candidate(session, program, month, article_id):
        raise ValueError("המאמר אינו ברשימת המועמדים של החודש")
    row = _row(session, program, month, article_id, curator)
    row.decision = decision
    if decision != "approved":
        row.featured = False
    session.commit()


def rate(session, program, month, article_id, rating, curator=None) -> None:
    if program.mode != "curated":
        raise ValueError("דירוג זמין רק במצב אוצרות מלאה")
    if rating is not None and rating not in (1, 2, 3, 4, 5):
        raise ValueError("דירוג בין 1 ל-5")
    if not _is_candidate(session, program, month, article_id):
        raise ValueError("המאמר אינו ברשימת המועמדים של החודש")
    _row(session, program, month, article_id, curator).rating = rating
    session.commit()


def feature(session, program, month, article_id, on, curator=None) -> bool:
    row = _decisions(session, program, month).get(article_id)
    if row is None or row.decision != "approved":
        return False
    if on and not row.featured:
        count = session.scalar(
            select(func.count()).where(
                ProgramDecision.program_id == program.id,
                ProgramDecision.month == month,
                ProgramDecision.featured.is_(True),
                ProgramDecision.decision == "approved",
            )
        )
        if count >= FEATURED:
            return False
    row.featured = on
    session.commit()
    return True


def add_article(session, program, month, article: Article, curator=None) -> None:
    """Add an article (already fetched from PubMed by PMID/DOI) to the program's month."""
    if program.mode != "curated":
        raise ValueError("הוספת מאמרים זמינה רק במצב אוצרות מלאה")
    row = _row(session, program, month, article.id, curator)
    row.added, row.decision = True, row.decision or "approved"
    session.commit()


def month_page(session: Session, program: Program, month: str) -> ProgramMonth:
    page = session.scalar(
        select(ProgramMonth).where(
            ProgramMonth.program_id == program.id, ProgramMonth.month == month
        )
    )
    if page is None:
        page = ProgramMonth(
            program_id=program.id,
            month=month,
            public_token=secrets.token_urlsafe(18),
            created_at=now(),
        )
        session.add(page)
        session.commit()
    return page


def months(session: Session) -> list[str]:
    """Months with a finished screening run, newest first."""
    from kria.db.models import ScreeningRun

    rows = session.scalars(
        select(ScreeningRun.month).where(ScreeningRun.status == "ok").distinct()
    ).all()
    return sorted(set(rows), reverse=True)
