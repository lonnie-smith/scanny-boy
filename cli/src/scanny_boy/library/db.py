"""Engine construction and schema migrations for the library database.

The database is one SQLite file at
`~/Library/Application Support/ScannyBoy/library.db`. `SCANNY_BOY_LIBRARY_DB`
overrides the location — the same Debug-only escape hatch pattern as
`SCANNY_BOY_CLI`, and how the test suite points every command at a
per-test database.

Migrations are Alembic revisions under `library/migrations/`, applied
programmatically on every engine open (`upgrade head` is a no-op once
current, so the per-invocation cost is one version-table read — `probe` is
launched constantly and must stay cheap).
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

ENGINES: dict[str, Engine] = {}
_ENGINES_LOCK = threading.Lock()

_BUSY_TIMEOUT_MS = 30_000


def library_db_path() -> Path:
    override = os.environ.get("SCANNY_BOY_LIBRARY_DB")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "ScannyBoy" / "library.db"


def _script_location() -> Path:
    """The Alembic `versions/` directory: beside this module in a checkout,
    unpacked at the bundle root in the PyInstaller build (see the `datas`
    entry in `build/scanny_boy.spec`)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "migrations"
    return Path(__file__).resolve().parent / "migrations"


def _set_sqlite_pragmas(dbapi_connection: object, _record: object) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    cursor.close()


def reset_engine_cache() -> None:
    """Dispose every cached engine. Test-only: `SCANNY_BOY_LIBRARY_DB`
    changes between tests, and a cached engine would hold the previous
    test's file open."""
    with _ENGINES_LOCK:
        for engine in ENGINES.values():
            engine.dispose()
        ENGINES.clear()


def open_engine() -> Engine:
    """The shared engine for the library database, migrated to head."""
    path = library_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path)
    with _ENGINES_LOCK:
        engine = ENGINES.get(key)
        if engine is None:
            engine = create_engine(
                f"sqlite:///{key}",
                connect_args={"check_same_thread": False, "timeout": _BUSY_TIMEOUT_MS / 1000},
            )
            event.listen(engine, "connect", _set_sqlite_pragmas)
            ENGINES[key] = engine
    _upgrade_to_head(engine, path)
    return engine


def _upgrade_to_head(engine: Engine, path: Path) -> None:
    config = Config()
    config.set_main_option("script_location", str(_script_location()))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    # Reuse the caller's engine rather than letting Alembic open its own,
    # so the pragmas above apply to migration connections too.
    config.attributes["connection"] = engine
    command.upgrade(config, "head")
