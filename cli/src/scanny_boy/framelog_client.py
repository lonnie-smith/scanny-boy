"""A small HTTP client for the Frame log export API
(docs/METADATA_IMPORT_PLAN.md section 5.1).

Built on `urllib.request` only: the PyInstaller bundle must not grow a new
dependency. The opener is injectable so tests never touch the network.

The bearer token is only ever placed in the `Authorization` header. It is
never logged, never put in an error message, and never emitted.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from scanny_boy.events import Code

DEFAULT_BASE_URL = "https://lonnie-smith.com"
TOKEN_ENV_VAR = "SCANNY_BOY_FRAMELOG_TOKEN"
API_PATH = "/api/framelogger/export/v1"
TIMEOUT_SECONDS = 15
SUPPORTED_API_VERSION = 1


class FrameLogError(Exception):
    """A Frame log failure, carrying one of the `FRAMELOG_*` codes."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def token_from_environment(environ: Mapping[str, str] | None = None) -> str:
    """The bearer token, or `FRAMELOG_NO_TOKEN` when it is unset or empty.
    Called before any request is built."""
    environ = os.environ if environ is None else environ
    token = environ.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        raise FrameLogError(
            Code.FRAMELOG_NO_TOKEN,
            f"{TOKEN_ENV_VAR} is not set; save a Frame log token in Settings",
        )
    return token


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The API never redirects. Refusing to follow one keeps the bearer
    token from being replayed at another host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _default_open(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)


class FrameLogClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self._base = base_url.rstrip("/") + API_PATH
        self._token = token
        self._opener = opener if opener is not None else _default_open
        scheme = urllib.parse.urlsplit(self._base).scheme
        if scheme not in ("http", "https"):
            raise FrameLogError(
                Code.FRAMELOG_UNREACHABLE,
                f"the Frame log base URL must be http or https, got {base_url!r}",
            )

    def list_rolls(self, status: str = "all") -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"status": status})
        data = self._get_json(f"/rolls?{query}")
        rolls = data.get("rolls") if isinstance(data, dict) else None
        if not isinstance(rolls, list) or not all(
            isinstance(roll, dict) and isinstance(roll.get("uid"), str)
            for roll in rolls
        ):
            raise _bad("the roll list is missing its 'rolls' array")
        return rolls

    def get_roll(self, ref: str | int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """One roll and its frames, as `(roll, frames)` in API shape."""
        data = self._get_json(f"/rolls/{urllib.parse.quote(str(ref), safe='')}")
        if not isinstance(data, dict):
            raise _bad("the roll response is not a JSON object")
        version = data.get("apiVersion")
        if type(version) is not int or version != SUPPORTED_API_VERSION:
            raise _bad(
                f"unsupported Frame log apiVersion {version!r}; "
                f"expected {SUPPORTED_API_VERSION}"
            )
        roll, frames = data.get("roll"), data.get("frames")
        if not isinstance(roll, dict) or not isinstance(roll.get("uid"), str):
            raise _bad("the roll response has no 'roll' object with a uid")
        if not isinstance(frames, list) or not all(
            isinstance(frame, dict)
            and isinstance(frame.get("uid"), str)
            and type(frame.get("frameNumber")) is int
            for frame in frames
        ):
            raise _bad("the roll response has a malformed 'frames' array")
        return roll, frames

    def _get_json(self, path: str) -> Any:
        request = urllib.request.Request(
            self._base + path,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", 200)
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise _error_for_status(exc.code, exc.headers) from None
        except urllib.error.URLError as exc:
            raise _unreachable(exc.reason) from None
        except http.client.HTTPException as exc:
            raise _bad(f"malformed HTTP response ({type(exc).__name__})") from None
        except OSError as exc:  # timeouts, resets, TLS failures
            raise _unreachable(exc) from None
        if not 200 <= status < 300:
            raise _error_for_status(status, None)
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise _bad("the response body is not JSON") from None


def _bad(message: str) -> FrameLogError:
    return FrameLogError(Code.FRAMELOG_BAD_RESPONSE, message)


def _unreachable(reason: object) -> FrameLogError:
    detail = reason if isinstance(reason, str) else f"{type(reason).__name__}: {reason}"
    return FrameLogError(
        Code.FRAMELOG_UNREACHABLE, f"could not reach Frame log ({detail})"
    )


def _error_for_status(status: int, headers: Any) -> FrameLogError:
    if status in (401, 403):
        return FrameLogError(
            Code.FRAMELOG_UNAUTHORIZED,
            f"Frame log rejected the token (HTTP {status}); check it in Settings",
        )
    if status == 404:
        return FrameLogError(Code.FRAMELOG_NOT_FOUND, "Frame log roll not found")
    if status == 429:
        retry_after = headers.get("Retry-After") if headers is not None else None
        suffix = f"; retry after {retry_after} seconds" if retry_after else ""
        return FrameLogError(
            Code.FRAMELOG_RATE_LIMITED, f"Frame log rate limit reached{suffix}"
        )
    return _bad(f"Frame log answered HTTP {status}")
