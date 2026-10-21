"""Make `camera` roll-only: drop the per-negative `negatives.camera` column.

A roll is shot on one body, so a per-negative camera override has no use.
Before the column goes, any camera a user typed onto negatives is not
thrown away: for every roll whose own `camera` is NULL (or empty) and
whose negatives carry non-empty camera values, the most common negative
value becomes the roll's camera. Ties are broken alphabetically, so the
result is deterministic. A roll that already has a camera keeps it; the
negatives' values are discarded.

Downgrade re-adds the nullable column, empty: the promoted roll values stay
on the rolls, which every older reader treats as the fallback.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-21

"""

from __future__ import annotations

import collections

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    rolls = bind.execute(
        sa.text(
            "SELECT roll_id FROM rolls WHERE camera IS NULL OR TRIM(camera) = '' "
            "ORDER BY roll_id"
        )
    ).fetchall()
    for (roll_id,) in rolls:
        counts = collections.Counter(
            value.strip()
            for (value,) in bind.execute(
                sa.text(
                    "SELECT camera FROM negatives "
                    "WHERE roll_id = :roll_id AND camera IS NOT NULL"
                ),
                {"roll_id": roll_id},
            )
            if value.strip()
        )
        if not counts:
            continue
        # Most common first; alphabetical among equals.
        promoted = min(counts, key=lambda value: (-counts[value], value))
        bind.execute(
            sa.text("UPDATE rolls SET camera = :camera WHERE roll_id = :roll_id"),
            {"camera": promoted, "roll_id": roll_id},
        )

    # A plain DROP COLUMN, not `batch_alter_table`: the batch route rebuilds
    # the table by dropping the old one, and with foreign keys on that
    # would cascade-delete the `edits` rows that reference `negatives`.
    op.drop_column("negatives", "camera")


def downgrade() -> None:
    op.add_column("negatives", sa.Column("camera", sa.Text(), nullable=True))
