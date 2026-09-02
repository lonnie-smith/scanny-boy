"""The flat-field profile table.

Profiles' gain maps live as `.npz` files beside the library database; the
row here is the metadata record, read back through the CLI because Swift is
forbidden from reading the library's storage directly.

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
    op.create_table(
        "flatfield_profiles",
        sa.Column("profile_id", sa.Text(), primary_key=True),
        # The dropdown must be unambiguous, so the name is unique too.
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("gain_map_path", sa.Text(), nullable=False),
        sa.Column("gain_map_sha256", sa.Text(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("reference_width", sa.Integer(), nullable=False),
        sa.Column("reference_height", sa.Integer(), nullable=False),
        sa.Column("params", sa.Text(), nullable=False),
        sa.Column("scanny_boy_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("flatfield_profiles")