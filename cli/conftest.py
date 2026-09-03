"""Root test configuration.

`pytest_addoption` is only honoured in conftests pytest loads before it
parses the command line, which for this project means the rootdir — the
per-package `src/scanny_boy/conftest.py` loads too late. This file is
therefore also home to the `slow` gate: tests marked `slow` (real RAW
decoding, real stitching of the sample scans, the packaged-app runs) skip
unless pytest is passed `--slow`, so an ordinary `pytest` run — an agent
iterating, or CI on a clean checkout — costs minutes, not tens of minutes.
"""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--slow",
        action="store_true",
        default=False,
        help="also run the slow tests (real RAW decoding, real stitching, packaged app)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if config.getoption("--slow"):
        return
    skip_slow = pytest.mark.skip(
        reason="slow test; pass --slow to include it (registered via the `slow` marker)"
    )
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)
