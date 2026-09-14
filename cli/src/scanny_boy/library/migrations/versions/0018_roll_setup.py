"""Convenience defaults for the roll's next capture/stitch run — grid,
interval, and film format.

One nullable JSON column on `rolls`: existing rows read back with NULL and
no block — there is no data migration, matching the module's stance for
every other added column.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-13

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rolls", sa.Column("setup", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("rolls", "setup")
