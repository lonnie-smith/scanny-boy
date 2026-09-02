"""Drop the roll-level shots_per_negative column.

Each stitch batch's grouping now lives only in its own work manifest, so a
roll can hold negatives stitched from different scan counts; the value is
discarded rather than preserved.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-01

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("rolls") as batch:
        batch.drop_column("shots_per_negative")


def downgrade() -> None:
    with op.batch_alter_table("rolls") as batch:
        batch.add_column(
            sa.Column(
                "shots_per_negative", sa.Integer(), nullable=False, server_default="3"
            )
        )
