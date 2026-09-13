"""Rename flatfield_profiles to rig_profiles and drop gain-map columns.

The scanning-rig profile is calibration-only; gain maps now live on each
roll's `flat_field` block (migration 0016). Existing rows keep their
geometry and chromatic-aberration fits; the dropped columns are discarded.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-13

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_GAIN_MAP_COLUMNS = (
    "gain_map_path",
    "gain_map_sha256",
    "source_path",
    "reference_width",
    "reference_height",
    "params",
)


def upgrade() -> None:
    with op.batch_alter_table("flatfield_profiles") as batch:
        for name in _GAIN_MAP_COLUMNS:
            batch.drop_column(name)
    op.rename_table("flatfield_profiles", "rig_profiles")


def downgrade() -> None:
    op.rename_table("rig_profiles", "flatfield_profiles")
    with op.batch_alter_table("flatfield_profiles") as batch:
        batch.add_column(sa.Column("gain_map_path", sa.Text(), nullable=True))
        batch.add_column(sa.Column("gain_map_sha256", sa.Text(), nullable=True))
        batch.add_column(sa.Column("source_path", sa.Text(), nullable=True))
        batch.add_column(sa.Column("reference_width", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("reference_height", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("params", sa.Text(), nullable=True))
