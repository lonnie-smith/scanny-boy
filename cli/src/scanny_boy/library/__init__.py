"""The library database: a single SQLite store (SQLAlchemy + Alembic) that
replaces `scanny-boy-roll.json` as the durable record of every roll.

The database lives at `~/Library/Application Support/ScannyBoy/library.db`
(overridable for tests and debug builds through `SCANNY_BOY_LIBRARY_DB`).
The domain objects it persists are still `roll_manifest.py`'s dataclasses —
this package is a persistence layer, not a second domain model — so the
pipelines keep reading and writing `RollManifest` exactly as before, and the
`roll info` event payload is unchanged.
"""
