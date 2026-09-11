"""Advisory per-roll file lock for concurrent writers.

Every command that writes a roll takes an exclusive lock on
``~/Library/Application Support/ScannyBoy/locks/<roll_id>.lock``.
``export`` takes a shared lock so it can read published TIFFs while a
stitch may be replacing them. See docs/TETHER_PLAN.md §4.3.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from scanny_boy.events import Code
from scanny_boy.library.db import library_db_path
from scanny_boy.library.repo import RollNotRegisteredError, roll_id_for_folder


class RollBusyError(Exception):
    """Maps to ``ROLL_BUSY``: another writer holds the roll lock."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = Code.ROLL_BUSY
        self.message = message


def locks_root() -> Path:
    return library_db_path().parent / "locks"


def lock_path(roll_id: str) -> Path:
    return locks_root() / f"{roll_id}.lock"


@contextmanager
def exclusive_roll_lock(roll_dir: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock for one roll until the block exits."""
    roll_id = roll_id_for_folder(roll_dir)
    locks_root().mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path(roll_id), os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RollBusyError(
                f"another command is writing {roll_dir}; try again when it finishes"
            ) from exc
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def shared_roll_lock(roll_dir: Path) -> Iterator[None]:
    """Hold a shared advisory lock so reads can proceed alongside each other."""
    roll_id = roll_id_for_folder(roll_dir)
    locks_root().mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path(roll_id), os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RollBusyError(
                f"another command is writing {roll_dir}; try again when it finishes"
            ) from exc
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
