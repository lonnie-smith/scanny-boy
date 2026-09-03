"""Shared test fixtures.

The library database is process-global state, so every test gets its own
database file: `SCANNY_BOY_LIBRARY_DB` is pointed at a per-test path and the
engine cache is reset around the test. This mirrors the Debug-only
environment-override pattern the shipped app itself honours.

Slow tests — real RAW decoding, real stitching of the sample scans, and the
packaged-app runs — carry the `slow` marker and skip by default (the gate
itself lives in the root `conftest.py`, the only place pytest picks up
`pytest_addoption`), so an ordinary `pytest` run (an agent iterating, or CI
on a clean checkout) costs minutes, not tens of minutes. Pass `--slow` to
include them.
"""

from __future__ import annotations

import pytest

from scanny_boy.library import db as library_db


@pytest.fixture(autouse=True)
def isolated_library_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNY_BOY_LIBRARY_DB", str(tmp_path / "library.db"))
    library_db.reset_engine_cache()
    yield
    library_db.reset_engine_cache()
