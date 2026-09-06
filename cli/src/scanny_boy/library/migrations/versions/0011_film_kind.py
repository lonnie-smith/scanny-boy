"""The roll's frozen film-kind decision (MONOCHROME_PLAN section 2.3).

One nullable JSON column on `rolls`, distinct from the existing `film` text
column (the free-text film-stock metadata): existing rows read back with
NULL and stay valid — there is no data migration. A NULL `film_kind` reads
as "no decision frozen yet", exactly as section 5.2 requires for a roll
that predates the feature.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-05

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rolls", sa.Column("film_kind", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("rolls", "film_kind")
