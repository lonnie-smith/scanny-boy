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

`work_dir` is the other cost control. Building a Phase 1 work directory means
three synthetic scenes, encoded, written out as three real TIFFs — about 2.1
seconds — and the great majority of the callers wanted the very same default
tree. Building it once per session and handing out `shutil.copytree` copies
costs about 3 milliseconds a test instead.
"""

from __future__ import annotations

import shutil

import pytest

from scanny_boy.library import db as library_db
from scanny_boy.work_dir_support import make_work_dir


@pytest.fixture(autouse=True)
def isolated_library_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNY_BOY_LIBRARY_DB", str(tmp_path / "library.db"))
    library_db.reset_engine_cache()
    yield
    library_db.reset_engine_cache()


@pytest.fixture(scope="session")
def _work_dir_template(tmp_path_factory):
    """The default single-negative work directory, built exactly once.

    Session-scoped, so with `--dist loadfile` each xdist worker pays for this
    at most once. Never handed to a test directly — tests write into their
    work directory (stitching stages files into it, reruns rewrite the
    manifest), so each one needs its own copy.
    """
    return make_work_dir(tmp_path_factory.mktemp("work-dir-template"))


@pytest.fixture
def work_dir(tmp_path, _work_dir_template):
    """A private, writable copy of the default Phase 1 work directory.

    Equivalent to `make_work_dir(tmp_path)`, which is what the tests using
    this fixture used to call. Call `make_work_dir` directly when you need a
    shape the default does not cover — more negatives, a non-complete status,
    injected frame gains, a frame hook, non-overlapping frames.
    """
    destination = tmp_path / "work"
    shutil.copytree(_work_dir_template, destination)
    return destination
