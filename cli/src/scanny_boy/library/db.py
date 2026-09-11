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
import re
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine

from scanny_boy.events import Code

if TYPE_CHECKING:
    from alembic.config import Config

ENGINES: dict[str, Engine] = {}
_ENGINES_LOCK = threading.Lock()

_BUSY_TIMEOUT_MS = 30_000

# Alembic is imported only on the migration path
# (below), never at module scope — a database already at head, which is
# every command's ordinary case, must not pay for it. The fast path reads
# the version table through SQLAlchemy and compares it against a revision
# graph parsed straight from the versions directory, so "already current"
# is decided without Alembic in the process. The regexes match the
# generated shape of every file in `versions/` (`revision = "0013"`,
# `down_revision = "0012"` or `None`).
_REVISION_RE = re.compile(r'^revision = "([^"]+)"', re.MULTILINE)
_DOWN_REVISION_RE = re.compile(r'^down_revision = (?:"([^"]+)"|None)', re.MULTILINE)


class LibraryDBError(Exception):
    """Maps to `LIBRARY_DB_UNSUPPORTED`: the database sits at a migration
    revision this helper's script directory does not contain — written by
    a newer Scanny Boy, so nothing can be safely read or written. Reported
    as an ordinary `error` event rather than a crash, because every
    database-touching command reaches the engine through `open_engine` and
    the app deserves a sentence it can show, not a stream that stops after
    `started`."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = Code.LIBRARY_DB_UNSUPPORTED
        self.message = message


def library_db_path() -> Path:
    override = os.environ.get("SCANNY_BOY_LIBRARY_DB")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "ScannyBoy" / "library.db"


def _script_location() -> Path:
    """The Alembic `versions/` directory: beside this module in a checkout,
    unpacked at the bundle root in the PyInstaller build (see the `datas`
    entry in `packaging/scanny_boy.spec`)."""
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


def _revision_graph() -> tuple[set[str], str | None]:
    """The revision ids in the packaged `versions/` directory and the
    single head among them, parsed from the files' `revision` and
    `down_revision` assignments.

    Returns `(known, head)`; `head` is None when the directory does not
    parse into exactly one head (a branch or a malformed file) — the
    caller then falls back to Alembic, which owns the real graph."""
    versions_dir = _script_location() / "versions"
    known: set[str] = set()
    down_of: set[str] = set()
    for path in sorted(versions_dir.glob("*.py")):
        source = path.read_text()
        revision = _REVISION_RE.search(source)
        down_revision = _DOWN_REVISION_RE.search(source)
        if revision is None or down_revision is None:
            return set(), None
        known.add(revision.group(1))
        if down_revision.group(1) is not None:
            down_of.add(down_revision.group(1))
    heads = known - down_of
    if len(heads) == 1 and len(known) == len(down_of) + 1:
        return known, heads.pop()
    return known, None


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
    """Bring the database to the migrations' head revision.

    The fast path — a database already at head, which is every ordinary
    command's case — needs only one cheap SELECT plus the parsed revision
    graph and returns without importing Alembic at all. Anything else
    (a fresh database, a stale one catching up, an unknown revision)
    takes the Alembic path."""
    known, head = _revision_graph()
    current = _recorded_revision(engine)
    if current is not None and known and current not in known:
        raise LibraryDBError(
            f"the library database at {path} is at migration revision "
            f"{current}, which this helper does not know (it knows up to "
            f"{head}); it was written by a newer Scanny Boy. Update this "
            "helper, or point SCANNY_BOY_LIBRARY_DB at a fresh database."
        ) from None
    if current is not None and head is not None and current == head:
        return
    # No version table, no recorded revision, a revision this pass could
    # not place, or a stale one: Alembic owns the upgrade from here.
    _run_alembic_upgrade(engine, path)


def _recorded_revision(engine: Engine) -> str | None:
    """The database's recorded Alembic revision, or None when there is no
    version table yet (a fresh database). One SELECT through the caller's
    engine, so the connect pragmas apply."""
    with engine.connect() as connection:
        if not inspect(connection).has_table("alembic_version"):
            return None
        return connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()


def _run_alembic_upgrade(engine: Engine, path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(_script_location()))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{path}")
    # Reuse the caller's engine rather than letting Alembic open its own,
    # so the pragmas above apply to migration connections too.
    config.attributes["connection"] = engine
    _refuse_unknown_revision(engine, config, path)
    command.upgrade(config, "head")


def _refuse_unknown_revision(engine: Engine, config: Config, path: Path) -> None:
    """A database ahead of this helper's migrations cannot be upgraded
    down; `command.upgrade` would raise Alembic's own `ResolutionError`
    and kill the process after nothing but `started` reached stdout. The
    revision table read here is one cheap SELECT on a connection that is
    about to run a migration anyway."""
    from alembic.script import ScriptDirectory
    from alembic.util.exc import CommandError

    script = ScriptDirectory.from_config(config)
    with engine.connect() as connection:
        if not inspect(connection).has_table("alembic_version"):
            return
        current = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    if current is None:
        return
    try:
        script.get_revision(current)
    except CommandError:
        head = script.get_current_head()
        raise LibraryDBError(
            f"the library database at {path} is at migration revision "
            f"{current}, which this helper does not know (it knows up to "
            f"{head}); it was written by a newer Scanny Boy. Update this "
            "helper, or point SCANNY_BOY_LIBRARY_DB at a fresh database."
        ) from None
