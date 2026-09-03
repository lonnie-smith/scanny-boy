"""Shared lookup for the real sample NEFs at `tests/fixtures/nef/`.

Per `docs/IMPLEMENTATION_PLAN.md` section 7: resolve the fixtures directory
from this file's own location, not the current working directory, and skip
tests needing them with one shared helper rather than a per-file check.
Expected values are recorded in appendix A; do not re-derive them.
"""

from __future__ import annotations

import shutil
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

requires_real_samples = pytest.mark.skipif(
    bool(_missing),
    reason=(
        "real sample NEFs not present at tests/fixtures/nef/ (see "
        f"docs/IMPLEMENTATION_PLAN.md appendix A); missing: {_missing}"
    ),
)


def stage_samples(tmp_path: Path, names: list[str]) -> Path:
    """A scratch input directory holding only `names`, copied from the
    shared fixtures.

    The fixtures directory keeps growing (it now also holds the gate-B
    stitching scans and later sessions), so a selection of the appendix A
    sample files is no longer contiguous in *its* catalogue. Probing and
    converting the staged directory gives the tests the catalogue they were
    written against — only the six named files, contiguous — while still
    reading the real sample bytes. `shutil.copy2` clones on APFS, so the
    copies cost no real space or time; the catalogue check refuses
    symlinks that resolve outside the input folder, so copying is the one
    supported route. Tests that must *mutate* a source file use
    `pipeline_test._copy_samples` for the same reason.
    """
    input_dir = tmp_path / "samples"
    input_dir.mkdir()
    for name in names:
        shutil.copy2(FIXTURES_DIR / name, input_dir / name)
    return input_dir
