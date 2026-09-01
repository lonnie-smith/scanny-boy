import shutil

import pytest

from scanny_boy import hashing
from scanny_boy.disk_check import one_frame_bytes
from scanny_boy.events import Code
from scanny_boy.fake_nef_support import write_fake_nef
from scanny_boy.icc_profile import PROFILE_FILENAME, PROFILE_SHA256
from scanny_boy.manifest import (
    CuratedMetadata,
    GroupRecord,
    Manifest,
    OutputRecord,
    current_scanny_boy_version,
    write_manifest,
)
from scanny_boy.pipeline import hash_sources
from scanny_boy.probe import ProbeFailure, run_probe
from scanny_boy.raw_decode import jsonable_raw_params
from scanny_boy.sample_nef_support import (
    FIXTURES_DIR,
    NEGATIVE_1,
    NEGATIVE_2,
    REAL_SAMPLE_FILES,
    requires_real_samples,
)

# The rolls the --roll tests probe are built by a genuine `stitch` through
# P3-2's writer (section 4); the stitch fixtures are the one place that
# machinery lives.
from scanny_boy.stitch_pipeline_test import (
    _negative_frames,
    _roll_dir,
    _stitch,
    _write_intermediate,
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
        icc_profile={"name": "ScannyBoy-ROMM-LibRaw-v4.icc", "sha256": PROFILE_SHA256},
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


# --- probe --roll (Phase 3 section 3.5) -----------------------------------


def _stitched_roll(tmp_path, *, groups: list[list[str]], run_id: str = "stitch-run"):
    """A real roll built by a genuine `stitch` through P3-2's writer
    (section 4: a hand-authored manifest proves nothing about overlap). The
    work manifest's sources carry the real sample files' names and sha256
    hashes — that is what overlap detection compares — while the
    intermediates are synthetic Phase 1 TIFFs, so only RAW decoding is
    skipped. `processing_params` is the real decode parameter set, matching
    what a `run` would present as its invariants."""
    members = [name for group in groups for name in group]
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    group_records = []
    for index, group_members in enumerate(groups):
        frames = _negative_frames(
            overlapping=True, seed=11 + index * 7, count=len(group_members)
        )
        outputs = []
        for member, pixels in zip(group_members, frames):
            output_name = f"{member[:-4]}.tif"
            _write_intermediate(work_dir / output_name, pixels, member)
            outputs.append(
                OutputRecord(
                    name=output_name,
                    size=(work_dir / output_name).stat().st_size,
                    sha256=hashing.sha256_file(work_dir / output_name),
                )
            )
        group_records.append(
            GroupRecord(
                group_id=f"negative-{index + 1:02d}",
                members=list(group_members),
                expected_outputs=[f"{m[:-4]}.tif" for m in group_members],
                status="completed",
                outputs=outputs,
            )
        )

    write_manifest(
        work_dir,
        Manifest(
            scanny_boy_version=current_scanny_boy_version(),
            run_id="convert-run",
            status="complete",
            input_folder=str(FIXTURES_DIR),
            film_date="2026-08-02",
            shots_per_negative=len(groups[0]),
            processing_params=jsonable_raw_params(),
            icc_profile={"name": PROFILE_FILENAME, "sha256": PROFILE_SHA256},
            source_order=members,
            sources=hash_sources(FIXTURES_DIR, members),
            curated_metadata=_curated_placeholder(),
            groups=group_records,
            started_at="2026-08-02T00:00:00Z",
            finished_at="2026-08-02T00:01:00Z",
        ),
    )

    roll = _roll_dir(tmp_path, "roll", shots_per_negative=len(groups[0]))
    outcome = _stitch(work_dir, roll, run_id=run_id)
    assert outcome.status == "complete"
    assert outcome.failed == []
    return roll


@requires_real_samples
def test_roll_overlap_empty_for_fresh_sources(tmp_path):
    """Section 3.5: a selection the roll has never seen reports no overlap —
    the normal additive case."""
    roll = _stitched_roll(tmp_path, groups=[NEGATIVE_1])

    outcome = run_probe(FIXTURES_DIR, list(NEGATIVE_2), 3, roll_dir=roll)

    assert outcome.roll_overlap == []


@requires_real_samples
def test_roll_overlap_names_the_prior_negative(tmp_path):
    roll = _stitched_roll(tmp_path, groups=[NEGATIVE_1])

    outcome = run_probe(FIXTURES_DIR, list(NEGATIVE_1), 3, roll_dir=roll)

    [entry] = outcome.roll_overlap
    assert entry.negative_id == "stitch-negative-01"
    assert entry.expected_output == "_DSC4638.tif"
    assert entry.run_id == "stitch-run"
    assert entry.overlapping_sources == list(NEGATIVE_1)
    assert entry.group_index == 0


@requires_real_samples
def test_roll_overlap_detects_renamed_file_by_hash(tmp_path):
    """Section 3.5: overlap is a comparison by sha256, so a rescanned frame
    under a new name still matches the negative it came from."""
    roll = _stitched_roll(tmp_path, groups=[NEGATIVE_1])
    rescan = tmp_path / "rescan"
    rescan.mkdir()
    shutil.copy(FIXTURES_DIR / "_DSC4638.NEF", rescan / "RESCAN-0001.NEF")
    for name in ("_DSC4639.NEF", "_DSC4640.NEF"):
        shutil.copy(FIXTURES_DIR / name, rescan / name)

    outcome = run_probe(
        rescan, ["RESCAN-0001.NEF", "_DSC4639.NEF", "_DSC4640.NEF"], 3, roll_dir=roll
    )

    [entry] = outcome.roll_overlap
    assert entry.negative_id == "stitch-negative-01"
    assert entry.overlapping_sources == [
        "RESCAN-0001.NEF",
        "_DSC4639.NEF",
        "_DSC4640.NEF",
    ]


@requires_real_samples
def test_roll_overlap_detects_regrouped_sources(tmp_path):
    """Section 3.5: a selection whose grouping straddles the roll's negative
    boundaries — here the tail of negative 1 plus the head of negative 2 in
    one prospective group — collides with both, and each entry names only
    the sources the two actually share. `shots_per_negative` cannot change
    (section 3.4), so this straddle is how a regroup reaches the roll."""
    roll = _stitched_roll(tmp_path, groups=[NEGATIVE_1, NEGATIVE_2])

    outcome = run_probe(
        FIXTURES_DIR, ["_DSC4639.NEF", "_DSC4640.NEF", "_DSC4644.NEF"], 3, roll_dir=roll
    )

    assert [entry.group_index for entry in outcome.roll_overlap] == [0, 0]
    by_id = {entry.negative_id: entry for entry in outcome.roll_overlap}
    assert sorted(by_id) == ["stitch-negative-01", "stitch-negative-02"]
    assert by_id["stitch-negative-01"].overlapping_sources == ["_DSC4639.NEF", "_DSC4640.NEF"]
    assert by_id["stitch-negative-01"].expected_output == "_DSC4638.tif"
    assert by_id["stitch-negative-02"].overlapping_sources == ["_DSC4644.NEF"]
    assert by_id["stitch-negative-02"].expected_output == "_DSC4644.tif"
    assert {entry.run_id for entry in outcome.roll_overlap} == {"stitch-run"}


@requires_real_samples
def test_probe_rejects_changed_shots_per_negative(tmp_path):
    """Section 3.5: `--roll` validates the roll invariants of section 3.4 —
    `shots_per_negative` was fixed at roll creation and cannot change."""
    roll = _stitched_roll(tmp_path, groups=[NEGATIVE_1, NEGATIVE_2])

    with pytest.raises(ProbeFailure) as excinfo:
        run_probe(FIXTURES_DIR, list(REAL_SAMPLE_FILES), 2, roll_dir=roll)

    assert excinfo.value.code == Code.ROLL_INVARIANT_MISMATCH


# --- flat-field: --roll surfaces a profile mismatch before any run ---------


def _save_flatfield_profile(profile_id: str, name: str):
    import numpy as np

    from scanny_boy import flatfield
    from scanny_boy.library import repo

    path, sha256 = flatfield.save_gain_map(
        profile_id, np.full((8, 8, 3), 1.25, dtype=np.float32)
    )
    profile = flatfield.FlatFieldProfile(
        profile_id=profile_id,
        name=name,
        gain_map_path=str(path),
        gain_map_sha256=sha256,
        source_path=None,
        reference_width=12,
        reference_height=8,
        params=flatfield.build_params(),
        scanny_boy_version="0.3.0",
        created_at="2026-09-01T00:00:00Z",
    )
    repo.save_flatfield_profile(profile)
    return profile


def _catalogue_dir(tmp_path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    write_fake_nef(input_dir / "a.NEF", date_time_original="2026:08:02 12:00:00")
    write_fake_nef(input_dir / "b.NEF", date_time_original="2026:08:02 12:00:05")
    return input_dir


def test_probe_with_unknown_flatfield_profile_fails_before_the_roll(tmp_path):
    from scanny_boy.roll_manifest import new_roll_manifest, write_roll_manifest

    input_dir = _catalogue_dir(tmp_path)
    roll_dir = tmp_path / "Roll"
    write_roll_manifest(
        roll_dir, new_roll_manifest(roll_id="rid-1", roll_name="Roll", shots_per_negative=2)
    )

    with pytest.raises(ProbeFailure) as excinfo:
        run_probe(input_dir, None, 2, roll_dir=roll_dir, flatfield_profile_id="nope")

    assert excinfo.value.code == Code.FLATFIELD_PROFILE_NOT_FOUND


def test_probe_with_roll_reports_a_flatfield_mismatch(tmp_path):
    from scanny_boy import flatfield
    from scanny_boy.roll_manifest import (
        RunRecord,
        new_roll_manifest,
        write_roll_manifest,
    )
    from scanny_boy.stitch_pipeline import _stitch_params

    profile_a = _save_flatfield_profile("pid-a", "Profile A")
    profile_b = _save_flatfield_profile("pid-b", "Profile B")
    input_dir = _catalogue_dir(tmp_path)
    roll_dir = tmp_path / "Roll"
    roll_dir.mkdir()
    manifest = new_roll_manifest(roll_id="rid-1", roll_name="Roll", shots_per_negative=2)
    # An unseeded roll accepts anything; the invariants only bind once a
    # run has established them — so seed the roll exactly as a first run
    # with profile A would have.
    manifest.runs.append(
        RunRecord(run_id="run-1", kind="stitch", status="complete", started_at="t")
    )
    manifest.processing_params = {
        **jsonable_raw_params(),
        "flat_field": flatfield.profile_token(profile_a),
    }
    manifest.stitch_params = _stitch_params()
    write_roll_manifest(roll_dir, manifest)

    # The roll's own profile probes clean...
    run_probe(input_dir, None, 2, roll_dir=roll_dir, flatfield_profile_id=profile_a.profile_id)

    # ...a different one is the same refusal `run` will give, seen before
    # Stitch is ever pressed.
    with pytest.raises(ProbeFailure) as excinfo:
        run_probe(input_dir, None, 2, roll_dir=roll_dir, flatfield_profile_id=profile_b.profile_id)

    assert excinfo.value.code == Code.ROLL_INVARIANT_MISMATCH
