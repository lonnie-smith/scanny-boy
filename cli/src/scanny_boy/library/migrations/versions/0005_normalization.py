"""The normalization record and the second ICC profile
(docs/DECISIONS.md, "Normalization decisions").

Nullable columns throughout: existing rows read back with NULLs and stay
valid — there is no data migration. `rolls.published_icc_profile` is
section 3.12's split of the single-profile invariant into the intermediates'
(linear, already in `rolls.icc_profile`) and the published TIFFs' (density).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-02

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # NOT NULL with an empty-object server default: pre-normalization rolls
    # read back with `{}`, which `check_roll_invariants` treats as the
    # pre-split state (its sha256 compares equal only once seeded, exactly
    # like the other first-run-established invariants).
    op.add_column(
        "rolls",
        sa.Column(
            "published_icc_profile", sa.Text(), nullable=False, server_default="{}"
        ),
    )
    op.add_column(
        "runs", sa.Column("normalization_aggregate", sa.Text(), nullable=True)
    )
    op.add_column("sources", sa.Column("scan_clip_fractions", sa.Text(), nullable=True))
    op.add_column("negatives", sa.Column("normalization", sa.Text(), nullable=True))
    op.add_column("negatives", sa.Column("normalized_fill", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("negatives", "normalized_fill")
    op.drop_column("negatives", "normalization")
    op.drop_column("sources", "scan_clip_fractions")
    op.drop_column("runs", "normalization_aggregate")
    op.drop_column("rolls", "published_icc_profile")
