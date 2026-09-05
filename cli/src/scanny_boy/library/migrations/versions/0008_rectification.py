"""The per-negative rectification record (docs/RECTIFICATION_PLAN.md
section 7).

Nullable column: existing rows read back with NULLs and stay valid — there
is no data migration. `negatives.rectification` records the fitted rig-tilt
correction the stitch stage applied, beside the per-negative
`normalization` record it mirrors.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-05

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("negatives", sa.Column("rectification", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("negatives", "rectification")
