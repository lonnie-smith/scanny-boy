"""Tests for the per-roll advisory lock."""

from __future__ import annotations

import threading

import pytest

from scanny_boy.events import Code
from scanny_boy.library import repo
from scanny_boy.roll_folder import create_roll
from scanny_boy.roll_lock import RollBusyError, exclusive_roll_lock


def test_second_exclusive_writer_fails_immediately(tmp_path, monkeypatch):
    library = tmp_path / "Library"
    roll_dir = create_roll(library, "Roll-A")
    monkeypatch.setenv("SCANNY_BOY_LIBRARY_DB", str(tmp_path / "library.db"))

    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with exclusive_roll_lock(roll_dir):
            acquired.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=hold_lock, daemon=True)
    thread.start()
    assert acquired.wait(timeout=5)

    with pytest.raises(RollBusyError) as exc_info, exclusive_roll_lock(roll_dir):
        pass
    assert exc_info.value.code == Code.ROLL_BUSY

    release.set()
    thread.join(timeout=5)


def test_unregistered_roll_raises_roll_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNY_BOY_LIBRARY_DB", str(tmp_path / "library.db"))
    missing = tmp_path / "nope"
    missing.mkdir()
    with pytest.raises(repo.RollNotRegisteredError), exclusive_roll_lock(missing):
        pass
