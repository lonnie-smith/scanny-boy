"""Advisory per-roll file locks for concurrent writers.

Two locks guard a roll (docs/PARALLEL_STITCH_PLAN.md §3.4):

- The **roll lock**, ``~/Library/Application Support/ScannyBoy/locks/
  <roll_id>.lock``. Every command that writes a roll takes it exclusively,
  except a deferred ``stitch`` (a parallel commit) and ``stitch
  --compose-only``, which take it shared, so several of them can run at
  once while every exclusive writer is still kept out. ``export`` takes it
  shared too. See docs/TETHER_PLAN.md §4.3.
- The **publish lock**, ``<roll_id>.publish.lock``. A parallel commit holds
  it exclusively for its publish section only (planning, the clamp, the TIFF
  write and the publish transaction), so commits of one roll publish one at
  a time, in order. ``export`` holds it shared for its whole run, so it
  never reads a TIFF a commit is replacing.

Both are non-blocking: contention raises ``RollBusyError`` (``ROLL_BUSY``).
flock locks belong to the open file description, so two opens in one
process contend just as two processes do.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from scanny_boy.events import Code
from scanny_boy.library.db import library_db_path
from scanny_boy.library.repo import roll_id_for_folder


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


def publish_lock_path(roll_id: str) -> Path:
    return locks_root() / f"{roll_id}.publish.lock"


@contextmanager
def _flock(path: Path, mode: int, busy_message: str) -> Iterator[None]:
    """Hold `flock(path, mode | LOCK_NB)` until the block exits; raise
    `RollBusyError(busy_message)` at once if another holder conflicts."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RollBusyError(busy_message) from exc
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _writing_message(roll_dir: Path) -> str:
    return f"another command is writing {roll_dir}; try again when it finishes"


def _publishing_message(roll_dir: Path) -> str:
    return f"another stitch is publishing to {roll_dir}; try again when it finishes"


@contextmanager
def exclusive_roll_lock(roll_dir: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock for one roll until the block exits."""
    roll_id = roll_id_for_folder(roll_dir)
    with _flock(lock_path(roll_id), fcntl.LOCK_EX, _writing_message(roll_dir)):
        yield


@contextmanager
def shared_roll_lock(roll_dir: Path) -> Iterator[None]:
    """Hold a shared advisory lock so reads can proceed alongside each other."""
    roll_id = roll_id_for_folder(roll_dir)
    with _flock(lock_path(roll_id), fcntl.LOCK_SH, _writing_message(roll_dir)):
        yield


@contextmanager
def exclusive_publish_lock(roll_dir: Path) -> Iterator[None]:
    """A parallel commit's publish section: one at a time per roll, and
    never while an export is reading the roll's TIFFs."""
    roll_id = roll_id_for_folder(roll_dir)
    with _flock(
        publish_lock_path(roll_id), fcntl.LOCK_EX, _publishing_message(roll_dir)
    ):
        yield


@contextmanager
def shared_publish_lock(roll_dir: Path) -> Iterator[None]:
    """Held by `export` for its whole run: a commit's publish section waits
    (fails `ROLL_BUSY`) rather than replacing a TIFF an export is reading."""
    roll_id = roll_id_for_folder(roll_dir)
    with _flock(
        publish_lock_path(roll_id), fcntl.LOCK_SH, _publishing_message(roll_dir)
    ):
        yield
