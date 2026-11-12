"""The national editor's screens: login, review of the ranked shortlist, digest preview and send.

Until M5 adds users and roles there is one editor password (EDITOR_PASSWORD). A successful login
sets a signed cookie for 30 days. The signing key is derived from the password, so changing the
password logs everyone out.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kria.config import settings
from kria.db.models import Digest, EditorDecision, ScreeningRun
from kria.pipeline import digest as dg
from kria.pipeline import screen
from kria.pipeline.ingest import parse_month
from kria.web.deps import get_session as _session
from kria.web.deps import templates
from kria.web.labels import DESIGN_HE, SCORE_HE

router = APIRouter(prefix="/editor")
COOKIE = "kria_editor"
MAX_AGE = 30 * 24 * 3600
PAGE = 40


# ---- authentication ----------------------------------------------------------------------------


def _key() -> bytes:
    return hashlib.sha256(f"kria-editor-session:{settings.editor_password}".encode()).digest()


def make_token(expires: int) -> str:
    sig = hmac.new(_key(), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def is_editor(request: Request) -> bool:
    if not settings.editor_password:
        return False
    token = request.cookies.get(COOKIE, "")
    expires, _, sig = token.partition(".")
    if not expires.isdigit() or int(expires) < time.time():
        return False
    return hmac.compare_digest(make_token(int(expires)), token)


def _valid(month: str) -> str:
    try:
        parse_month(month)
    except ValueError:
        raise HTTPException(status_code=404) from None
    return month


def _to_login(request: Request) -> RedirectResponse:
    return RedirectResponse(f"/editor/login?next={quote(request.url.path)}", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/editor", error: str | None = None):
    return templates.TemplateResponse(
        request,
        "editor_login.html",
        {"next": next, "error": error, "configured": bool(settings.editor_password)},
    )


@router.post("/login")
def login(request: Request, password: str = Form(...), next: str = Form("/editor")):
    ok = bool(settings.editor_password) and hmac.compare_digest(
        password.strip().encode(), settings.editor_password.encode()
    )
    if not ok:
        time.sleep(1)  # slows down guessing
        return RedirectResponse(f"/editor/login?error=1&next={quote(next)}", status_code=303)
    target = next if next.startswith("/editor") else "/editor"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(
        COOKIE,
        make_token(int(time.time()) + MAX_AGE),
        max_age=MAX_AGE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/editor/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ---- screens -----------------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def months(request: Request, session: Session = Depends(_session)):  # noqa: B008
    if not is_editor(request):
        return _to_login(request)
    done = session.scalars(
        select(ScreeningRun.month).where(ScreeningRun.status == "ok").distinct()
    ).all()
    rows = []
    for month in sorted(set(done), reverse=True):
        counts = dict(
            session.execute(
                select(EditorDecision.decision, func.count())
                .where(EditorDecision.month == month)
                .group_by(EditorDecision.decision)
            ).all()
        )
        sent = session.scalar(
            select(Digest).where(Digest.month == month).order_by(Digest.id.desc())
        )
        rows.append(
            {
                "month": month,
                "month_he": dg.month_he(month),
                "approved": counts.get("approved", 0),
                "rejected": counts.get("rejected", 0),
                "sent_at": sent.sent_at if sent else None,
                "minutes": dg.month_minutes(session, month),
            }
        )
    return templates.TemplateResponse(request, "editor_months.html", {"months": rows})


@router.get("/{month}", response_class=HTMLResponse)
def review(
    request: Request,
    month: str,
    show: int = PAGE,
    session: Session = Depends(_session),  # noqa: B008
):
    if not is_editor(request):
        return _to_login(request)
    _valid(month)
    items = dg.ranked(session, month)
    dg.log_event(session, month, "view")
    session.commit()
    taxonomy = {t["id"]: t["he"] for t in screen.load_taxonomy()["topics"]}
    shown = items[: max(show, PAGE)]
    approved = sum(1 for i in items if i["decision"] == "approved")
    rejected = sum(1 for i in items if i["decision"] == "rejected")
    return templates.TemplateResponse(
        request,
        "editor_review.html",
        {
            "month": month,
            "month_he": dg.month_he(month),
            "items": shown,
            "total": len(items),
            "show": len(shown),
            "approved": approved,
            "rejected": rejected,
            "undecided_shown": sum(1 for i in shown if i["decision"] is None),
            "minutes": dg.month_minutes(session, month),
            "topics": taxonomy,
            "designs": DESIGN_HE,
            "scores": SCORE_HE,
        },
    )


@router.post("/{month}/decide")
def decide(
    request: Request,
    month: str,
    article_id: int = Form(...),
    decision: str = Form(...),
    show: int = Form(PAGE),
    session: Session = Depends(_session),  # noqa: B008
):
    if not is_editor(request):
        return _to_login(request)
    _valid(month)
    dg.decide(session, month, article_id, None if decision == "clear" else decision)
    return RedirectResponse(f"/editor/{month}?show={show}#a{article_id}", status_code=303)


@router.get("/{month}/digest", response_class=HTMLResponse)
def digest_preview(
    request: Request,
    month: str,
    sent: int = 0,
    error: str | None = None,
    session: Session = Depends(_session),  # noqa: B008
):
    if not is_editor(request):
        return _to_login(request)
    _valid(month)
    groups = dg.approved_groups(session, month)
    html = dg.render(month, groups, settings.public_url)[1] if groups else None
    history = session.scalars(
        select(Digest).where(Digest.month == month).order_by(Digest.id.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "editor_digest.html",
        {
            "month": month,
            "month_he": dg.month_he(month),
            "count": sum(len(g["items"]) for g in groups),
            "email_html": html,
            "recipients": settings.recipients,
            "mail_ready": bool(settings.resend_api_key and settings.recipients),
            "history": history,
            "sent": sent,
            "error": error,
        },
    )


@router.post("/{month}/digest/send")
def digest_send(
    request: Request,
    month: str,
    session: Session = Depends(_session),  # noqa: B008
):
    if not is_editor(request):
        return _to_login(request)
    _valid(month)
    try:
        mailer = dg.Resend(settings.resend_api_key)
        dg.send_digest(
            session, month, mailer, settings.digest_from, settings.recipients, settings.public_url
        )
    except RuntimeError as exc:
        session.rollback()
        return RedirectResponse(
            f"/editor/{month}/digest?error={quote(str(exc)[:300])}", status_code=303
        )
    return RedirectResponse(f"/editor/{month}/digest?sent=1", status_code=303)
