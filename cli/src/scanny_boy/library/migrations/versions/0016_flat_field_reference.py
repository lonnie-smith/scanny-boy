"""The roll's per-roll flat-field reference.

One nullable JSON column on `rolls`: existing rows read back with NULL and
no block — there is no data migration, matching the module's stance for
every other added column.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-13

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rolls", sa.Column("flat_field", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("rolls", "flat_field")
