"""Database models. Import every model here so Alembic can see it.

M1 adds the ingestion tables: topics, versioned queries, articles, ingest runs, and which query
found which article in which run. M2 adds screening runs and one screening result per article per
run. Later milestones add the rest of the model in docs/SPEC.md.
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
    String,
    Text,
    UniqueConstraint,
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
