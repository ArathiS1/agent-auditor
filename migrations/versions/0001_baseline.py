"""Baseline: the schema as of release 0.9.0 (phases 1-8).

This revision creates every table from the application's models. That is the right baseline for a database
that has never had migrations, but it means this file always reflects the *current* models, not a frozen
copy. So, before you change a model for the first time:

    1. Replace the body of upgrade() below with explicit DDL: create an empty database, point
       AUDITOR_DATABASE_URL at it, run `alembic revision --autogenerate -m "explicit baseline"`
       with the OLD models, and paste the generated operations here.
    2. Then make your model change and generate a new revision on top.

An existing database that was created by the app's automatic create_all (the SQLite default) already has these
tables: run `alembic stamp head` once instead of upgrading.

Revision ID: 0001
Revises:
"""
from alembic import op

from agent_auditor import models  # noqa: F401
from agent_auditor.db import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
