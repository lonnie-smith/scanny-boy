"""The per-negative auto-crop evidence block.

One nullable JSON column on `negatives`: existing rows read back with NULL
and no block — there is no data migration, matching the module's stance for
every other added column.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-20

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("negatives", sa.Column("auto_crop", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("negatives", "auto_crop")
