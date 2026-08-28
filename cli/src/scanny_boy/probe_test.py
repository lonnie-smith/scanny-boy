import pytest

from scanny_boy.events import Code
from scanny_boy.fake_nef_support import write_fake_nef
from scanny_boy.probe import ProbeFailure, run_probe
from scanny_boy.sample_nef_support import (
    FIXTURES_DIR,
    REAL_SAMPLE_FILES,
    requires_real_samples,
)


def _run_probe_collecting_warnings(*args, **kwargs):
    warnings: list[tuple[Code, str]] = []
    outcome = run_probe(
        *args, on_warning=lambda code, message: warnings.append((code, message)), **kwargs
    )
    return outcome, warnings


def test_probe_without_files_returns_full_catalogue_in_canonical_order(tmp_path):
    write_fake_nef(tmp_path / "b.NEF", date_time_original="2026:08:02 12:00:10")
    write_fake_nef(tmp_path / "a.NEF", date_time_original="2026:08:02 12:00:05")

    outcome, warnings = _run_probe_collecting_warnings(tmp_path, None, 3)

    assert outcome.catalogue == ["a.NEF", "b.NEF"]
    assert warnings == []
    assert outcome.groups == []


def test_probe_without_files_warns_on_missing_timestamp(tmp_path):
    write_fake_nef(tmp_path / "a.NEF", date_time_original="2026:08:02 12:00:00")
    write_fake_nef(tmp_path / "b.NEF", date_time_original=None)

    _outcome, warnings = _run_probe_collecting_warnings(tmp_path, None, 3)

    assert warnings == [
        (
            Code.FILENAME_SORT_USED,
            (
                "a catalogue file has no usable capture timestamp; sorted "
                "the whole catalogue by filename instead"
            ),
        )
    ]


def test_missing_timestamp_outside_selection_still_warns_even_if_selection_later_fails(
    tmp_path,
):
    # The file with no usable timestamp ("z-missing.NEF") is outside the
    # selection. The whole catalogue still falls back to filename order,
    # and that warning must reach the caller even though this run
    # ultimately fails at a later step (these fake fixtures are a real
    # TIFF but not a real RAW file, so LibRaw reports UNSUPPORTED_RAW when
    # the selected files are opened) — warnings are not batched until a
    # successful finish.
    write_fake_nef(tmp_path / "a.NEF", date_time_original="2026:08:02 12:00:00")
    write_fake_nef(tmp_path / "b.NEF", date_time_original="2026:08:02 12:00:05")
    write_fake_nef(tmp_path / "z-missing.NEF", date_time_original=None)

    # A plain list mutated in-place: run_probe raises before it could
    # return, so warnings must be captured as they're emitted, not read
    # back from a return value.
    observed: list[tuple[Code, str]] = []
    with pytest.raises(ProbeFailure) as excinfo:
        run_probe(
            tmp_path,
            ["a.NEF", "b.NEF"],
            2,
            on_warning=lambda code, message: observed.append((code, message)),
        )

    assert excinfo.value.code == Code.UNSUPPORTED_RAW
    assert observed == [
        (
            Code.FILENAME_SORT_USED,
            (
                "a catalogue file has no usable capture timestamp; sorted "
                "the whole catalogue by filename instead"
            ),
        )
    ]


def test_probe_empty_folder_is_no_files(tmp_path):
    with pytest.raises(ProbeFailure) as excinfo:
        run_probe(tmp_path, None, 3)

    assert excinfo.value.code == Code.NO_FILES


def test_probe_missing_input_folder_is_no_files(tmp_path):
    with pytest.raises(ProbeFailure) as excinfo:
        run_probe(tmp_path / "does-not-exist", None, 3)

    assert excinfo.value.code == Code.NO_FILES


@requires_real_samples
def test_probe_with_files_six_sample_files_groups_by_three():
    outcome, warnings = _run_probe_collecting_warnings(
        FIXTURES_DIR, list(REAL_SAMPLE_FILES), 3
    )

    assert outcome.catalogue == REAL_SAMPLE_FILES
    assert outcome.groups == [
        ["_DSC4638.NEF", "_DSC4639.NEF", "_DSC4640.NEF"],
        ["_DSC4644.NEF", "_DSC4645.NEF", "_DSC4646.NEF"],
    ]
    assert warnings == []


@requires_real_samples
def test_probe_with_files_non_contiguous_selection_is_rejected():
    # Per appendix A: frames 1, 2, 4, 5, 6 skip frame 3 in canonical order.
    files = [
        "_DSC4638.NEF",
        "_DSC4639.NEF",
        "_DSC4644.NEF",
        "_DSC4645.NEF",
        "_DSC4646.NEF",
    ]

    with pytest.raises(ProbeFailure) as excinfo:
        run_probe(FIXTURES_DIR, files, 3)

    assert excinfo.value.code == Code.NON_CONTIGUOUS_SELECTION


@requires_real_samples
def test_probe_with_files_not_divisible_explains_nearest_counts():
    with pytest.raises(ProbeFailure) as excinfo:
        run_probe(FIXTURES_DIR, list(REAL_SAMPLE_FILES), 4)

    assert excinfo.value.code == Code.NOT_DIVISIBLE
    assert "4" in excinfo.value.message
    assert "8" in excinfo.value.message
