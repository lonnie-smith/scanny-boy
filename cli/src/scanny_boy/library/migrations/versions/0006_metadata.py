"""Extended metadata: per-roll and per-negative descriptive fields plus the
catalog of previously-entered values the app's typeahead offers.

Nullable columns throughout: existing rows read back with NULLs and stay
valid — there is no data migration. A negative's columns are its *explicit*
per-image values; the roll's are the fallback every negative without its own
value displays and exports (docs/ARCHITECTURE.md, "extended metadata
editing"). `metadata_values` is the cross-roll catalog: one row per
(field, value) pair the user has ever committed, `last_used_at`-ordered so
the typeahead can offer most-recently-used first. Caption is deliberately
not cataloged — it is prose, not a canonical value.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-04

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_METADATA_FIELDS = ("city", "state", "camera", "lens", "caption")


def upgrade() -> None:
    for field in _METADATA_FIELDS:
        op.add_column("rolls", sa.Column(field, sa.Text(), nullable=True))
        op.add_column("negatives", sa.Column(field, sa.Text(), nullable=True))
    op.create_table(
        "metadata_values",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("last_used_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("field", "value", name="uq_metadata_values_field_value"),
    )
    op.create_index(
        "ix_metadata_values_field", "metadata_values", ["field"]
    )


def downgrade() -> None:
    op.drop_index("ix_metadata_values_field", table_name="metadata_values")
    op.drop_table("metadata_values")
    for field in _METADATA_FIELDS:
        op.drop_column("negatives", field)
        op.drop_column("rolls", field)
