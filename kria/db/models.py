"""Database models. Import every model here so Alembic can see it.

M1 adds the ingestion tables: topics, versioned queries, articles, ingest runs, and which query
found which article in which run. M2 adds screening runs and one screening result per article per
run. M3 adds the national editor's decisions, a log of editor actions (to measure editor time),
and sent digests. M4 adds appraisals and the privately uploaded full-text files they use.
Later milestones add the rest of the model in docs/SPEC.md.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kria.db.base import Base

JsonType = JSON().with_variant(JSONB(), "postgresql")


class Topic(Base):
    """A base taxonomy topic, synced from data/taxonomy.yaml."""

    __tablename__ = "topics"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name_he: Mapped[str] = mapped_column(String(128))
    group: Mapped[str] = mapped_column(String(32))
    suggested: Mapped[bool] = mapped_column(default=False)
    active: Mapped[bool] = mapped_column(default=True)


class QueryVersion(Base):
    """One version of a PubMed query for a source (a topic or a shared journal pool).

    A new version is created whenever the query text changes, so every article can be traced to
    the exact query that found it.
    """

    __tablename__ = "query_versions"
    __table_args__ = (
        UniqueConstraint("source_key", "version"),
        UniqueConstraint("source_key", "sha256"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    query: Mapped[str] = mapped_column(Text)
    journals: Mapped[list] = mapped_column(JsonType)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IngestRun(Base):
    __tablename__ = "ingest_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), index=True)  # "2026-10"
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")
    filter_version: Mapped[int] = mapped_column(Integer)
    report: Mapped[dict | None] = mapped_column(JsonType)
    error: Mapped[str | None] = mapped_column(Text)


class Article(Base):
    """A PubMed record. Every article has a PMID; the model never creates one."""

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    pmid: Mapped[str] = mapped_column(String(16), unique=True)
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    pmcid: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    abstract: Mapped[str | None] = mapped_column(Text)
    journal: Mapped[str] = mapped_column(String(512))
    journal_ta: Mapped[str] = mapped_column(String(255), index=True)
    nlm_id: Mapped[str | None] = mapped_column(String(32))
    pub_types: Mapped[list] = mapped_column(JsonType)
    pub_date: Mapped[str | None] = mapped_column(String(64))
    entrez_date: Mapped[dt.date | None] = mapped_column(Date)
    # Latest electronic or print publication date, partial dates rounded up.
    latest_pub_date: Mapped[dt.date | None] = mapped_column(Date)
    languages: Mapped[list] = mapped_column(JsonType)
    authors: Mapped[list] = mapped_column(JsonType)
    mesh: Mapped[list] = mapped_column(JsonType)
    keywords: Mapped[list] = mapped_column(JsonType)
    flags: Mapped[list] = mapped_column(JsonType)
    # Why the code filter left this article out of screening; None = goes on to screening.
    excluded_reason: Mapped[str | None] = mapped_column(String(64), index=True)
    duplicate_of_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"))
    first_seen_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingest_runs.id"))
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RunHit(Base):
    """Which query version found which article in which run."""

    __tablename__ = "run_hits"

    run_id: Mapped[int] = mapped_column(ForeignKey("ingest_runs.id"), primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    query_version_id: Mapped[int] = mapped_column(ForeignKey("query_versions.id"), primary_key=True)


class ScreeningRun(Base):
    """One screening pass over a month's articles: one Message Batch, one model, one prompt.

    Records what was sent (model, prompt version and hash, settings), when (submitted, ended at
    the API, collected), and what it cost (tokens and USD).
    """

    __tablename__ = "screening_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), index=True)
    ingest_run_id: Mapped[int] = mapped_column(ForeignKey("ingest_runs.id"))
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(64))
    prompt_sha256: Mapped[str] = mapped_column(String(64))
    settings: Mapped[dict] = mapped_column(JsonType)
    batch_id: Mapped[str | None] = mapped_column(String(128))
    # submitting -> submitted -> ok, or failed at any step (error says why)
    status: Mapped[str] = mapped_column(String(16), default="submitted", index=True)
    submitted_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    requested: Mapped[int] = mapped_column(Integer, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    report: Mapped[dict | None] = mapped_column(JsonType)
    error: Mapped[str | None] = mapped_column(Text)


class Screening(Base):
    """The model's screening of one article in one run, stored with model and prompt version.

    status "ok" means the output passed validation; anything else (invalid, errored, refused,
    max_tokens, expired, canceled) keeps the raw output or error so it can be checked, and the
    article is retried in the next run.
    """

    __tablename__ = "screenings"
    __table_args__ = (UniqueConstraint("run_id", "article_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("screening_runs.id"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    relevant: Mapped[bool | None] = mapped_column(Boolean)
    topics: Mapped[list] = mapped_column(JsonType, default=list)
    design: Mapped[str | None] = mapped_column(String(64))
    practice_changing: Mapped[int | None] = mapped_column(Integer)
    practice_changing_reason: Mapped[str | None] = mapped_column(Text)
    board_exam: Mapped[int | None] = mapped_column(Integer)
    board_exam_reason: Mapped[str | None] = mapped_column(Text)
    journal_club: Mapped[int | None] = mapped_column(Integer)
    journal_club_reason: Mapped[str | None] = mapped_column(Text)
    # From screening.v2: connection to ED work, 1-5 (ranking key after the high-yield score).
    ed_relevance: Mapped[int | None] = mapped_column(Integer)
    ed_relevance_reason: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    raw_output: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @property
    def high_yield_score(self) -> int | None:
        """High yield = the highest of the three dimensions (docs/SPEC.md)."""
        scores = [self.practice_changing, self.board_exam, self.journal_club]
        return max(scores) if all(s is not None for s in scores) else None


class EditorDecision(Base):
    """The national editor's approve/reject decision on one article for one month's digest."""

    __tablename__ = "editor_decisions"
    __table_args__ = (UniqueConstraint("month", "article_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"))
    screening_id: Mapped[int | None] = mapped_column(ForeignKey("screenings.id"))
    decision: Mapped[str] = mapped_column(String(16))  # approved | rejected
    rank: Mapped[int | None] = mapped_column(Integer)  # rank shown when decided
    # One of the (up to 3) articles the editor puts at the top of the digest.
    featured: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    decided_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class EditorEvent(Base):
    """One editor action (page view, decision, digest sent). Used to measure editor minutes."""

    __tablename__ = "editor_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"))
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class Digest(Base):
    """One sending of a month's approved digest: exactly what was sent, to whom, and when."""

    __tablename__ = "digests"

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), index=True)
    article_ids: Mapped[list] = mapped_column(JsonType)
    subject: Mapped[str] = mapped_column(String(255))
    html: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    recipients: Mapped[list] = mapped_column(JsonType)
    provider_id: Mapped[str | None] = mapped_column(String(128))
    editor_minutes: Mapped[float | None] = mapped_column(Float)
    sent_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))


class Appraisal(Base):
    """An SGEM-style critical appraisal of one article, requested by the editor.

    Every model step's output is stored in `steps` with the model that served it and the prompt
    version. Statistics are computed in kria/appraisal/calc.py and stored in `stats`. Nothing is
    public until the editor approves it; then it is served at /a/<public_token>.
    """

    __tablename__ = "appraisals"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    # needs_pdf -> queued -> running -> review -> approved | rejected; failed at any step
    status: Mapped[str] = mapped_column(String(16), index=True)
    stage: Mapped[str | None] = mapped_column(String(32))  # current step while running
    # pmc_xml | oa_pdf | upload | abstract (abstract-only appraisals are labelled limited)
    source: Mapped[str | None] = mapped_column(String(16))
    source_url: Mapped[str | None] = mapped_column(Text)
    limited: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    lookup: Mapped[dict | None] = mapped_column(JsonType)  # what the open-access lookup found
    steps: Mapped[dict] = mapped_column(JsonType, default=dict)
    stats: Mapped[list | None] = mapped_column(JsonType)
    checks: Mapped[dict | None] = mapped_column(JsonType)  # anchor and number checks
    report: Mapped[dict | None] = mapped_column(JsonType)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text)
    public_token: Mapped[str] = mapped_column(String(64), unique=True)
    requested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class UploadedFile(Base):
    """A full-text PDF the requesting editor downloaded with their own access and uploaded.

    Visible only to its uploader (never served back), used for one appraisal, deleted when the
    appraisal is approved, and in any case after 30 days (the daily job purges expired files).
    """

    __tablename__ = "uploaded_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    appraisal_id: Mapped[int] = mapped_column(ForeignKey("appraisals.id"), index=True)
    uploader: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    content: Mapped[bytes] = mapped_column(LargeBinary)
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    delete_after: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
