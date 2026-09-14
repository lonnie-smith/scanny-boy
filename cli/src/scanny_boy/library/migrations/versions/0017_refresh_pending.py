"""Deferred highlight-lock refresh for tethered capture (docs/TETHER_PLAN.md §4.4).

One nullable column on ``rolls``: existing rows read back as NULL and are
treated as not pending — no data migration, and no bump to the roll
invariants or ``ROLL_MANIFEST_FORMAT_VERSION``.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-11

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rolls", sa.Column("refresh_pending", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("rolls", "refresh_pending")
