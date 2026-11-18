"""Frame log import: roll link, frame link and per-negative place columns.

docs/METADATA_IMPORT_PLAN.md section 4.1. Adds `rolls.framelog_roll_uid` and
`rolls.framelog_roll_number`, and on `negatives` the `framelog_frame_uid`,
`latitude`, `longitude`, `gps_accuracy_m`, `location_name` and `address`
columns. All are nullable, so existing rows read back unchanged. The two new
capture-time keys live inside the existing `capture_time` JSON column and
need no migration: rows without them read back as `None`.

Downgrade drops the columns (plain DROP COLUMN, for the reason given in
0021).

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-18

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

ROLL_COLUMNS = (
    ("framelog_roll_uid", sa.Text),
    ("framelog_roll_number", sa.Integer),
)
NEGATIVE_COLUMNS = (
    ("framelog_frame_uid", sa.Text),
    ("latitude", sa.Float),
    ("longitude", sa.Float),
    ("gps_accuracy_m", sa.Float),
    ("location_name", sa.Text),
    ("address", sa.Text),
)


def upgrade() -> None:
    for name, kind in ROLL_COLUMNS:
        op.add_column("rolls", sa.Column(name, kind(), nullable=True))
    for name, kind in NEGATIVE_COLUMNS:
        op.add_column("negatives", sa.Column(name, kind(), nullable=True))


def downgrade() -> None:
    for name, _ in reversed(NEGATIVE_COLUMNS):
        op.drop_column("negatives", name)
    for name, _ in reversed(ROLL_COLUMNS):
        op.drop_column("rolls", name)
