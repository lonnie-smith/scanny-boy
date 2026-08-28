"""Shared lookup for the packaged helper bundle at `cli/dist/ScannyBoyCLI.app`.

The bundle is build output: `cli/dist/` is ignored by Git and is absent from a
clean checkout, so the packaged checks of `docs/IMPLEMENTATION_PLAN.md`
section 5.2 skip clearly when it has not been built, in the same style as the
real-sample-NEF helper.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[2]

BUNDLE_PATH = CLI_DIR / "dist" / "ScannyBoyCLI.app"
BUNDLE_EXECUTABLE = BUNDLE_PATH / "Contents" / "MacOS" / "scanny-boy"
DEV_VENV = CLI_DIR / ".venv"

requires_packaged_app = pytest.mark.skipif(
    not BUNDLE_EXECUTABLE.exists(),
    reason=(
        f"packaged helper not built at {BUNDLE_PATH}; the packaged checks "
        "(--version, probe, jobs 1, jobs 4, cancellation, signature, and the "
        "development-vs-packaged comparison) did not run. Build it with "
        "./scripts/build-cli.sh"
    ),
)


def run_packaged(
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float = 600,
) -> subprocess.CompletedProcess[str]:
    """Run the packaged executable to completion, capturing both streams.

    stdout and stderr are captured separately because the contract keeps them
    separate: stdout is the JSON event stream, stderr is human-readable log
    text that is never parsed. `env` adds to the caller's environment rather
    than replacing it, so a run under test differs from an ordinary run only
    in the variables a test deliberately sets.
    """
    return subprocess.run(
        [str(BUNDLE_EXECUTABLE), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=None if env is None else os.environ | env,
        check=False,
    )
