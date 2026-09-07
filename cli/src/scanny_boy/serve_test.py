"""`scanny-boy serve`'s tests (docs/OPTIMIZATION.md §2.2, §2.5).

The served variant of the contract the one-shot tests already hold:
events partitioned by `request_id`, in-band cancellation that touches
exactly one request, SIGTERM that shuts the daemon down without lying
about any request, and a terminal `finished` for every request whatever
happened to it. These run `run_serve` in-process against a piped stdin;
the Swift side's served-variant tests live in `CLISessionTests`.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time

import pytest

from scanny_boy.pipeline import ConvertOutcome
from scanny_boy.serve import run_serve


def _request(request_id: str, command: list[str]) -> str:
    return json.dumps({"request_id": request_id, "command": command}) + "\n"


def _cancel(request_id: str) -> str:
    return json.dumps({"request_id": request_id, "cancel": True}) + "\n"


def _events(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _by_request(events: list[dict]) -> dict[str, list[dict]]:
    partitioned: dict[str, list[dict]] = {}
    for event in events:
        request_id = event.get("request_id")
        assert request_id, f"event without request_id: {event}"
        partitioned.setdefault(request_id, []).append(event)
    return partitioned


def _stub_run_convert(
    started: threading.Event,
    *,
    wait_for_cancel: bool = True,
    timeout: float = 5.0,
):
    """A `prepare` implementation that blocks until its token is cancelled,
    then reports the cancelled outcome — the in-process stand-in for a
    long run the user cancels."""

    def _run(*args, **kwargs):
        token = kwargs["cancel"]
        started.set()
        deadline = time.monotonic() + timeout
        while wait_for_cancel and not token.cancelled:
            if time.monotonic() > deadline:
                break
            time.sleep(0.01)
        if token.cancelled:
            return ConvertOutcome(
                run_id=kwargs["run_id"], status="cancelled", manifest=None
            )
        return ConvertOutcome(
            run_id=kwargs["run_id"], status="complete", manifest=None
        )

    return _run


def test_two_requests_partition_by_request_id(capsys, tmp_path, monkeypatch):
    """§2.5: two overlapping requests both complete; events are correctly
    partitioned by `request_id`, and neither sees the other's."""
    first, second = "req-a", "req-b"
    monkeypatch.setattr(
        "sys.stdin",
        _StringIO(
            _request(first, ["roll", "list", "--library", str(tmp_path)])
            + _request(second, ["roll", "list", "--library", str(tmp_path)])
        ),
    )

    status = run_serve()

    assert status == 0
    partitioned = _by_request(_events(capsys))
    assert set(partitioned) == {first, second}
    for events in partitioned.values():
        assert [e["event"] for e in events] == [
            "started",
            "roll_list",
            "finished",
        ]
        assert events[-1]["exit_status"] == 0


def test_cancel_sets_only_that_request_token(capsys, tmp_path, monkeypatch):
    """§2.5: cancel request A while B runs (here: A queued and started,
    B behind it) — A reports cancelled, B completes untouched."""
    slow = _stub_run_convert(threading.Event())
    started = threading.Event()
    monkeypatch.setattr(
        "scanny_boy.pipeline.run_convert",
        lambda *a, **k: (started.set(), slow(*a, **k))[1],
    )
    cancelled_id, other_id = "req-cancelled", "req-other"
    monkeypatch.setattr(
        "sys.stdin",
        _StringIO(
            _request(
                cancelled_id,
                [
                    "prepare",
                    "--input",
                    str(tmp_path),
                    "--files",
                    "a.NEF",
                    "--out",
                    str(tmp_path / "out"),
                    "--per-negative",
                    "1",
                ],
            )
            + _cancel(cancelled_id)
            + _request(other_id, ["roll", "list", "--library", str(tmp_path)])
        ),
    )

    status = run_serve()

    assert status == 0
    partitioned = _by_request(_events(capsys))
    cancelled_events = partitioned[cancelled_id]
    assert [e["event"] for e in cancelled_events] == [
        "started",
        "error",
        "finished",
    ]
    assert cancelled_events[1]["code"] == "CANCELLED"
    assert cancelled_events[-1]["exit_status"] == 143
    other_events = partitioned[other_id]
    assert [e["event"] for e in other_events] == [
        "started",
        "roll_list",
        "finished",
    ]
    assert other_events[-1]["exit_status"] == 0


def test_sigterm_shuts_down_and_answers_queued_requests_cancelled(
    capsys, tmp_path, monkeypatch
):
    """§2.2: SIGTERM to the daemon means shut down — every live token is
    cancelled, the in-flight request finishes, and a still-queued request
    is answered as cancelled rather than left hanging."""
    started = threading.Event()
    monkeypatch.setattr(
        "scanny_boy.pipeline.run_convert",
        _stub_run_convert(started),
    )
    in_flight_id, queued_id = "req-in-flight", "req-queued"
    monkeypatch.setattr(
        "sys.stdin",
        _StringIO(
            _request(
                in_flight_id,
                [
                    "prepare",
                    "--input",
                    str(tmp_path),
                    "--files",
                    "a.NEF",
                    "--out",
                    str(tmp_path / "out"),
                    "--per-negative",
                    "1",
                ],
            )
            + _request(queued_id, ["roll", "list", "--library", str(tmp_path)])
        ),
    )

    serve_returned = threading.Event()

    def _send_sigterm() -> None:
        started.wait(timeout=5.0)
        time.sleep(0.2)
        if serve_returned.is_set():
            return
        os.kill(os.getpid(), signal.SIGTERM)

    killer = threading.Thread(target=_send_sigterm, daemon=True)
    killer.start()

    status = run_serve()
    serve_returned.set()

    assert status == 0
    partitioned = _by_request(_events(capsys))
    in_flight = partitioned[in_flight_id]
    assert [e["event"] for e in in_flight] == ["started", "error", "finished"]
    assert in_flight[1]["code"] == "CANCELLED"
    assert in_flight[-1]["exit_status"] == 143
    queued = partitioned[queued_id]
    assert [e["event"] for e in queued] == ["error", "finished"]
    assert queued[0]["code"] == "CANCELLED"
    assert queued[-1]["exit_status"] == 143


def test_a_usage_error_still_ends_its_request(capsys, tmp_path, monkeypatch):
    """A command line the parser refuses produces no events of its own;
    the daemon synthesizes the terminal `finished` at exit status 2, so
    the app's registry never waits on a request that will not end."""
    request_id = "req-usage"
    monkeypatch.setattr(
        "sys.stdin",
        _StringIO(_request(request_id, ["no-such-command", "--roll", str(tmp_path)])),
    )

    status = run_serve()

    assert status == 0
    events = _events(capsys)
    # The one-shot CLI's usage-error shape is exit 2 with no events; the
    # daemon's synthesized `finished` is the only line the request adds.
    assert [e["event"] for e in events] == ["finished"]
    assert events[0]["request_id"] == request_id
    assert events[-1]["exit_status"] == 2


def test_a_cancel_for_an_unknown_request_is_ignored(capsys, monkeypatch):
    """A cancel that races a completion must not synthesize a second
    terminal event for a request that already ended."""
    monkeypatch.setattr("sys.stdin", _StringIO(_cancel("no-such-request")))

    status = run_serve()

    assert status == 0
    assert _events(capsys) == []


class _StringIO:
    """A minimal text-stream stand-in for `sys.stdin`: iteration yields
    the pre-written lines, then EOF."""

    def __init__(self, text: str) -> None:
        self._lines = text.splitlines(keepends=True)

    def __iter__(self):
        return iter(self._lines)

    def readline(self) -> str:
        if not self._lines:
            return ""
        return self._lines.pop(0)
