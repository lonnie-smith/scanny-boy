"""The roll's film-base reference.

One nullable JSON column on `rolls`: existing rows read back with NULL and
no block — there is no data migration, matching the module's stance for
every other added column.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-06

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rolls", sa.Column("film_base", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("rolls", "film_base")
