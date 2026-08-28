import math
from collections import namedtuple

import pytest

from scanny_boy.disk_check import (
    MIB,
    DiskCheckError,
    check_disk_space,
    one_frame_bytes,
    required_free_bytes,
)

# Appendix A dimensions, identical across all six real sample files.
WIDTH = 6064
HEIGHT = 4040
PER_NEGATIVE = 3

_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free"])


def _hand_computed_required(*, missing_output_count: int) -> int:
    # Section 3.9's formula, reproduced independently of disk_check.py so
    # this test would catch a regression in the implementation.
    p = WIDTH * HEIGHT * 3 * 2
    b = math.ceil(p * 1.05)
    d = MIB  # our manifest-size estimate is always well under 1 MiB here
    return math.ceil((missing_output_count * b + 2 * PER_NEGATIVE * b + d) * 1.20)


def test_one_frame_bytes_matches_appendix_a():
    # Appendix A: "P = 6064 x 4040 x 3 x 2 = 146,991,360 bytes."
    assert one_frame_bytes(WIDTH, HEIGHT) == 146_991_360


def test_required_free_bytes_fresh_run_matches_hand_computed_formula():
    # Appendix A's "fresh six-file run": M = 6 (all outputs missing).
    required = required_free_bytes(
        width=WIDTH,
        height=HEIGHT,
        missing_output_count=6,
        largest_group_size=PER_NEGATIVE,
        manifest_size_estimate=1024,  # well under 1 MiB, so D = 1 MiB dominates
    )
    assert required == _hand_computed_required(missing_output_count=6)


def test_required_free_bytes_pure_overwrite_has_zero_missing_but_still_requires_staging():
    # Section 3.9: "For a pure overwrite, M is zero because old files
    # already occupy disk, but the staging term still applies."
    zero_missing = required_free_bytes(
        width=WIDTH,
        height=HEIGHT,
        missing_output_count=0,
        largest_group_size=PER_NEGATIVE,
        manifest_size_estimate=1024,
    )
    assert zero_missing == _hand_computed_required(missing_output_count=0)
    assert zero_missing > 0  # the 2 x G x B staging term alone is nonzero


def test_required_free_bytes_manifest_size_estimate_floor_is_one_mib():
    # D = max(1 MiB, estimated manifest size): a tiny estimate is floored.
    with_tiny_estimate = required_free_bytes(
        width=WIDTH, height=HEIGHT, missing_output_count=0, largest_group_size=1, manifest_size_estimate=1
    )
    with_one_mib = required_free_bytes(
        width=WIDTH,
        height=HEIGHT,
        missing_output_count=0,
        largest_group_size=1,
        manifest_size_estimate=MIB,
    )
    assert with_tiny_estimate == with_one_mib


@pytest.mark.parametrize("missing_output_count", [0, 6])
def test_check_disk_space_fails_just_below_and_passes_just_above(
    tmp_path, monkeypatch, missing_output_count
):
    required = required_free_bytes(
        width=WIDTH,
        height=HEIGHT,
        missing_output_count=missing_output_count,
        largest_group_size=PER_NEGATIVE,
        manifest_size_estimate=1024,
    )

    def _fake_usage(_path):
        return _DiskUsage(total=required * 10, used=0, free=_fake_usage.free)

    monkeypatch.setattr("scanny_boy.disk_check.shutil.disk_usage", _fake_usage)

    _fake_usage.free = required - 1
    with pytest.raises(DiskCheckError) as excinfo:
        check_disk_space(tmp_path, required)
    assert excinfo.value.required_bytes == required
    assert excinfo.value.available_bytes == required - 1

    _fake_usage.free = required
    check_disk_space(tmp_path, required)  # exactly enough: must not raise
