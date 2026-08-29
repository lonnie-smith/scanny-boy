import pytest

from scanny_boy.disk_check import one_frame_bytes
from scanny_boy.events import Code
from scanny_boy.fake_nef_support import write_fake_nef
from scanny_boy.icc_profile import PROFILE_SHA256
from scanny_boy.manifest import (
    CuratedMetadata,
    GroupRecord,
    Manifest,
    OutputRecord,
    write_manifest,
)
from scanny_boy.pipeline import hash_sources
from scanny_boy.probe import ProbeFailure, run_probe
from scanny_boy.sample_nef_support import (
    FIXTURES_DIR,
    NEGATIVE_1,
    NEGATIVE_2,
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

    # A prefix, not the whole catalogue: the fixtures directory also holds
    # Phase 2's gate-B stitching scans, captured 27 days later. That makes this
    # a stronger ordering check than an equality — it proves the canonical sort
    # orders two capture sessions by timestamp, not just one.
    assert outcome.catalogue[: len(REAL_SAMPLE_FILES)] == REAL_SAMPLE_FILES
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


# --- probe --out: output-folder validation, disk estimate, and
# overwrite-conflict preview (section 4.1) --------------------------------


def test_probe_out_dir_is_ignored_without_files(tmp_path):
    write_fake_nef(tmp_path / "a.NEF", date_time_original="2026:08:02 12:00:00")

    outcome, _warnings = _run_probe_collecting_warnings(
        tmp_path, None, 3, out_dir=tmp_path / "does-not-exist"
    )

    assert outcome.output_conflicts == []
    assert outcome.estimated_required_bytes is None
    assert outcome.available_bytes is None


@requires_real_samples
def test_probe_with_out_dir_fresh_folder_reports_a_disk_estimate(tmp_path):
    outcome, _warnings = _run_probe_collecting_warnings(
        FIXTURES_DIR, list(NEGATIVE_1), 3, out_dir=tmp_path
    )

    assert outcome.output_conflicts == []
    assert outcome.available_bytes is not None
    assert outcome.available_bytes > 0
    # Section 3.9's formula multiplies in headroom and a safety margin,
    # so the real estimate is always well above the raw uncompressed size
    # of the missing outputs alone.
    raw_floor = one_frame_bytes(6064, 4040) * len(NEGATIVE_1)
    assert outcome.estimated_required_bytes is not None
    assert outcome.estimated_required_bytes > raw_floor


@requires_real_samples
def test_probe_with_out_dir_same_as_input_is_rejected():
    with pytest.raises(ProbeFailure) as excinfo:
        run_probe(FIXTURES_DIR, list(NEGATIVE_1), 3, out_dir=FIXTURES_DIR)

    assert excinfo.value.code == Code.OUTPUT_SAME_AS_INPUT


@requires_real_samples
def test_probe_with_out_dir_not_writable_is_rejected(tmp_path):
    with pytest.raises(ProbeFailure) as excinfo:
        run_probe(FIXTURES_DIR, list(NEGATIVE_1), 3, out_dir=tmp_path / "does-not-exist")

    assert excinfo.value.code == Code.OUTPUT_NOT_WRITABLE


@requires_real_samples
def test_probe_with_out_dir_nonempty_without_manifest_is_rejected(tmp_path):
    (tmp_path / "unrelated.jpg").touch()

    with pytest.raises(ProbeFailure) as excinfo:
        run_probe(FIXTURES_DIR, list(NEGATIVE_1), 3, out_dir=tmp_path)

    assert excinfo.value.code == Code.OUTPUT_NOT_EMPTY


def _curated_placeholder() -> CuratedMetadata:
    return CuratedMetadata(
        exposure_time="1/30",
        f_number="8",
        iso=100,
        focal_length="55",
        lens_model="55mm f/2.8",
        orientation=1,
        camera_whitebalance=(1.691406, 1.0, 1.378906, 1.0),
    )


def _write_matching_manifest(
    out_dir, *, members: list[str], status: str, film_date: str = "2026-08-02"
) -> None:
    """A manifest `plan_rerun_preview` would treat as describing the same
    run as `members` selected from the real sample NEFs — every field the
    preview compares (source order and hashes, grouping, and ICC) matches;
    `film_date` deliberately does not need to, per section 4.1."""
    records = hash_sources(FIXTURES_DIR, members)
    manifest = Manifest(
        scanny_boy_version="0.1.0",
        run_id="prior-run",
        status="complete" if status == "completed" else "partial",
        input_folder=str(FIXTURES_DIR),
        film_date=film_date,
        shots_per_negative=len(members),
        processing_params={"output_bps": 16},
        icc_profile={"name": "ProPhoto-v4.icc", "sha256": PROFILE_SHA256},
        source_order=members,
        sources=records,
        curated_metadata=_curated_placeholder(),
        groups=[
            GroupRecord(
                group_id="negative-01",
                members=members,
                expected_outputs=[f"{name[:-4]}.tif" for name in members],
                status=status,
                outputs=(
                    [
                        OutputRecord(name=f"{name[:-4]}.tif", size=1, sha256="a" * 64)
                        for name in members
                    ]
                    if status == "completed"
                    else []
                ),
            )
        ],
        started_at="2026-08-28T12:00:00+00:00",
        finished_at="2026-08-28T12:05:00+00:00" if status == "completed" else None,
    )
    write_manifest(out_dir, manifest)


@requires_real_samples
def test_probe_with_out_dir_reports_conflicts_for_a_matching_rerun(tmp_path):
    _write_matching_manifest(tmp_path, members=NEGATIVE_1, status="completed")
    expected_outputs = [f"{name[:-4]}.tif" for name in NEGATIVE_1]
    for name in expected_outputs:
        (tmp_path / name).touch()

    outcome, _warnings = _run_probe_collecting_warnings(
        FIXTURES_DIR, list(NEGATIVE_1), 3, out_dir=tmp_path
    )

    assert outcome.output_conflicts == sorted(expected_outputs)


@requires_real_samples
def test_probe_with_out_dir_manifest_mismatch_is_rejected(tmp_path):
    # Recorded against negative 2, but this probe selects negative 1: the
    # source order and hashes differ, so this is not a matching rerun.
    _write_matching_manifest(tmp_path, members=NEGATIVE_2, status="completed")

    with pytest.raises(ProbeFailure) as excinfo:
        run_probe(FIXTURES_DIR, list(NEGATIVE_1), 3, out_dir=tmp_path)

    assert excinfo.value.code == Code.MANIFEST_MISMATCH


@requires_real_samples
def test_probe_with_out_dir_insufficient_disk_is_rejected(tmp_path, monkeypatch):
    import scanny_boy.probe as probe_module

    class _TinyDiskUsage:
        free = 1

    monkeypatch.setattr(
        probe_module.shutil, "disk_usage", lambda _path: _TinyDiskUsage()
    )

    with pytest.raises(ProbeFailure) as excinfo:
        run_probe(FIXTURES_DIR, list(NEGATIVE_1), 3, out_dir=tmp_path)

    assert excinfo.value.code == Code.INSUFFICIENT_DISK
    assert "1 bytes are available" in excinfo.value.message
