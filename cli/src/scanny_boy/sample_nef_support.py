"""Shared lookup for the real sample NEFs at `tests/fixtures/nef/`.

Per `docs/IMPLEMENTATION_PLAN.md` section 7: resolve the fixtures directory
from this file's own location, not the current working directory, and skip
tests needing them with one shared helper rather than a per-file check.
Expected values are recorded in appendix A; do not re-derive them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "nef"

REAL_SAMPLE_FILES = [
    "_DSC4638.NEF",
    "_DSC4639.NEF",
    "_DSC4640.NEF",
    "_DSC4644.NEF",
    "_DSC4645.NEF",
    "_DSC4646.NEF",
]

NEGATIVE_1 = ["_DSC4638.NEF", "_DSC4639.NEF", "_DSC4640.NEF"]
NEGATIVE_2 = ["_DSC4644.NEF", "_DSC4645.NEF", "_DSC4646.NEF"]

_missing = [name for name in REAL_SAMPLE_FILES if not (FIXTURES_DIR / name).exists()]

# Both a skip (fixtures absent) and a marker (so the default pytest run can
# deselect the whole group regardless — see `addopts` in pyproject.toml).
# The two marks are applied one after the other: nesting one MarkDecorator
# inside another stores it as plain data and silently drops the skipif.
def requires_real_samples(func):
    return pytest.mark.real_samples(
        pytest.mark.skipif(
            bool(_missing),
            reason=(
                "real sample NEFs not present at tests/fixtures/nef/ (see "
                f"docs/IMPLEMENTATION_PLAN.md appendix A); missing: {_missing}"
            ),
        )(func)
    )
