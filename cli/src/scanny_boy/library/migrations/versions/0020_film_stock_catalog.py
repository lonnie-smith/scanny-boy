"""Film kinds on the `metadata_values` catalog, and the seeded film stocks.

One nullable text column, `metadata_values.film_kind` (`colour` |
`monochrome`; NULL = unknown), meaningful only for rows of field `film`.
The migration then seeds `film` rows for ten common stocks so the app's
typeahead offers them on a fresh library, each carrying its kind, which is
what lets the app auto-set the roll's film kind when a stock is picked.

Seeding rules: a stock the user already typed keeps its row and its
`last_used_at` (only a NULL `film_kind` is filled in); every other stock is
inserted with a fixed 1970 `last_used_at`, so seeds always sort below any
real use under the catalog's `last_used_at desc` ordering. The timestamps
strictly decrease down the list (same `YYYY-MM-DDTHH:MM:SS[.ffffff]Z` shape
`_now_iso()` writes), so the seeds keep their listed order among
themselves regardless of row ids.

The seed list lives here rather than in app code: a migration is a frozen
snapshot and must not change when the app's own list does.

Downgrade drops the column and leaves the rows: a seeded stock the user has
since picked is indistinguishable from a typed one, so deleting by value
could erase real history. The leftover rows are ordinary `film` values to
the older code.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-20

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

# (stock, film kind), in the order the typeahead lists them when no stock
# has been used yet.
FILM_STOCK_SEEDS: tuple[tuple[str, str], ...] = (
    ("Kodak Ultramax 400", "colour"),
    ("Kodak Gold 200", "colour"),
    ("Kodak Portra 160", "colour"),
    ("Kodak Portra 400", "colour"),
    ("Kodak Tri-X 400", "monochrome"),
    ("Kodak TMax 400", "monochrome"),
    ("Kodak TMax P3200", "monochrome"),
    ("Ilford HP5+", "monochrome"),
    ("Ilford FP4+", "monochrome"),
    ("Ilford Delta 3200", "monochrome"),
)


def _seed_last_used_at(index: int) -> str:
    # Strictly decreasing per index: the first seed is the most recent.
    return f"1970-01-01T00:00:{len(FILM_STOCK_SEEDS) - index:02d}Z"


def upgrade() -> None:
    op.add_column("metadata_values", sa.Column("film_kind", sa.Text(), nullable=True))

    bind = op.get_bind()
    for index, (value, kind) in enumerate(FILM_STOCK_SEEDS):
        existing = bind.execute(
            sa.text(
                "SELECT id FROM metadata_values WHERE field = 'film' AND value = :value"
            ),
            {"value": value},
        ).first()
        if existing is None:
            bind.execute(
                sa.text(
                    "INSERT INTO metadata_values "
                    "(field, value, last_used_at, film_kind) "
                    "VALUES ('film', :value, :last_used_at, :kind)"
                ),
                {
                    "value": value,
                    "last_used_at": _seed_last_used_at(index),
                    "kind": kind,
                },
            )
        else:
            bind.execute(
                sa.text(
                    "UPDATE metadata_values SET film_kind = :kind "
                    "WHERE id = :id AND film_kind IS NULL"
                ),
                {"kind": kind, "id": existing[0]},
            )


def downgrade() -> None:
    op.drop_column("metadata_values", "film_kind")
