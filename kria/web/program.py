"""Program screens (M5): the national editor manages programs and curators; each curator sees
only their own program. Public program digest pages at /p/<token>."""

from __future__ import annotations

import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from kria.config import settings
from kria.db.models import Curator, Program, ProgramMonth
from kria.pipeline import digest as dg
from kria.pipeline import programs as pg
from kria.pipeline import share as sh
from kria.pipeline.ingest import parse_month
from kria.web import auth
from kria.web.deps import get_session, templates
from kria.web.editor import _to_login, is_editor
from kria.web.labels import DESIGN_HE, SCORE_HE

router = APIRouter()
MODE_HE = {"automatic": "אוטומטי", "curated": "אוצרות מלאה"}


def _valid(month: str) -> str:
    try:
        parse_month(month)
    except ValueError:
        raise HTTPException(status_code=404) from None
    return month


def _taxonomy():
    return dg.screen.load_taxonomy()


# ---- national editor: programs and curators -----------------------------------------------------


def _programs_page(request, session, **extra):
    rows = session.scalars(select(Program).order_by(Program.name)).all()
    curators = session.scalars(select(Curator).order_by(Curator.username)).all()
    by_program: dict[int, list[Curator]] = {}
    for c in curators:
        by_program.setdefault(c.program_id, []).append(c)
    return templates.TemplateResponse(
        request,
        "editor_programs.html",
        {"programs": rows, "curators": by_program, "mode_he": MODE_HE, **extra},
    )


@router.get("/editor/programs", response_class=HTMLResponse)
def programs_list(request: Request, session: Session = Depends(get_session)):  # noqa: B008
    if not is_editor(request):
        return _to_login(request)
    return _programs_page(request, session)


@router.post("/editor/programs", response_class=HTMLResponse)
def programs_create(
    request: Request,
    name: str = Form(...),
    session: Session = Depends(get_session),  # noqa: B008
):
    if not is_editor(request):
        return _to_login(request)
    try:
        pg.create_program(session, name)
    except ValueError as exc:
        session.rollback()
        return _programs_page(request, session, error=str(exc))
    return RedirectResponse("/editor/programs", status_code=303)


@router.post("/editor/programs/{program_id}/curators", response_class=HTMLResponse)
def curators_create(
    request: Request,
    program_id: int,
    username: str = Form(...),
    display_name: str = Form(""),
    session: Session = Depends(get_session),  # noqa: B008
):
    if not is_editor(request):
        return _to_login(request)
    program = session.get(Program, program_id) or _404()
    try:
        curator, password = pg.create_curator(session, program, username, display_name)
    except ValueError as exc:
        session.rollback()
        return _programs_page(request, session, error=str(exc))
    # The temporary password is shown once, on this response only, never stored in plain text.
    return _programs_page(
        request, session, new_login={"curator": curator, "password": password, "program": program}
    )


@router.post("/editor/curators/{curator_id}/reset", response_class=HTMLResponse)
def curators_reset(
    request: Request,
    curator_id: int,
    session: Session = Depends(get_session),  # noqa: B008
):
    if not is_editor(request):
        return _to_login(request)
    curator = session.get(Curator, curator_id) or _404()
    password = pg.reset_password(session, curator)
    program = session.get(Program, curator.program_id)
    return _programs_page(
        request, session, new_login={"curator": curator, "password": password, "program": program}
    )


@router.post("/editor/curators/{curator_id}/active")
def curators_active(
    request: Request,
    curator_id: int,
    active: int = Form(...),
    session: Session = Depends(get_session),  # noqa: B008
):
    if not is_editor(request):
        return _to_login(request)
    curator = session.get(Curator, curator_id) or _404()
    curator.active = bool(active)
    session.commit()
    return RedirectResponse("/editor/programs", status_code=303)


def _404():
    raise HTTPException(status_code=404)


# ---- curator login -------------------------------------------------------------------------------


def _curator(request: Request, session: Session):
    """(curator, program) or (None, None)."""
    c = auth.current_curator(request, session)
    if c is None:
        return None, None
    return c, session.get(Program, c.program_id)


def _to_program_login(request: Request) -> RedirectResponse:
    return RedirectResponse(f"/program/login?next={quote(request.url.path)}", status_code=303)


@router.get("/program/login", response_class=HTMLResponse)
def program_login_form(request: Request, next: str = "/program", error: int = 0):
    return templates.TemplateResponse(request, "program_login.html", {"next": next, "error": error})


@router.post("/program/login")
def program_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/program"),
    session: Session = Depends(get_session),  # noqa: B008
):
    c = session.scalar(select(Curator).where(Curator.username == username.strip().lower()))
    if c is None or not c.active or not auth.verify_password(password, c.password_hash):
        time.sleep(1)
        return RedirectResponse(f"/program/login?error=1&next={quote(next)}", status_code=303)
    c.last_login_at = pg.now()
    session.commit()
    target = next if next.startswith("/program") else "/program"
    if c.must_change_password:
        target = "/program/password"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(
        auth.COOKIE,
        auth.make_cookie(session, c),
        max_age=auth.MAX_AGE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return resp


@router.post("/program/logout")
def program_logout():
    resp = RedirectResponse("/program/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@router.get("/program/password", response_class=HTMLResponse)
def password_form(
    request: Request,
    error: str | None = None,
    session: Session = Depends(get_session),  # noqa: B008
):
    c, program = _curator(request, session)
    if c is None:
        return _to_program_login(request)
    return templates.TemplateResponse(
        request,
        "program_password.html",
        {"curator": c, "program": program, "error": error, "min": auth.MIN_PASSWORD},
    )


@router.post("/program/password")
def password_change(
    request: Request,
    current: str = Form(...),
    new: str = Form(...),
    again: str = Form(...),
    session: Session = Depends(get_session),  # noqa: B008
):
    c, _ = _curator(request, session)
    if c is None:
        return _to_program_login(request)
    error = None
    if not auth.verify_password(current, c.password_hash):
        error = "הסיסמה הנוכחית שגויה"
    elif new != again:
        error = "שתי הסיסמאות החדשות אינן זהות"
    elif new == current:
        error = "הסיסמה החדשה צריכה להיות שונה מהנוכחית"
    else:
        error = auth.password_problem(new)
    if error:
        return RedirectResponse(f"/program/password?error={quote(error)}", status_code=303)
    c.password_hash, c.must_change_password = auth.hash_password(new), False
    session.commit()
    resp = RedirectResponse("/program", status_code=303)
    resp.set_cookie(
        auth.COOKIE,
        auth.make_cookie(session, c),
        max_age=auth.MAX_AGE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
    )
    return resp


def _guard(request, session):
    """(curator, program, redirect) for curator pages."""
    c, program = _curator(request, session)
    if c is None:
        return None, None, _to_program_login(request)
    if c.must_change_password:
        return None, None, RedirectResponse("/program/password", status_code=303)
    return c, program, None


# ---- curator pages -------------------------------------------------------------------------------


@router.get("/program", response_class=HTMLResponse)
def program_home(request: Request, session: Session = Depends(get_session)):  # noqa: B008
    c, program, redirect = _guard(request, session)
    if redirect:
        return redirect
    names = {t["id"]: t["he"] for t in _taxonomy()["topics"]}
    rows = []
    for month in pg.months(session):
        content = pg.content(session, program, month)
        rows.append({"month": month, "month_he": dg.month_he(month), "count": content["count"]})
    return templates.TemplateResponse(
        request,
        "program_home.html",
        {
            "curator": c,
            "program": program,
            "months": rows,
            "mode_he": MODE_HE,
            "topic_names": [names.get(t, t) for t in program.topics],
        },
    )


@router.get("/program/settings", response_class=HTMLResponse)
def settings_form(
    request: Request,
    saved: int = 0,
    session: Session = Depends(get_session),  # noqa: B008
):
    c, program, redirect = _guard(request, session)
    if redirect:
        return redirect
    tax = _taxonomy()
    return templates.TemplateResponse(
        request,
        "program_settings.html",
        {
            "curator": c,
            "program": program,
            "groups": tax["groups"],
            "topics": tax["topics"],
            "mode_he": MODE_HE,
            "saved": saved,
        },
    )


@router.post("/program/settings")
async def settings_save(
    request: Request,
    session: Session = Depends(get_session),  # noqa: B008
):
    c, program, redirect = _guard(request, session)
    if redirect:
        return redirect
    form = await request.form()
    try:
        pg.set_settings(session, program, form.get("mode", ""), form.getlist("topics"))
    except ValueError:
        session.rollback()
        raise HTTPException(status_code=400) from None
    return RedirectResponse("/program/settings?saved=1", status_code=303)


@router.get("/program/{month}", response_class=HTMLResponse)
def program_month(
    request: Request,
    month: str,
    show: int = 40,
    error: str | None = None,
    full: int = 0,
    session: Session = Depends(get_session),  # noqa: B008
):
    c, program, redirect = _guard(request, session)
    if redirect:
        return redirect
    _valid(month)
    content = pg.content(session, program, month)
    share = None
    if content["count"]:
        page = pg.month_page(session, program, month)
        page_url = f"{settings.public_url}/p/{page.public_token}"
        text = sh.digest_message(month, content, page_url, program=program.name)
        share = {"text": text, "link": sh.whatsapp_link(text), "page_url": page_url}
    items = pg.candidates(session, program, month) if program.mode == "curated" else []
    names = {t["id"]: t["he"] for t in _taxonomy()["topics"]}
    return templates.TemplateResponse(
        request,
        "program_month.html",
        {
            "curator": c,
            "program": program,
            "month": month,
            "month_he": dg.month_he(month),
            "content": content,
            "share": share,
            "items": items[: max(show, 40)],
            "total": len(items),
            "show": max(show, 40),
            "approved": sum(1 for i in items if i["decision"] == "approved"),
            "topics": names,
            "designs": DESIGN_HE,
            "scores": SCORE_HE,
            "mode_he": MODE_HE,
            "error": error,
            "featured_full": bool(full),
            "featured_max": pg.FEATURED,
        },
    )


@router.post("/program/{month}/decide")
def program_decide(
    request: Request,
    month: str,
    article_id: int = Form(...),
    action: str = Form(...),
    show: int = Form(40),
    session: Session = Depends(get_session),  # noqa: B008
):
    c, program, redirect = _guard(request, session)
    if redirect:
        return redirect
    _valid(month)
    back = f"/program/{month}?show={show}"
    try:
        if action in ("approved", "rejected"):
            pg.decide(session, program, month, article_id, action, c)
        elif action == "clear":
            pg.decide(session, program, month, article_id, None, c)
        elif action in ("feature", "unfeature"):
            if not pg.feature(session, program, month, article_id, action == "feature", c):
                back += "&full=1"
        elif action.startswith("rate-"):
            value = action.removeprefix("rate-")
            pg.rate(session, program, month, article_id, int(value) if value != "0" else None, c)
        else:
            raise ValueError("פעולה לא מוכרת")
    except ValueError as exc:
        session.rollback()
        return RedirectResponse(f"{back}&error={quote(str(exc))}", status_code=303)
    return RedirectResponse(f"{back}#a{article_id}", status_code=303)


@router.post("/program/{month}/add")
def program_add(
    request: Request,
    month: str,
    identifier: str = Form(...),
    session: Session = Depends(get_session),  # noqa: B008
):
    from kria.appraisal import service
    from kria.web.appraisals import _eu

    c, program, redirect = _guard(request, session)
    if redirect:
        return redirect
    _valid(month)
    try:
        article = service.find_article(session, _eu(request), identifier)
        pg.add_article(session, program, month, article, c)
    except ValueError as exc:
        session.rollback()
        return RedirectResponse(f"/program/{month}?error={quote(str(exc))}", status_code=303)
    except Exception as exc:
        session.rollback()
        msg = f"PubMed לא זמין כרגע ({exc.__class__.__name__})"
        return RedirectResponse(f"/program/{month}?error={quote(msg)}", status_code=303)
    return RedirectResponse(f"/program/{month}#a{article.id}", status_code=303)


# ---- public program digest -----------------------------------------------------------------------


@router.get("/p/{token}", response_class=HTMLResponse)
def public_program_digest(
    token: str,
    session: Session = Depends(get_session),  # noqa: B008
):
    page = session.scalar(select(ProgramMonth).where(ProgramMonth.public_token == token))
    if page is None:
        raise HTTPException(status_code=404)
    program = session.get(Program, page.program_id)
    content = pg.content(session, program, page.month)
    if not content["count"] or not program.active:
        raise HTTPException(status_code=404)
    return HTMLResponse(dg.render(page.month, content, settings.public_url)[1])
