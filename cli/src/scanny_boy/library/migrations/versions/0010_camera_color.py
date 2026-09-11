"""The roll manifest's `camera_color` block.

A nullable JSON column on `rolls` only: existing rows read back with NULL
and no block — there is no data migration, matching the module's stance
for every other added column. The block is recorded data for the export
path, not a roll invariant.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-05

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rolls", sa.Column("camera_color", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("rolls", "camera_color")
