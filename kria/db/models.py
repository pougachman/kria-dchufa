"""Database models. Import every model here so Alembic can see it.

M1 adds the ingestion tables: topics, versioned queries, articles, ingest runs, and which query
found which article in which run. Later milestones add the rest of the model in docs/SPEC.md.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
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
