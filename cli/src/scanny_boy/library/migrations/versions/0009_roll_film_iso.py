"""Roll-only film stock and ISO rating metadata.

Nullable columns on `rolls` only: existing rows read back with NULLs and
stay valid — there is no data migration. These fields are not stored on
negatives and are not subject to live-fallback override.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-05

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

_ROLL_ONLY_METADATA_FIELDS = ("film", "iso")


def upgrade() -> None:
    for field in _ROLL_ONLY_METADATA_FIELDS:
        op.add_column("rolls", sa.Column(field, sa.Text(), nullable=True))


def downgrade() -> None:
    for field in _ROLL_ONLY_METADATA_FIELDS:
        op.drop_column("rolls", field)
