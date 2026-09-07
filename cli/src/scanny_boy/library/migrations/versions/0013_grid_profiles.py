"""The grid configuration preset table.

Each row is a user-labelled `across` x `down` shape for the Add Scans
grouping picker — metadata only, read back through the CLI because Swift is
forbidden from reading the library's storage directly.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-06

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "grid_profiles",
        sa.Column("profile_id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("across", sa.Integer(), nullable=False),
        sa.Column("down", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("grid_profiles")
