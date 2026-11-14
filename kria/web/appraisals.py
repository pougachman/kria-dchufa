"""Appraisal screens: request by PMID/DOI, upload a PDF, follow progress, review, approve.

Editor pages are behind the editor password. An approved appraisal is public at /a/<token>
(an unguessable link), which is what the digest links to. Uploaded PDFs are never served.
"""

from __future__ import annotations

import datetime as dt
from urllib.parse import quote

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from kria.appraisal import engine, service
from kria.appraisal.fulltext import OpenAccess
from kria.config import settings
from kria.db.models import Appraisal, Article, UploadedFile
from kria.pipeline.batches import as_utc
from kria.web.deps import get_session, templates
from kria.web.editor import _to_login, is_editor

router = APIRouter()
STALE = dt.timedelta(minutes=60)
STATUS_HE = {
    "needs_pdf": "ממתין לטקסט מלא",
    "queued": "בתור",
    "running": "בעבודה",
    "review": "ממתין לאישורך",
    "approved": "אושר",
    "rejected": "נדחה",
    "failed": "נכשל",
    "superseded": "הוחלף בגרסה חדשה",
}
SOURCE_HE = {
    "pmc_xml": "טקסט מלא, גישה פתוחה (PMC)",
    "oa_pdf": "PDF בגישה פתוחה (Unpaywall)",
    "upload": "PDF שהעלית",
    "abstract": "תקציר בלבד (ניתוח מוגבל)",
}
STAGE_HE = {
    "extract": "חילוץ נתונים וציטוטים",
    "report": "כתיבת הניתוח",
    "skeptic": "Guest Skeptic",
    "reconcile": "יישוב מחלוקות",
}


def run_in_background(appraisal_id: int) -> None:
    """Default runner: a fresh database session and the Claude client."""
    from kria.db.base import SessionLocal

    with SessionLocal() as session:
        try:
            llm = engine.Claude(settings.anthropic_api_key)
        except Exception as exc:
            a = session.get(Appraisal, appraisal_id)
            a.status, a.error = "failed", repr(exc)
            session.commit()
            return
        engine.run(session, appraisal_id, llm)


def _runner(request: Request):
    return getattr(request.app.state, "appraisal_runner", run_in_background)


def _pubmed():
    from kria.pipeline.eutils import EUtils

    return EUtils(api_key=settings.ncbi_api_key, email=settings.ncbi_email, tool=settings.ncbi_tool)


def _oa(request: Request):
    factory = getattr(request.app.state, "open_access", None)
    return (
        factory()
        if factory
        else OpenAccess(settings.unpaywall_email, ncbi_api_key=settings.ncbi_api_key)
    )


def _eu(request: Request):
    factory = getattr(request.app.state, "pubmed", None)
    return factory() if factory else _pubmed()


def _start(request: Request, background: BackgroundTasks, appraisal: Appraisal) -> None:
    background.add_task(_runner(request), appraisal.id)


def _get(session: Session, appraisal_id: int) -> Appraisal:
    a = session.get(Appraisal, appraisal_id)
    if a is None:
        raise HTTPException(status_code=404)
    return a


def _stale(a: Appraisal) -> bool:
    return (
        a.status in ("running", "queued")
        and a.started_at is not None
        and engine.now() - as_utc(a.started_at) > STALE
    )


# ---- list and request --------------------------------------------------------------------------


@router.get("/editor/appraisals", response_class=HTMLResponse)
def appraisal_list(
    request: Request,
    error: str | None = None,
    session: Session = Depends(get_session),  # noqa: B008
):
    if not is_editor(request):
        return _to_login(request)
    rows = session.execute(
        select(Appraisal, Article)
        .join(Article, Article.id == Appraisal.article_id)
        .order_by(Appraisal.id.desc())
    ).all()
    return templates.TemplateResponse(
        request,
        "editor_appraisals.html",
        {
            "rows": rows,
            "status_he": STATUS_HE,
            "source_he": SOURCE_HE,
            "error": error,
            "key_ok": bool(settings.anthropic_api_key),
        },
    )


@router.post("/editor/appraisals")
def appraisal_request(
    request: Request,
    background: BackgroundTasks,
    identifier: str = Form(...),
    session: Session = Depends(get_session),  # noqa: B008
):
    if not is_editor(request):
        return _to_login(request)
    try:
        article = service.find_article(session, _eu(request), identifier)
    except ValueError as exc:
        session.rollback()
        return RedirectResponse(f"/editor/appraisals?error={quote(str(exc))}", status_code=303)
    except Exception as exc:
        session.rollback()
        msg = f"PubMed לא זמין כרגע ({exc.__class__.__name__}). נסה שוב בעוד כמה דקות."
        return RedirectResponse(f"/editor/appraisals?error={quote(msg)}", status_code=303)
    existing = session.scalar(
        select(Appraisal)
        .where(
            Appraisal.article_id == article.id, Appraisal.status.not_in(("rejected", "superseded"))
        )
        .order_by(Appraisal.id.desc())
    )
    if existing is not None:
        return RedirectResponse(f"/editor/appraisals/{existing.id}", status_code=303)
    appraisal = service.request(session, article, _oa(request))
    if appraisal.status == "queued":
        _start(request, background, appraisal)
    return RedirectResponse(f"/editor/appraisals/{appraisal.id}", status_code=303)


# ---- one appraisal -----------------------------------------------------------------------------


@router.get("/editor/appraisals/{appraisal_id}", response_class=HTMLResponse)
def appraisal_page(
    request: Request,
    appraisal_id: int,
    error: str | None = None,
    note: str | None = None,
    session: Session = Depends(get_session),  # noqa: B008
):
    if not is_editor(request):
        return _to_login(request)
    a = _get(session, appraisal_id)
    article = session.get(Article, a.article_id)
    file = session.scalar(select(UploadedFile).where(UploadedFile.appraisal_id == a.id))
    return templates.TemplateResponse(
        request,
        "editor_appraisal.html",
        {
            "a": a,
            "article": article,
            "file": file,
            "status_he": STATUS_HE,
            "source_he": SOURCE_HE,
            "stage_he": STAGE_HE,
            "stale": _stale(a),
            "error": error,
            "note": note,
            "public_url": f"{settings.public_url}/a/{a.public_token}",
            "extraction": (a.steps or {}).get("extract", {}).get("output"),
            "skeptic": (a.steps or {}).get("skeptic", {}).get("output"),
            "editor_view": True,
        },
    )


@router.post("/editor/appraisals/{appraisal_id}/upload")
async def appraisal_upload(
    request: Request,
    appraisal_id: int,
    background: BackgroundTasks,
    pdf: UploadFile = File(...),  # noqa: B008
    session: Session = Depends(get_session),  # noqa: B008
):
    if not is_editor(request):
        return _to_login(request)
    a = _get(session, appraisal_id)
    content = await pdf.read()
    filename = pdf.filename or "upload.pdf"
    try:
        reason = service.attach_upload(session, a, content, filename, uploader="national-editor")
    except ValueError as exc:
        session.rollback()
        return RedirectResponse(
            f"/editor/appraisals/{appraisal_id}?error={quote(str(exc))}", status_code=303
        )
    _start(request, background, a)
    return RedirectResponse(
        f"/editor/appraisals/{appraisal_id}?note={quote('הקובץ התקבל: ' + reason)}",
        status_code=303,
    )


@router.post("/editor/appraisals/{appraisal_id}/action")
def appraisal_action(
    request: Request,
    appraisal_id: int,
    background: BackgroundTasks,
    action: str = Form(...),
    session: Session = Depends(get_session),  # noqa: B008
):
    if not is_editor(request):
        return _to_login(request)
    a = _get(session, appraisal_id)
    try:
        if action == "lookup":
            if service.retry_lookup(session, a, _oa(request)):
                _start(request, background, a)
            else:
                raise ValueError("גם הפעם לא נמצא טקסט מלא בגישה פתוחה")
        elif action == "abstract":
            service.use_abstract(session, a)
            _start(request, background, a)
        elif action == "rerun":
            if a.status in ("running", "queued") and not _stale(a):
                raise ValueError("הניתוח עדיין רץ")
            a.status = "queued"
            session.commit()
            _start(request, background, a)
        elif action == "new_version":
            if a.status not in ("approved", "superseded"):
                raise ValueError("גרסה חדשה נוצרת מניתוח שכבר אושר")
            article = session.get(Article, a.article_id)
            new = service.request(session, article, _oa(request))
            if new.status == "queued":
                _start(request, background, new)
            return RedirectResponse(f"/editor/appraisals/{new.id}", status_code=303)
        elif action == "approve":
            service.approve(session, a)
        elif action == "reject":
            service.reject(session, a)
        else:
            raise ValueError("פעולה לא מוכרת")
    except ValueError as exc:
        session.rollback()
        return RedirectResponse(
            f"/editor/appraisals/{appraisal_id}?error={quote(str(exc))}", status_code=303
        )
    return RedirectResponse(f"/editor/appraisals/{appraisal_id}", status_code=303)


# ---- public page -------------------------------------------------------------------------------


@router.get("/a/{token}", response_class=HTMLResponse)
def public_appraisal(
    request: Request,
    token: str,
    session: Session = Depends(get_session),  # noqa: B008
):
    a = session.scalar(select(Appraisal).where(Appraisal.public_token == token))
    if a is None or a.status not in ("approved", "superseded"):
        raise HTTPException(status_code=404)
    article = session.get(Article, a.article_id)
    newer = service.latest_approved(session, a.article_id) if a.status == "superseded" else None
    return templates.TemplateResponse(
        request,
        "appraisal_public.html",
        {
            "a": a,
            "article": article,
            "source_he": SOURCE_HE,
            "extraction": (a.steps or {}).get("extract", {}).get("output"),
            "skeptic": (a.steps or {}).get("skeptic", {}).get("output"),
            "editor_view": False,
            "newer": newer,
        },
    )
