"""Tests for the per-roll advisory lock."""

from __future__ import annotations

import threading

import pytest

from scanny_boy.events import Code
from scanny_boy.library import repo
from scanny_boy.roll_folder import create_roll
from scanny_boy.roll_lock import (
    RollBusyError,
    exclusive_publish_lock,
    exclusive_roll_lock,
    lock_path,
    publish_lock_path,
    shared_publish_lock,
    shared_roll_lock,
)


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


# --- the publish lock (docs/PARALLEL_STITCH_PLAN.md §3.4) ---


@pytest.fixture
def roll_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNY_BOY_LIBRARY_DB", str(tmp_path / "library.db"))
    return create_roll(tmp_path / "Library", "Roll-A")


def _busy(lock, roll_dir) -> bool:
    try:
        with lock(roll_dir):
            return False
    except RollBusyError as exc:
        assert exc.code == Code.ROLL_BUSY
        return True


def test_two_opens_in_one_process_contend(roll_dir):
    """flock belongs to the open file description, so the tests below can
    exercise real contention without a second process."""
    with exclusive_roll_lock(roll_dir):
        assert _busy(exclusive_roll_lock, roll_dir)
        assert _busy(shared_roll_lock, roll_dir)


def test_shared_roll_locks_coexist_and_exclude_an_exclusive_one(roll_dir):
    with shared_roll_lock(roll_dir), shared_roll_lock(roll_dir):
        assert _busy(exclusive_roll_lock, roll_dir)
    with exclusive_roll_lock(roll_dir):
        pass


def test_the_exclusive_publish_lock_admits_one_holder(roll_dir):
    with exclusive_publish_lock(roll_dir):
        assert _busy(exclusive_publish_lock, roll_dir)
        assert _busy(shared_publish_lock, roll_dir)
    with exclusive_publish_lock(roll_dir):
        pass


def test_shared_publish_locks_coexist_and_exclude_an_exclusive_one(roll_dir):
    with shared_publish_lock(roll_dir), shared_publish_lock(roll_dir):
        assert _busy(exclusive_publish_lock, roll_dir)
    with exclusive_publish_lock(roll_dir):
        pass


def test_the_publish_lock_is_independent_of_the_roll_lock(roll_dir):
    assert publish_lock_path("x") != lock_path("x")
    assert publish_lock_path("x").name == "x.publish.lock"
    with exclusive_roll_lock(roll_dir), exclusive_publish_lock(roll_dir):
        pass
    with shared_roll_lock(roll_dir), exclusive_publish_lock(roll_dir):
        # The stitch shape: roll lock shared for the lifetime, publish
        # lock exclusive for the publish section; another stitch can hold
        # the roll lock shared meanwhile, but not the publish lock.
        with shared_roll_lock(roll_dir):
            assert _busy(exclusive_publish_lock, roll_dir)
        assert _busy(exclusive_roll_lock, roll_dir)
    with exclusive_publish_lock(roll_dir):
        assert _busy(exclusive_roll_lock, roll_dir) is False


def test_a_released_publish_lock_can_be_retaken_after_an_error(roll_dir):
    with pytest.raises(ValueError), exclusive_publish_lock(roll_dir):
        raise ValueError("boom")
    with exclusive_publish_lock(roll_dir):
        pass
