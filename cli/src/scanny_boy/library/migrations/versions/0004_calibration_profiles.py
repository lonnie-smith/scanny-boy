"""Geometric calibration columns on the flat-field profile record.

A profile becomes the complete optical description of one rig
configuration: the four nullable columns here carry the ChArUco board the
calibration was fitted with, the radial distortion fit, the chromatic
aberration fit, and the human-readable calibration report
(docs/GEOMETRIC_PLAN.md sections 3.1-3.5). Existing rows read back with
four NULLs and stay valid — there is no data migration.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-02

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_COLUMNS = (
    "board_key",
    "geometry",
    "chromatic_aberration",
    "calibration_report",
)


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column("flatfield_profiles", sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("flatfield_profiles", name)
