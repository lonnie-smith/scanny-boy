"""`scanny-boy serve`: one resident process answering requests on stdin.

docs/OPTIMIZATION.md §2. The Edit tab pays 0.4-0.7 s of startup on every
gesture because each one-shot invocation re-imports the application; the
daemon pays it once. The transport does not change: this is the same
newline-delimited JSON event stream on stdout the one-shot CLI writes,
read by the app's existing `CLISession` machinery. What changes is the
direction of the other pipe — this process reads newline-delimited JSON
*requests* on stdin:

    {"request_id": "<uuid>", "command": ["edit", "render-region", ...]}

`command` is the argv the one-shot CLI would have received; each request
re-enters `cli.run_argv`, so there is exactly one implementation of every
command and the served path cannot drift from the one-shot path. Every
event written while a request is in flight carries that request's
`request_id`, and each request ends with a `finished` event carrying the
exit status the one-shot CLI would have returned.

Concurrency is deliberately serialized (§2.3): one request at a time on a
worker thread, with this reader loop free to accept a `cancel` for the
request in flight. A queue depth greater than one is a thing to add when a
measurement demands it — the app already assumes supersession everywhere.

Cancellation (§2.2) moves in band: a request whose body is
`{"request_id": "...", "cancel": true}` sets **that request's** token.
SIGTERM to the daemon keeps its one-shot meaning — shut down: it cancels
every live token, lets the in-flight request finish, answers every
still-queued request as cancelled, and exits 0. Closing stdin is the
ordinary stop.

Nothing here writes to stdout outside a request's own events: the stream
is partitioned by `request_id` on the Swift side, and a stray event with
no `request_id` would have nowhere to go.
"""

from __future__ import annotations

import json
import queue
import signal
import sys
import threading
from typing import IO, Any

from scanny_boy.cancellation import CancellationToken
from scanny_boy.cli import run_argv
from scanny_boy.events import Code, ErrorEvent, EventWriter, Finished

# 128 + SIGTERM — the exit status a cancelled request reports, matching the
# one-shot CLI's table in CONTRACT.md.
CANCELLED_EXIT_STATUS = 143


class _LockedStream:
    """A write-through wrapper serializing the request writer and the
    shutdown path's cancellation answers onto one stream."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        with self._lock:
            self._stream.write(text)
            self._stream.flush()

    def flush(self) -> None:
        # `write` already flushed under the lock; this exists so the
        # wrapper is a complete file-like object for `EventWriter`.
        return None


class _Registry:
    """The live requests' cancellation tokens.

    A request registers on dequeue and leaves when its terminal event has
    been written; `cancel` answers the in-band cancel message, and
    `cancel_all` is SIGTERM's "cancel every live token". A cancel for an
    unknown or already-finished id is ignored silently — the app only
    cancels requests it has itself submitted, and an answer that raced a
    completion must not synthesize a second terminal event."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, CancellationToken] = {}

    def register(self, request_id: str) -> CancellationToken:
        token = CancellationToken()
        with self._lock:
            self._tokens[request_id] = token
        return token

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            token = self._tokens.get(request_id)
        if token is None:
            return False
        token.cancel()
        return True

    def remove(self, request_id: str) -> None:
        with self._lock:
            self._tokens.pop(request_id, None)

    def cancel_all(self) -> None:
        with self._lock:
            tokens = list(self._tokens.values())
        for token in tokens:
            token.cancel()


def _answer_cancelled(
    stream: _LockedStream, request_id: str, why: str
) -> None:
    """The terminal pair for a request that will not run: the one-shot
    CLI's SIGTERM shape (an `error` with `CANCELLED`, then `finished` at
    exit status 143), carrying the request's id."""
    writer = EventWriter(stream, request_id=request_id)
    writer.write(ErrorEvent(code=Code.CANCELLED, message=why))
    writer.write(
        Finished(
            status="cancelled", exit_status=CANCELLED_EXIT_STATUS
        )
    )


def _run_request(
    stream: _LockedStream,
    registry: _Registry,
    request_id: str,
    command: list[str],
    token: CancellationToken,
) -> None:
    """One served request, serialized on the worker thread.

    The request's writer carries its `request_id`, so every event the
    command emits is partitioned to it; the request's token reaches the
    pipeline commands in band (`cli.run_argv`'s `cancel`), replacing the
    one-shot SIGTERM scope. If the command returned without writing its
    own `finished` — a usage error's exit 2 is the one such path — one is
    synthesized here, so the Swift side's `CLIOutcome` maps over
    unchanged."""

    class _RequestWriter(EventWriter):
        def __init__(self) -> None:
            super().__init__(stream, request_id=request_id)
            self.saw_finished = False

        def write(self, event) -> None:  # type: ignore[override]
            if isinstance(event, Finished):
                self.saw_finished = True
            super().write(event)

    writer = _RequestWriter()
    try:
        status = run_argv(command, writer, cancel=token)
    except Exception as exc:  # noqa: BLE001 — a request must always end
        writer.write(
            ErrorEvent(
                code=Code.INTERNAL_ERROR,
                message=f"unexpected {type(exc).__name__}: {exc}",
            )
        )
        status = 1
    finally:
        registry.remove(request_id)
    if not writer.saw_finished:
        writer.write(
            Finished(
                status="success" if status == 0 else "failed",
                exit_status=status,
            )
        )


def _read_requests(pending: queue.Queue, registry: _Registry) -> None:
    """The reader loop: one JSON request per stdin line, blocking until
    EOF. Runs on its own thread so the worker's queue never holds a
    `cancel` — those are answered here, immediately, against the token of
    the request in flight."""
    try:
        for line in sys.stdin:
            trimmed = line.strip()
            if not trimmed:
                continue
            try:
                request: dict[str, Any] = json.loads(trimmed)
            except json.JSONDecodeError:
                print(
                    "scanny-boy serve: discarding unparseable request line",
                    file=sys.stderr,
                )
                continue
            request_id = request.get("request_id")
            if request.get("cancel"):
                if isinstance(request_id, str):
                    registry.cancel(request_id)
                continue
            command = request.get("command")
            if not isinstance(request_id, str) or not isinstance(command, list):
                if isinstance(request_id, str):
                    # A well-formed envelope around a malformed command
                    # still deserves its terminal pair, or the app waits
                    # for a request that will never end.
                    pending.put(("invalid", request_id, None))
                else:
                    print(
                        "scanny-boy serve: discarding request with no "
                        "usable request_id",
                        file=sys.stderr,
                    )
                continue
            # The token is registered here, at enqueue time, not on
            # dequeue: a `cancel` for a request still sitting in the queue
            # must reach it, so the worker starts it already cancelled.
            token = registry.register(request_id)
            pending.put(("run", request_id, [str(part) for part in command], token))
    except OSError:
        # The reader's fd can disappear under a shutting-down process;
        # that is an ending, not a failure.
        pass
    finally:
        pending.put(None)


def run_serve() -> int:
    """The `serve` command: read requests until stdin closes or SIGTERM.

    Layout: this (main) thread owns the SIGTERM handler; one reader thread
    parses stdin and answers `cancel` messages immediately; one worker
    thread runs the requests, strictly one at a time. Main waits on the
    worker — SIGTERM wakes it through the handler's side effects, EOF
    wakes it through the reader's sentinel — then returns 0. The process
    dies with the app, whose closing of stdin is the ordinary path; the
    SIGTERM handler is the backstop it already was."""
    registry = _Registry()

    stream = _LockedStream(sys.stdout)
    pending: queue.Queue = queue.Queue()
    shutdown = threading.Event()

    def worker() -> None:
        while True:
            item = pending.get()
            if item is None:
                return
            kind, request_id, command, token = item
            if kind == "invalid":
                # A malformed command under a usable id: answer it as a
                # usage error (exit 2), the one-shot CLI's own shape.
                writer = EventWriter(stream, request_id=request_id)
                writer.write(
                    ErrorEvent(
                        code=Code.INTERNAL_ERROR,
                        message="the request's `command` was not a usable argv",
                    )
                )
                writer.write(
                    Finished(status="failed", exit_status=2)
                )
                continue
            if shutdown.is_set():
                _answer_cancelled(
                    stream,
                    request_id,
                    "the helper was shut down before this request ran",
                )
                registry.remove(request_id)
                continue
            # Run it. A `cancel` that raced the dequeue has already set
            # this token; the request's own terminal pair reports whatever
            # state it reached.
            _run_request(stream, registry, request_id, command, token)

    worker_thread = threading.Thread(target=worker, name="scanny-boy-serve-worker")
    worker_thread.start()

    previous_handler = signal.getsignal(signal.SIGTERM)

    def _on_sigterm(signum: int, frame: object) -> None:
        # §2.2: shut down — cancel every live token, let the in-flight
        # request finish, answer the rest as cancelled, exit 0. The reader
        # is left blocked on stdin and abandoned; nothing it could read
        # matters now.
        registry.cancel_all()
        shutdown.set()

    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        reader_thread = threading.Thread(
            target=_read_requests,
            args=(pending, registry),
            name="scanny-boy-serve-reader",
            daemon=True,
        )
        reader_thread.start()
        worker_thread.join()
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    return 0
