"""Cooperative cancellation: a shared flag, and the SIGTERM handler that
sets it.

See `docs/IMPLEMENTATION_PLAN.md` section 3.8. Two rules shape this module:

- "The Python signal handler sets a cancellation flag. Cleanup happens
  through normal control flow; the handler does not perform complex file
  operations." `_handler` below does exactly one thing — `token.cancel()`.
  Every deletion, manifest update, and final event happens later, on the
  main thread, in `pipeline.run_convert`.
- "A task that has started cannot be cancelled safely; let it finish its
  current RAW call and check the cancellation flag between the decode,
  TIFF-writing, and metadata steps." That is what `raise_if_cancelled` is
  for: worker threads call it at those step boundaries, never mid-decode.

`threading.Event` is the flag itself, so a token set from the signal
handler (which runs on the main thread) is visible to every worker thread
without further locking.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from scanny_boy.events import Code


class CancelledError(Exception):
    """Raised at a pipeline step boundary once cancellation was requested.

    Maps to `CANCELLED` and, at the process level, to exit status 143
    (128 + SIGTERM). Deliberately not a subclass of the group-failure
    exceptions `pipeline` handles: a cancelled group is abandoned, not
    recorded as `failed`.
    """

    def __init__(self, message: str = "the run was cancelled") -> None:
        super().__init__(message)
        self.code = Code.CANCELLED
        self.message = message


class CancellationToken:
    """A one-way flag shared by the main thread and every worker thread."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledError()


@contextmanager
def sigterm_cancellation() -> Iterator[CancellationToken]:
    """Install a SIGTERM handler that cancels the yielded token, and
    restore the previous handler on the way out.

    `signal.signal` can only be called from the main thread. When it
    cannot (a test driving `cli.main` from a worker thread, for example),
    yield a plain token instead of failing: the caller can still cancel it
    directly, which is what such a test wants anyway.
    """
    token = CancellationToken()

    def _handler(signum: int, frame: object) -> None:
        token.cancel()

    try:
        previous = signal.signal(signal.SIGTERM, _handler)
    except ValueError:
        yield token
        return

    try:
        yield token
    finally:
        signal.signal(signal.SIGTERM, previous)
