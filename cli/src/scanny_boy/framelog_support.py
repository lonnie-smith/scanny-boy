"""Shared helpers for the Frame log tests: the committed API fixtures, a
scripted opener that stands in for the network, and a cheap roll with N
sequenced negatives (no stitching, no RAW decoding)."""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Callable
from email.message import Message
from pathlib import Path
from typing import Any, Self

from scanny_boy.roll_manifest import (
    CaptureTime,
    NegativeRecord,
    RunRecord,
    append_run,
    load_roll_manifest,
    write_roll_manifest,
)
from scanny_boy.work_dir_support import make_roll_dir

FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "framelog"
TOKEN = "fl_" + "A" * 43


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text())


def negative_id(index: int) -> str:
    return f"stitch-negative-{index:02d}"


def make_sequenced_roll(tmp_path: Path, count: int, name: str = "out") -> Path:
    """A registered roll with `count` completed negatives whose source
    capture times ascend, so `sequence_negatives` returns them in index
    order."""
    roll_dir = make_roll_dir(tmp_path, name)
    manifest = load_roll_manifest(roll_dir)
    manifest.metadata.roll_capture_date = "2026-08-01"
    append_run(
        manifest,
        RunRecord(
            run_id="stitch-run",
            short_id="stitch",
            kind="stitch",
            status="complete",
            started_at="2026-08-02T00:00:00Z",
            convert_run_id="convert-1",
            input_folder=None,
            source_order=[],
            work_dir="/tmp/work",
            finished_at="2026-08-02T00:10:00Z",
        ),
    )
    for index in range(1, count + 1):
        manifest.negatives.append(
            NegativeRecord(
                negative_id=negative_id(index),
                run_id="stitch-run",
                members=[f"_DSC{index:04d}.NEF"],
                expected_output=f"_DSC{index:04d}.tif",
                fill_color=(0, 0, 0),
                status="completed",
                sequence=index,
                capture_time=CaptureTime(
                    source_datetime_original=f"2026-08-01T10:00:{index:02d}"
                ),
            )
        )
    write_roll_manifest(roll_dir, manifest)
    return roll_dir


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class ScriptedOpener:
    """Answers each request with `respond(request)` and records them."""

    def __init__(self, respond: Callable[[Any], Any]) -> None:
        self._respond = respond
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float | None = None) -> Any:
        self.requests.append(request)
        self.timeout = timeout
        result = self._respond(request)
        if isinstance(result, BaseException):
            raise result
        return result


def json_opener(payload: Any) -> ScriptedOpener:
    body = json.dumps(payload).encode()
    return ScriptedOpener(lambda request: FakeResponse(body))


def http_error(status: int, headers: dict[str, str] | None = None):
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError("https://example.test", status, "x", message, None)
