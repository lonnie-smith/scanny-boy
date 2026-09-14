"""The roll's highlight-colour lock (docs/ROLL_HIGHLIGHT_LOCK.md §1).

One nullable JSON column on `rolls`, additive like `film_base` and
`camera_color`: existing rows read back with NULL and no block — there is
no data migration, and no bump to the roll invariants or
`ROLL_MANIFEST_FORMAT_VERSION`, so an existing roll keeps taking new
negatives without a re-stitch.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-11

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rolls", sa.Column("highlight_lock", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("rolls", "highlight_lock")
