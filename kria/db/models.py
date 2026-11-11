"""Database models.

Empty in M0. M1 adds Topic, QueryVersion and Article; later milestones add the rest of the
data model described in docs/SPEC.md. Import every model here so Alembic can see it.
"""

from kria.db.base import Base  # noqa: F401
