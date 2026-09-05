"""2D grid stitching: per-negative grid fields on the negatives table.

All four are nullable: rows written by pre-grid builds read back with
NULLs and stay valid — there is no data migration. `grid` is the declared
`{"across": A, "down": D}`; `grid_cells` is the solved assignment (member
name -> [row, col]); `grid_pitch_ratio`/`grid_alignment_ratio` are the
regularity measures of docs/GRID_STITCH_PLAN.md section 4.2, null when
unmeasurable.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-04

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("negatives", sa.Column("grid", sa.Text(), nullable=True))
    op.add_column("negatives", sa.Column("grid_cells", sa.Text(), nullable=True))
    op.add_column(
        "negatives", sa.Column("grid_pitch_ratio", sa.Float(), nullable=True)
    )
    op.add_column(
        "negatives", sa.Column("grid_alignment_ratio", sa.Float(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("negatives", "grid_alignment_ratio")
    op.drop_column("negatives", "grid_pitch_ratio")
    op.drop_column("negatives", "grid_cells")
    op.drop_column("negatives", "grid")
