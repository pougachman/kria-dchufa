"""Database engine, session factory and the declarative base for all models."""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from kria.config import settings


def normalize_db_url(url: str) -> str:
    """Railway gives postgres:// or postgresql:// URLs.

    SQLAlchemy needs the psycopg 3 driver name in the URL.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


class Base(DeclarativeBase):
    pass


engine = create_engine(normalize_db_url(settings.database_url), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
