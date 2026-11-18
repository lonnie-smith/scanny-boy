"""FrameLogClient: request shape, status mapping, and response validation,
all through an injected opener (no network)."""

from __future__ import annotations

import urllib.error

import pytest

from scanny_boy.events import Code
from scanny_boy.framelog_client import (
    FrameLogClient,
    FrameLogError,
    token_from_environment,
)
from scanny_boy.framelog_support import (
    TOKEN,
    FakeResponse,
    ScriptedOpener,
    http_error,
    json_opener,
    load_fixture,
)


def _client(opener) -> FrameLogClient:
    return FrameLogClient("https://example.test/", TOKEN, opener=opener)


def test_list_rolls_request_shape():
    opener = json_opener(load_fixture("rolls"))
    rolls = _client(opener).list_rolls("active")
    assert len(rolls) == 3
    request = opener.requests[0]
    assert request.full_url == (
        "https://example.test/api/framelogger/export/v1/rolls?status=active"
    )
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("Accept") == "application/json"
    assert opener.timeout == 15


def test_get_roll_returns_roll_and_frames_and_quotes_the_ref():
    opener = json_opener(load_fixture("roll_42"))
    roll, frames = _client(opener).get_roll("a/b 42")
    assert roll["uid"] == "k3j9x0a1bc"
    assert [f["frameNumber"] for f in frames] == [1, 2, 3, 5, 6, 7]
    assert opener.requests[0].full_url.endswith("/rolls/a%2Fb%2042")


def test_get_roll_with_no_frames():
    roll, frames = _client(json_opener(load_fixture("roll_empty"))).get_roll(1)
    assert frames == []
    assert roll["lastFrame"] is None


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, Code.FRAMELOG_UNAUTHORIZED),
        (403, Code.FRAMELOG_UNAUTHORIZED),
        (404, Code.FRAMELOG_NOT_FOUND),
        (429, Code.FRAMELOG_RATE_LIMITED),
        (500, Code.FRAMELOG_BAD_RESPONSE),
    ],
)
def test_http_statuses_map_to_codes(status, code):
    opener = ScriptedOpener(lambda request: http_error(status))
    with pytest.raises(FrameLogError) as excinfo:
        _client(opener).list_rolls("all")
    assert excinfo.value.code == code
    assert TOKEN not in excinfo.value.message


def test_rate_limit_message_names_retry_after():
    opener = ScriptedOpener(lambda request: http_error(429, {"Retry-After": "17"}))
    with pytest.raises(FrameLogError) as excinfo:
        _client(opener).get_roll(42)
    assert "17" in excinfo.value.message


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.URLError("dns failure"),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
    ],
)
def test_transport_failures_are_unreachable(failure):
    opener = ScriptedOpener(lambda request: failure)
    with pytest.raises(FrameLogError) as excinfo:
        _client(opener).list_rolls("all")
    assert excinfo.value.code == Code.FRAMELOG_UNREACHABLE


@pytest.mark.parametrize(
    "body",
    [
        b"<html>nope</html>",
        b"",
        b"\xff\xfe",
        b"[]",
        b'{"rolls": {}}',
        b'{"rolls": [1]}',
    ],
)
def test_bad_roll_list_bodies(body):
    opener = ScriptedOpener(lambda request: FakeResponse(body))
    with pytest.raises(FrameLogError) as excinfo:
        _client(opener).list_rolls("all")
    assert excinfo.value.code == Code.FRAMELOG_BAD_RESPONSE


def test_api_version_2_is_rejected():
    payload = load_fixture("roll_42")
    payload["apiVersion"] = 2
    with pytest.raises(FrameLogError) as excinfo:
        _client(json_opener(payload)).get_roll(42)
    assert excinfo.value.code == Code.FRAMELOG_BAD_RESPONSE
    assert "apiVersion" in excinfo.value.message


@pytest.mark.parametrize("version", [None, True, "1"])
def test_api_version_must_be_the_integer_one(version):
    payload = load_fixture("roll_42")
    payload["apiVersion"] = version
    with pytest.raises(FrameLogError) as excinfo:
        _client(json_opener(payload)).get_roll(42)
    assert excinfo.value.code == Code.FRAMELOG_BAD_RESPONSE


@pytest.mark.parametrize("missing", ["roll", "frames"])
def test_missing_required_fields(missing):
    payload = load_fixture("roll_42")
    del payload[missing]
    with pytest.raises(FrameLogError) as excinfo:
        _client(json_opener(payload)).get_roll(42)
    assert excinfo.value.code == Code.FRAMELOG_BAD_RESPONSE


def test_non_2xx_status_without_an_exception_is_bad_response():
    opener = ScriptedOpener(lambda request: FakeResponse(b"{}", status=302))
    with pytest.raises(FrameLogError) as excinfo:
        _client(opener).list_rolls("all")
    assert excinfo.value.code == Code.FRAMELOG_BAD_RESPONSE


def test_non_http_base_url_is_refused_before_any_request():
    opener = json_opener({})
    with pytest.raises(FrameLogError):
        FrameLogClient("file:///etc", TOKEN, opener=opener)
    assert opener.requests == []


@pytest.mark.parametrize("environ", [{}, {"SCANNY_BOY_FRAMELOG_TOKEN": "  "}])
def test_missing_or_empty_token(environ):
    with pytest.raises(FrameLogError) as excinfo:
        token_from_environment(environ)
    assert excinfo.value.code == Code.FRAMELOG_NO_TOKEN


def test_token_is_read_from_the_environment():
    assert token_from_environment({"SCANNY_BOY_FRAMELOG_TOKEN": TOKEN}) == TOKEN
