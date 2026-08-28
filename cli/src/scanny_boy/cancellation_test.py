"""The cancellation flag and its SIGTERM handler.

`docs/IMPLEMENTATION_PLAN.md` section 3.8 says the handler must do nothing
but set a flag, so these tests check exactly that: the signal arrives, the
token flips, and the previous handler comes back afterwards.
"""

from __future__ import annotations

import os
import signal
import threading

import pytest

from scanny_boy.cancellation import (
    CancellationToken,
    CancelledError,
    sigterm_cancellation,
)


def test_a_fresh_token_is_not_cancelled():
    token = CancellationToken()
    assert token.cancelled is False
    token.raise_if_cancelled()  # does not raise


def test_cancel_sets_the_flag_and_raise_if_cancelled_raises():
    token = CancellationToken()
    token.cancel()

    assert token.cancelled is True
    with pytest.raises(CancelledError) as excinfo:
        token.raise_if_cancelled()
    assert excinfo.value.code.value == "CANCELLED"


def test_cancelling_twice_is_harmless():
    token = CancellationToken()
    token.cancel()
    token.cancel()
    assert token.cancelled is True


def test_a_token_cancelled_on_one_thread_is_visible_on_another():
    token = CancellationToken()
    seen = threading.Event()

    def _watch() -> None:
        while not token.cancelled:
            pass
        seen.set()

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()
    token.cancel()
    assert seen.wait(timeout=5) is True
    watcher.join(timeout=5)


def test_sigterm_cancels_the_token_and_the_handler_does_nothing_else():
    with sigterm_cancellation() as token:
        assert token.cancelled is False
        os.kill(os.getpid(), signal.SIGTERM)
        assert token.cancelled is True


def test_the_previous_sigterm_handler_is_restored_afterwards():
    calls: list[int] = []

    def _previous(signum: int, frame: object) -> None:
        calls.append(signum)

    original = signal.signal(signal.SIGTERM, _previous)
    try:
        with sigterm_cancellation():
            pass
        assert signal.getsignal(signal.SIGTERM) is _previous
        os.kill(os.getpid(), signal.SIGTERM)
        assert calls == [signal.SIGTERM]
    finally:
        signal.signal(signal.SIGTERM, original)


def test_a_token_is_still_yielded_off_the_main_thread():
    # `signal.signal` only works on the main thread. Rather than fail,
    # the context manager yields a plain token the caller can cancel
    # directly — which is what a test driving `cli.main` from a worker
    # thread needs.
    result: dict[str, object] = {}

    def _run() -> None:
        with sigterm_cancellation() as token:
            token.cancel()
            result["cancelled"] = token.cancelled

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join(timeout=5)

    assert result == {"cancelled": True}
