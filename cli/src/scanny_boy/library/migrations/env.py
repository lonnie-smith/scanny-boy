"""Alembic environment for the library database.

Run programmatically by `scanny_boy.library.db` — there is no ini file; the
script location and URL are set on the `Config` object before
`alembic.command.upgrade` is called. When the caller passes an engine via
`config.attributes["connection"]`, migrations run on that engine so the
connection pragmas (foreign keys, WAL) apply here too.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import pool

config = context.config

# Revisions are hand-written against `models.py`; no autogenerate.
target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = config.attributes.get("connection")
    if engine is not None:
        # Reuse the caller's engine so its pragmas (foreign keys, WAL)
        # apply to migration connections too.
        with engine.connect() as connection:
            context.configure(connection=connection, render_as_batch=True)
            with context.begin_transaction():
                context.run_migrations()
        return

    # No engine supplied (e.g. `alembic` invoked from the command line for
    # development): build one from the configured URL.
    import sqlalchemy

    engine = sqlalchemy.create_engine(
        config.get_main_option("sqlalchemy.url"), poolclass=pool.NullPool
    )
    with engine.connect() as connection:
        context.configure(connection=connection, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
