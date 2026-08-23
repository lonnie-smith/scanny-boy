"""The initial library schema.

Revision ID: 0001
Revises:
Create Date: 2026-08-31

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rolls",
        sa.Column("roll_id", sa.Text(), primary_key=True),
        sa.Column("folder_path", sa.Text(), nullable=False, unique=True),
        sa.Column("roll_name", sa.Text(), nullable=False),
        sa.Column("shots_per_negative", sa.Integer(), nullable=False),
        sa.Column("scanny_boy_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.Column("processing_params", sa.Text(), nullable=False),
        sa.Column("icc_profile", sa.Text(), nullable=False),
        sa.Column("stitch_params", sa.Text(), nullable=False),
        sa.Column("roll_capture_date", sa.Text(), nullable=True),
        sa.Column("last_applied_at", sa.Text(), nullable=True),
    )
    op.create_index("ix_rolls_folder_path", "rolls", ["folder_path"])

    op.create_table(
        "runs",
        sa.Column("run_id", sa.Text(), primary_key=True),
        sa.Column(
            "roll_id",
            sa.Text(),
            sa.ForeignKey("rolls.roll_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("short_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("convert_run_id", sa.Text(), nullable=True),
        sa.Column("input_folder", sa.Text(), nullable=True),
        sa.Column("source_order", sa.Text(), nullable=False),
        sa.Column("work_dir", sa.Text(), nullable=True),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("finished_at", sa.Text(), nullable=True),
    )
    op.create_index("ix_runs_roll_id", "runs", ["roll_id"])

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "roll_id",
            sa.Text(),
            sa.ForeignKey("rolls.roll_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("absolute_path", sa.Text(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("mtime", sa.Float(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
    )
    op.create_index("ix_sources_roll_id", "sources", ["roll_id"])

    op.create_table(
        "negatives",
        sa.Column("negative_id", sa.Text(), primary_key=True),
        sa.Column(
            "roll_id",
            sa.Text(),
            sa.ForeignKey("rolls.roll_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=True),
        sa.Column("members", sa.Text(), nullable=False),
        sa.Column("expected_output", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("frames", sa.Text(), nullable=False),
        sa.Column("pairs", sa.Text(), nullable=False),
        sa.Column("global_rms_px", sa.Float(), nullable=True),
        sa.Column("canvas", sa.Text(), nullable=True),
        sa.Column("valid_rect", sa.Text(), nullable=True),
        sa.Column("fill_color", sa.Text(), nullable=False),
        sa.Column("rebate_deviation_px", sa.Float(), nullable=True),
        sa.Column("used_clahe_fallback", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("capture_time", sa.Text(), nullable=False),
        sa.Column("preview_path", sa.Text(), nullable=True),
    )
    op.create_index("ix_negatives_roll_id", "negatives", ["roll_id"])

    op.create_table(
        "edits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "negative_id",
            sa.Text(),
            sa.ForeignKey("negatives.negative_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column("params", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("negative_id", "position"),
    )
    op.create_index("ix_edits_negative_id", "edits", ["negative_id"])


def downgrade() -> None:
    op.drop_table("edits")
    op.drop_table("negatives")
    op.drop_table("sources")
    op.drop_table("runs")
    op.drop_table("rolls")
