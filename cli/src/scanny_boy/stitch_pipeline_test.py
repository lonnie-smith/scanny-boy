import dataclasses
import math
from pathlib import Path

import numpy as np
import pytest
import tifftools
from tifftools.constants import Tag

from scanny_boy import hashing, registration, stitch_pipeline, work_dir_support
from scanny_boy.apply_metadata import ApplyMetadataFailure, run_apply_metadata
from scanny_boy.cancellation import CancellationToken
from scanny_boy.composite import MEMORY_SAFETY_FACTOR, estimate_peak_bytes
from scanny_boy.events import (
    Code,
    EditRecorded,
    MetadataApplied,
    MetadataSkipped,
    NegativeDone,
    NegativeFailed,
    Progress,
    Stage,
    WarningEvent,
)
from scanny_boy.icc_profile import ProfileKind, profile_record
from scanny_boy.library import repo
from scanny_boy.linear import encode_from_linear
from scanny_boy.manifest import (
    CuratedMetadata,
    GroupRecord,
    Manifest,
    OutputRecord,
    SourceRecord,
    current_scanny_boy_version,
    load_manifest,
    write_manifest,
)
from scanny_boy.normalization import Bounds
from scanny_boy.output_folder import (
    PREPARE_RULES,
    ROLL_RULES,
    OutputFolderError,
    plan_rerun,
)
from scanny_boy.registration import DETECTOR, StitchError, register_pair
from scanny_boy.roll_manifest import (
    CameraColor,
    NegativeRecord,
    RunRecord,
    append_run,
    load_roll_manifest,
    mutate_roll_manifest,
    new_roll_manifest,
    write_roll_manifest,
)
from scanny_boy.roll_manifest_schema_test_support import (
    assert_matches_roll_manifest_schema,
    load_roll_manifest_schema,
)
from scanny_boy.sample_nef_support import (
    FIXTURES_DIR,
    NEGATIVE_1,
    NEGATIVE_2,
    requires_real_samples,
)
from scanny_boy.stitch_pipeline import _seed_camera_color, run_stitch
from scanny_boy.synthetic_scene_support import synthetic_scene
from scanny_boy.tiff_exif import (
    DATE_TIME_ORIGINAL,
    SUBSEC_TIME_ORIGINAL,
)
from scanny_boy.work_dir_support import (
    FILM_DATE,
    FRAME_SIZE,
    attach_base_frame,
    make_out_dir,
    make_roll_dir,
    make_work_dir,
    negative_frames,
    roll_invariants,
    run_stitch_with_defaults,
    write_intermediate,
)


def _work_manifest(**overrides) -> Manifest:
    """A minimal completed work manifest for `_seed_camera_color` tests."""
    defaults = {
        "scanny_boy_version": "0.1.0",
        "run_id": "convert-run",
        "status": "complete",
        "input_folder": "/tmp/in",
        "film_date": FILM_DATE,
        "shots_per_negative": 1,
        "processing_params": {"gamma": [1.8, 16]},
        "icc_profile": {"name": "ScannyBoy-Linear-v1.icc", "sha256": "a" * 64},
        "source_order": ["_DSC4638.NEF"],
        "sources": [
            SourceRecord(
                filename="_DSC4638.NEF",
                absolute_path="/tmp/in/_DSC4638.NEF",
                size=123,
                mtime=1.0,
                sha256="a" * 64,
            )
        ],
        "curated_metadata": CuratedMetadata(
            exposure_time="1/30",
            f_number="8",
            iso=100,
            focal_length="55",
            lens_model="55mm f/2.8",
            orientation=1,
            camera_whitebalance=(1.69, 1.0, 1.38, 1.0),
        ),
        "groups": [],
        "started_at": "2026-08-02T00:00:00Z",
    }
    defaults.update(overrides)
    return Manifest(**defaults)


def _matrix(
    scale: float = 1.0,
) -> tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]:
    return (
        (0.7 * scale, 0.2, 0.1),
        (0.1, 0.75 * scale, 0.15),
        (0.05, 0.1, 0.85 * scale),
    )


def _curated_with_matrix(scale: float = 1.0, camera_model: str | None = "NIKON Z 7"):
    curated = _work_manifest().curated_metadata
    return CuratedMetadata(
        **{
            **curated.to_dict(),
            "camera_whitebalance": curated.camera_whitebalance,
            "rgb_xyz_matrix": _matrix(scale),
            "camera_model": camera_model,
        }
    )


# --- the happy path ------------------------------------------------------


@pytest.mark.slow
def test_end_to_end_on_real_samples(tmp_path):
    """The chunk's headline test. Real Phase 1 intermediates in, one
    stitched TIFF per negative out, named after each group's first frame,
    with a roll manifest that validates against its published schema and
    records a hash matching the file actually on disk."""
    work_dir = make_work_dir(tmp_path, negatives=2)
    out_dir = make_roll_dir(tmp_path)
    events: list = []

    outcome = run_stitch_with_defaults(work_dir, out_dir, events=events)

    assert outcome.status == "complete"
    assert outcome.failed == []
    assert sorted(outcome.published) == ["IMG_00.tif", "IMG_10.tif"]

    produced = sorted(p.name for p in out_dir.iterdir())
    assert produced == ["IMG_00.tif", "IMG_10.tif"]

    manifest = load_roll_manifest(out_dir)
    # Section 3.3: the roll is additive and has no single status; the status
    # belongs to the run that just finished.
    run = manifest.run("stitch-run")
    assert run.status == "complete"
    assert run.kind == "stitch"
    assert run.convert_run_id == "convert-run"
    assert run.short_id == "stitch"
    assert run.work_dir == str(work_dir)
    assert [n.negative_id for n in manifest.negatives] == [
        "stitch-negative-01",
        "stitch-negative-02",
    ]
    # Section 3.3: sources are keyed by hash and carry the run that first
    # contributed them.
    assert {s.run_id for s in manifest.sources} == {"stitch-run"}

    for negative in manifest.negatives:
        assert negative.run_id == "stitch-run"
        # Section 5.4 decision 4: the roll records the capture time the
        # negative's first frame actually carries. The metadata stage's three
        # fields are untouched by a stitch.
        assert negative.capture_time.source_datetime_original is not None
        assert negative.capture_time.intended_datetime_original is None
        assert negative.capture_time.applied_datetime_original is None
        assert negative.capture_time.date_override is None
        assert negative.status == "completed"
        assert negative.output is not None
        # Named after the group's first frame.
        assert negative.expected_output == f"{Path(negative.members[0]).stem}.tif"
        published = out_dir / negative.output["name"]
        assert published.stat().st_size == negative.output["size"]
        assert hashing.sha256_file(published) == negative.output["sha256"]
        assert negative.canvas is not None
        assert negative.output["width"] == negative.canvas[0]
        assert negative.output["height"] == negative.canvas[1]
        assert negative.valid_rect is not None
        # Section 3.12.2: never set in Phase 2.
        assert negative.rebate_deviation_px is None
        assert negative.error_code is None

    assert_matches_roll_manifest_schema(
        manifest.to_dict(),
        load_roll_manifest_schema(),
    )

    done = [e for e in events if isinstance(e, NegativeDone)]
    assert len(done) == 2
    assert not [e for e in events if isinstance(e, NegativeFailed)]

    # No staging directory survives a successful run.
    assert not [p for p in out_dir.iterdir() if p.is_dir()]


# --- the rig-tilt rectification -----------------------------------------


def _tilt_hook(l_x, l_y):
    """Warps every frame through W(l), making the true inter-frame map
    `W⁻¹·S·W` — the capture a tilted rig actually produces."""
    import cv2

    from scanny_boy.registration import Rectification, rectify

    rect = Rectification(
        l=np.array([l_x, l_y]),
        centre=np.array([FRAME_SIZE[1] / 2.0, FRAME_SIZE[0] / 2.0]),
        frame_size=FRAME_SIZE,
        rms_before_px=1.0,
        rms_after_px=0.5,
        relative_improvement=0.5,
        pair_count=2,
    )
    height, width = FRAME_SIZE
    ys, xs = np.mgrid[0:height, 0:width]
    pts = np.stack([xs, ys], axis=-1).reshape(-1, 2).astype(np.float64)
    mapped = rectify(pts, rect).reshape(height, width, 2)

    def hook(pixels: np.ndarray) -> np.ndarray:
        return cv2.remap(
            pixels,
            mapped[..., 0].astype(np.float32),
            mapped[..., 1].astype(np.float32),
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        ).astype(np.uint16)

    return hook, rect


def test_a_tilted_capture_rectifies_end_to_end(tmp_path):
    """The two-pass flow on a genuine run: pass 1 registers the tilted
    captures, the fit accepts, pass 2 re-registers in rectified space, and
    the manifest records the correction."""
    hook, rect = _tilt_hook(1.2e-5, -8e-6)
    work_dir = make_work_dir(tmp_path, frame_hook=hook)
    out_dir = make_roll_dir(tmp_path)

    outcome = run_stitch_with_defaults(work_dir, out_dir)

    assert outcome.status == "complete"
    manifest = load_roll_manifest(out_dir)
    negative = manifest.negatives[0]
    assert negative.rectification is not None
    block = negative.rectification
    assert np.allclose(block["l"], rect.l, rtol=0.25)
    assert block["pair_count"] >= 2
    assert block["relative_improvement"] > 0.15
    assert block["rms_after_px"] < block["rms_before_px"]


def test_a_healthy_capture_stitches_without_a_rectification(work_dir, tmp_path):
    """The additive guarantee: a similarity-consistent synthetic negative
    must not grow a tilt. The fit runs and is rejected by the improvement
    gate; no second pass, no manifest block."""
    out_dir = make_roll_dir(tmp_path)

    outcome = run_stitch_with_defaults(work_dir, out_dir)

    assert outcome.status == "complete"
    manifest = load_roll_manifest(out_dir)
    assert manifest.negatives[0].rectification is None


def test_gain_correction_is_recorded_in_the_roll_manifest(tmp_path):
    """Lamp drift between a negative's frames is reconciled by solved
    per-frame gains, and the manifest records both the gains and the two
    overlap-MAD measurements (pre-gain explains why, post-gain is what the
    gate checks)."""
    work_dir = make_work_dir(
        tmp_path,
        frame_gains=[(1.0, 1.0, 1.0), (0.85, 0.9, 0.95), (1.0, 1.0, 1.0)],
    )
    out_dir = make_roll_dir(tmp_path)

    outcome = run_stitch_with_defaults(work_dir, out_dir)

    assert outcome.status == "complete"
    manifest = load_roll_manifest(out_dir)
    negative = manifest.negatives[0]

    assert all(len(frame.gain) == 3 for frame in negative.frames)
    # The middle frame is darker than its neighbours; its solved gain must
    # sit above 1 in the channels that were scaled down.
    assert all(c > 1.0 for c in negative.frames[1].gain)
    measured = [
        (pair.overlap_mad_pregain, pair.overlap_mad)
        for pair in negative.pairs
        if pair.overlap_mad is not None and pair.overlap_mad_pregain is not None
    ]
    assert measured
    assert all(pregain > post for pregain, post in measured)

    assert_matches_roll_manifest_schema(
        manifest.to_dict(),
        load_roll_manifest_schema(),
    )


def test_calibrated_profile_geometry_reaches_the_composite_warp(
    work_dir, tmp_path, monkeypatch
):
    """A stitch run through a calibrated profile must hand that profile's
    geometry to `composite`, so the warp matches the undistorted coordinates
    the solve happened in. (The profile keyword was never passed at the call
    site, so the geometry-aware warp was dead in production.)"""
    from scanny_boy.calibration import RigProfile
    from scanny_boy.library import repo

    # Zero distortion: the warp is a no-op, so the synthetic scene still
    # stitches normally — only the plumbing, not the correction, is tested.
    frame_height, frame_width = FRAME_SIZE
    geometry = {
        "format_version": 1,
        "frame_width": frame_width,
        "frame_height": frame_height,
        "fx": float(max(frame_width, frame_height)),
        "fy": float(max(frame_width, frame_height)),
        "cx": frame_width / 2.0,
        "cy": frame_height / 2.0,
        "k1": 0.0,
        "k2": 0.0,
    }
    repo.save_rig_profile(
        RigProfile(
            profile_id="pid-geo",
            name="Profile Geo",
            scanny_boy_version="0.3.0",
            created_at="2026-09-01T00:00:00Z",
            geometry=geometry,
        )
    )

    captured = []
    real_composite = stitch_pipeline.composite

    def spy(*args, **kwargs):
        captured.append(kwargs)
        return real_composite(*args, **kwargs)

    monkeypatch.setattr(stitch_pipeline, "composite", spy)

    out_dir = make_roll_dir(tmp_path)

    outcome = run_stitch_with_defaults(work_dir, out_dir, rig_profile_id="pid-geo")

    assert outcome.status == "complete"
    assert captured
    assert captured[0]["geometry"] == geometry
    assert captured[0]["ca"] is None


def test_geometry_frame_size_mismatch_is_rejected(work_dir, tmp_path):
    """A profile fitted at different decode dimensions must fail before
    stitch starts — width and height are not interchangeable."""
    from scanny_boy.calibration import RigProfile
    from scanny_boy.library import repo

    frame_height, frame_width = FRAME_SIZE
    geometry = {
        "format_version": 1,
        "frame_width": frame_height,
        "frame_height": frame_width,
        "fx": float(max(frame_width, frame_height)),
        "fy": float(max(frame_width, frame_height)),
        "cx": frame_height / 2.0,
        "cy": frame_width / 2.0,
        "k1": 0.0,
        "k2": 0.0,
    }
    repo.save_rig_profile(
        RigProfile(
            profile_id="pid-mismatch",
            name="Swapped",
            scanny_boy_version="0.3.0",
            created_at="2026-09-01T00:00:00Z",
            geometry=geometry,
        )
    )

    out_dir = make_roll_dir(tmp_path)

    with pytest.raises(StitchError) as exc_info:
        run_stitch_with_defaults(work_dir, out_dir, rig_profile_id="pid-mismatch")
    assert exc_info.value.code is Code.GEOMETRY_FRAME_SIZE_MISMATCH


def test_gain_drift_warning_fires_when_solved_gains_leave_unity(tmp_path, monkeypatch):
    """A solved gain far from unity means something is wrong with the
    capture: warn, by the same pattern as STITCH_SCALE_DRIFT."""
    monkeypatch.setattr(stitch_pipeline, "GAIN_DRIFT_WARN", 1e-6)
    work_dir = make_work_dir(
        tmp_path,
        frame_gains=[(1.0, 1.0, 1.0), (0.85, 0.9, 0.95), (1.0, 1.0, 1.0)],
    )
    out_dir = make_roll_dir(tmp_path)
    events: list = []

    outcome = run_stitch_with_defaults(work_dir, out_dir, events=events)

    assert outcome.status == "complete"
    drift_warnings = [
        event
        for event in events
        if isinstance(event, WarningEvent) and event.code is Code.STITCH_GAIN_DRIFT
    ]
    assert drift_warnings
    assert any("IMG_01.tif" in event.message for event in drift_warnings)


def test_progress_events_carry_the_stitch_stage(work_dir, tmp_path):
    out_dir = make_roll_dir(tmp_path)
    events: list = []

    run_stitch_with_defaults(work_dir, out_dir, events=events)

    progress = [e for e in events if isinstance(e, Progress)]
    assert progress
    assert all(e.stage is Stage.STITCH for e in progress)
    completed = [e.completed for e in progress]
    assert completed == sorted(completed)
    assert progress[-1].completed <= progress[-1].total


# --- work-manifest gating ------------------------------------------------


def test_running_work_manifest_is_rejected(tmp_path):
    work_dir = make_work_dir(tmp_path, status="running")
    out_dir = make_roll_dir(tmp_path)

    with pytest.raises(StitchError) as exc_info:
        run_stitch_with_defaults(work_dir, out_dir)
    assert exc_info.value.code is Code.WORK_MANIFEST_UNUSABLE


def test_partial_work_manifest_needs_allow_partial(tmp_path):
    work_dir = make_work_dir(
        tmp_path, negatives=2, status="partial", group_statuses=["completed", "failed"]
    )
    out_dir = make_roll_dir(tmp_path)

    with pytest.raises(StitchError) as exc_info:
        run_stitch_with_defaults(work_dir, out_dir)
    assert exc_info.value.code is Code.WORK_MANIFEST_UNUSABLE


def test_partial_work_manifest_stitches_completed_groups_only(tmp_path):
    work_dir = make_work_dir(
        tmp_path, negatives=2, status="partial", group_statuses=["completed", "failed"]
    )
    out_dir = make_roll_dir(tmp_path)

    outcome = run_stitch_with_defaults(work_dir, out_dir, allow_partial=True)

    assert outcome.status == "complete"
    assert outcome.published == ["IMG_00.tif"]
    manifest = load_roll_manifest(out_dir)
    assert [n.negative_id for n in manifest.negatives] == ["stitch-negative-01"]


def test_cancelled_work_manifest_is_rejected(tmp_path):
    work_dir = make_work_dir(tmp_path, status="cancelled")
    out_dir = make_roll_dir(tmp_path)

    with pytest.raises(StitchError) as exc_info:
        run_stitch_with_defaults(work_dir, out_dir)
    assert exc_info.value.code is Code.WORK_MANIFEST_UNUSABLE


# --- intermediate verification -------------------------------------------


def test_missing_intermediate_is_caught(work_dir, tmp_path):
    out_dir = make_roll_dir(tmp_path)
    (work_dir / "IMG_01.tif").unlink()

    with pytest.raises(StitchError) as exc_info:
        run_stitch_with_defaults(work_dir, out_dir)
    assert exc_info.value.code is Code.INTERMEDIATE_MISSING
    assert "IMG_01.tif" in exc_info.value.message


def test_changed_intermediate_is_caught(work_dir, tmp_path):
    out_dir = make_roll_dir(tmp_path)

    # Same byte count, different content: only the SHA-256 can catch this,
    # which is why the verification step requires both checks and not just
    # the size.
    target = work_dir / "IMG_01.tif"
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))

    with pytest.raises(StitchError) as exc_info:
        run_stitch_with_defaults(work_dir, out_dir)
    assert exc_info.value.code is Code.INTERMEDIATE_CHANGED


# --- failure, cancellation, and the output folder ------------------------


@pytest.mark.slow
def test_failing_negative_does_not_stop_the_run(tmp_path):
    """Section 3.5: a negative that cannot be stitched fails alone, the run
    continues, and the run ends `partial`."""
    good = make_work_dir(tmp_path, negatives=1)
    # Add a second negative whose three frames share no content at all, so
    # its pair graph is disconnected and it must fail.
    bad_frames = negative_frames(overlapping=False, seed=5)
    manifest = load_manifest(good)
    outputs = []
    members = []
    for i, pixels in enumerate(bad_frames):
        name = f"IMG_9{i}.tif"
        write_intermediate(good / name, pixels, f"IMG_9{i}.NEF")
        path = good / name
        outputs.append(
            OutputRecord(
                name=name, size=path.stat().st_size, sha256=hashing.sha256_file(path)
            )
        )
        members.append(f"IMG_9{i}.NEF")
    manifest.groups.append(
        GroupRecord(
            group_id="negative-99",
            members=members,
            expected_outputs=[f"{Path(m).stem}.tif" for m in members],
            status="completed",
            outputs=outputs,
        )
    )
    manifest.sources.extend(
        SourceRecord(
            filename=m,
            absolute_path=f"/tmp/in/{m}",
            size=2000 + i,
            mtime=1.0,
            sha256=f"9{i}".ljust(64, "d"),
        )
        for i, m in enumerate(members)
    )
    manifest.source_order.extend(members)
    write_manifest(good, manifest)

    out_dir = make_roll_dir(tmp_path)
    events: list = []
    outcome = run_stitch_with_defaults(good, out_dir, events=events)

    assert outcome.status == "partial"
    assert outcome.published == ["IMG_00.tif"]
    assert outcome.failed == ["negative-99"]

    # The good negative is published; the failed one left nothing behind.
    assert (out_dir / "IMG_00.tif").exists()
    assert not (out_dir / "IMG_90.tif").exists()
    assert not [p for p in out_dir.iterdir() if p.is_dir()]

    failures = [e for e in events if isinstance(e, NegativeFailed)]
    # The event carries the roll's `negative_id`, not the work manifest's
    # group id, which is what `outcome.failed` still reports.
    assert [e.negative_id for e in failures] == ["stitch-negative-02"]
    assert failures[0].code is Code.STITCH_UNDERCONSTRAINED

    # The recorded message is the friendly, user-facing wording that names
    # the negative and its source files. `CONTRACT.md` is explicit that
    # message text is not the machine interface (`code` is), so this is the
    # one place wording can be reworded without breaking the app.
    expected_message = (
        "Could not find a stitching solution for stitch-negative-02 "
        "(IMG_90.NEF, IMG_91.NEF, IMG_92.NEF)"
    )
    assert failures[0].message == expected_message

    roll = load_roll_manifest(out_dir)
    assert roll.run("stitch-run").status == "partial"
    assert roll.negative("stitch-negative-01").status == "completed"
    failed_record = roll.negative("stitch-negative-02")
    assert failed_record.status == "failed"
    assert failed_record.error_code == Code.STITCH_UNDERCONSTRAINED.value
    assert failed_record.error_message == expected_message
    assert failed_record.output is None

    # Section 3.4 asks for every per-pair metric to be recorded. A failed
    # negative's pairs are exactly what shows *why* it failed, so they are
    # written even though no layout was ever solved.
    assert len(failed_record.pairs) == 3
    assert all(not p.accepted for p in failed_record.pairs)
    assert all(p.inliers < 40 for p in failed_record.pairs)
    assert failed_record.frames == []
    assert failed_record.canvas is None


# --- the CLAHE fallback ---------------------------------------------------


def test_featureless_negative_fails_with_a_retry_eligible_code(tmp_path, monkeypatch):
    """Blank intermediates — a blank or near-black scan is an ordinary
    outcome, not an internal error — must fail the negative with a stable,
    CLAHE-retry-eligible code, not an AttributeError from inside the
    matcher."""
    from scanny_boy.linear import encode_from_linear as _encode

    def blank_frames(*, overlapping, seed, count=3, frame_gains=None):
        blank = np.full(FRAME_SIZE, 0.2, dtype=np.float32)
        return [
            _encode(np.stack([blank] * 3, axis=-1).astype(np.float32))
            for _ in range(count)
        ]

    # This is the one caller that cannot take the `work_dir` fixture: it needs
    # the frames the patch produces, and the fixture's template was built once
    # per session, long before the patch. Build a work directory here, after
    # it — patching the name where `make_work_dir` looks it up, in the support
    # module's own globals rather than in this one's.
    monkeypatch.setattr(work_dir_support, "negative_frames", blank_frames)
    work_dir = make_work_dir(tmp_path)
    out_dir = make_roll_dir(tmp_path)

    events: list = []
    outcome = run_stitch_with_defaults(work_dir, out_dir, events=events)

    assert outcome.status == "partial"
    failures = [e for e in events if isinstance(e, NegativeFailed)]
    assert failures
    assert failures[0].code in stitch_pipeline._CLAHE_RETRY_CODES


def test_clahe_fallback_recovers_an_underconstrained_negative(
    work_dir, tmp_path, monkeypatch
):
    """A negative whose plain-pass registration disconnects the pair graph
    is retried once with CLAHE; a graph that connects on that pass still
    stitches, and the manifest says the fallback was needed."""
    out_dir = make_roll_dir(tmp_path)

    clahe_by_call: list[bool] = []
    real_detect_all = stitch_pipeline._detect_all

    def fake_detect_all(paths, workers, cancel, *, use_clahe):
        clahe_by_call.append(use_clahe)
        return real_detect_all(paths, workers, cancel, use_clahe=use_clahe)

    def fake_register_pair(a, b, undistorter=None):
        result = register_pair(a, b)
        if not clahe_by_call[-1]:
            # Force the plain pass to look disconnected regardless of what
            # the synthetic frames actually matched, so the retry is
            # exercised without needing frames tuned to fail only without
            # CLAHE.
            return dataclasses.replace(
                result, accepted=False, reject_code=Code.STITCH_INSUFFICIENT_MATCHES
            )
        return result

    monkeypatch.setattr(stitch_pipeline, "_detect_all", fake_detect_all)
    monkeypatch.setattr(stitch_pipeline, "register_pair", fake_register_pair)

    events: list = []
    outcome = run_stitch_with_defaults(work_dir, out_dir, events=events)

    assert outcome.status == "complete"
    assert clahe_by_call == [False, True]

    fallback_warnings = [
        e
        for e in events
        if isinstance(e, WarningEvent) and e.code is Code.STITCH_CLAHE_FALLBACK_USED
    ]
    assert len(fallback_warnings) == 1
    assert "STITCH_UNDERCONSTRAINED" in fallback_warnings[0].message

    roll = load_roll_manifest(out_dir)
    negative = roll.negative("stitch-negative-01")
    assert negative.status == "completed"
    assert negative.used_clahe_fallback is True

    # The retry spends no further progress budget: `completed` never passes
    # the `total` declared before any negative was solved.
    progress_events = [e for e in events if isinstance(e, Progress)]
    total = progress_events[0].total
    assert all(e.total == total for e in progress_events)
    assert max(e.completed for e in progress_events) <= total


def test_clahe_fallback_is_not_used_for_an_oversized_canvas(
    work_dir, tmp_path, monkeypatch
):
    """`STITCH_OUTPUT_TOO_LARGE` is not in `_CLAHE_RETRY_CODES`: a canvas
    that is already too big to write stays too big under CLAHE too, so the
    negative fails on the first pass with no retry."""
    out_dir = make_roll_dir(tmp_path)

    clahe_by_call: list[bool] = []
    real_detect_all = stitch_pipeline._detect_all

    def fake_detect_all(paths, workers, cancel, *, use_clahe):
        clahe_by_call.append(use_clahe)
        return real_detect_all(paths, workers, cancel, use_clahe=use_clahe)

    def fake_check_output_size(canvas_size, *, on_warning):
        raise StitchError(Code.STITCH_OUTPUT_TOO_LARGE, "too large for this test")

    monkeypatch.setattr(stitch_pipeline, "_detect_all", fake_detect_all)
    monkeypatch.setattr(stitch_pipeline, "check_output_size", fake_check_output_size)

    events: list = []
    outcome = run_stitch_with_defaults(work_dir, out_dir, events=events)

    assert outcome.status == "partial"
    assert clahe_by_call == [False]

    failures = [e for e in events if isinstance(e, NegativeFailed)]
    assert failures[0].code is Code.STITCH_OUTPUT_TOO_LARGE

    fallback_warnings = [
        e
        for e in events
        if isinstance(e, WarningEvent) and e.code is Code.STITCH_CLAHE_FALLBACK_USED
    ]
    assert fallback_warnings == []

    roll = load_roll_manifest(out_dir)
    assert roll.negative("stitch-negative-01").used_clahe_fallback is False


@requires_real_samples
@pytest.mark.slow
def test_real_underconstrained_negative_recovers_with_clahe(tmp_path):
    """`NEGATIVE_2` (`_DSC4644/45/46.NEF`) is a real low-texture scan: its
    plain-pass registration leaves the pair graph disconnected
    (`STITCH_UNDERCONSTRAINED`), and only the CLAHE retry finds enough
    correspondences to connect it. Regression test for the bug that
    motivated the fallback."""
    from scanny_boy.pipeline import run_convert

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for name in NEGATIVE_2:
        (input_dir / name).write_bytes((FIXTURES_DIR / name).read_bytes())

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    run_convert(
        input_dir,
        NEGATIVE_2,
        work_dir,
        3,
        run_id="convert-run",
        jobs=1,
        cancel=CancellationToken(),
        emit=lambda event: None,
    )

    out_dir = make_roll_dir(tmp_path)
    events = []
    outcome = run_stitch_with_defaults(work_dir, out_dir, events=events)

    assert outcome.status == "complete"

    fallback_warnings = [
        e
        for e in events
        if isinstance(e, WarningEvent) and e.code is Code.STITCH_CLAHE_FALLBACK_USED
    ]
    assert len(fallback_warnings) == 1

    roll = load_roll_manifest(out_dir)
    negative = roll.negatives[0]
    assert negative.status == "completed"
    assert negative.used_clahe_fallback is True


@requires_real_samples
@pytest.mark.slow
def test_real_auto_crop_lies_inside_the_recorded_film_extent(tmp_path):
    """A real roll's seeded crop stays inside the film-extent inset the
    stitch recorded (the carrier the metering already withheld) — or the
    detector refused and said why. Never a crop that includes carrier."""
    import cv2

    from scanny_boy.normalization import analysis_grid_block_sizes
    from scanny_boy.pipeline import run_convert

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for name in NEGATIVE_1:
        (input_dir / name).write_bytes((FIXTURES_DIR / name).read_bytes())
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    run_convert(
        input_dir,
        NEGATIVE_1,
        work_dir,
        3,
        run_id="convert-run",
        jobs=1,
        cancel=CancellationToken(),
        emit=lambda event: None,
    )
    out_dir = make_roll_dir(tmp_path)
    _tick_auto_crop(out_dir, "35mm")

    outcome = run_stitch_with_defaults(work_dir, out_dir)

    assert outcome.status == "complete"
    negative = load_roll_manifest(out_dir).negatives[0]
    assert negative.auto_crop is not None
    ops = _crop_ops(out_dir, negative.negative_id)
    if not ops:
        assert negative.auto_crop["result"] == "refused"
        assert negative.auto_crop["reason"]
        return
    params = ops[-1]["params"]
    extent = negative.normalization["film_extent"]
    if not extent["detected"]:
        return
    block = analysis_grid_block_sizes(
        (negative.output["height"], negative.output["width"])
    )[0]
    x, y, w, h = negative.normalization["analysis_rect"]
    top, bottom, left, right = (v * block for v in extent["insets"])
    inner = (x + left, y + top, x + w - right, y + h - bottom)
    centre = (params["x"] + params["w"] / 2, params["y"] + params["h"] / 2)
    matrix = cv2.getRotationMatrix2D(centre, params["tilt_deg"], 1.0)
    for cx, cy in (
        (params["x"], params["y"]),
        (params["x"] + params["w"], params["y"]),
        (params["x"] + params["w"], params["y"] + params["h"]),
        (params["x"], params["y"] + params["h"]),
    ):
        px = matrix[0, 0] * cx + matrix[0, 1] * cy + matrix[0, 2]
        py = matrix[1, 0] * cx + matrix[1, 1] * cy + matrix[1, 2]
        assert inner[0] - 2 <= px <= inner[2] + 2
        assert inner[1] - 2 <= py <= inner[3] + 2


def test_cancellation_keeps_completed_negatives(tmp_path):
    """Section 3.5: a cancelled negative is abandoned, not failed — no
    `negative_failed` event, and the manifest ends `cancelled`."""
    work_dir = make_work_dir(tmp_path, negatives=2)
    out_dir = make_roll_dir(tmp_path)
    cancel = CancellationToken()
    events: list = []

    def emit(event) -> None:
        events.append(event)
        # Cancel the moment the first negative is published, so the second
        # is abandoned mid-run.
        if isinstance(event, NegativeDone):
            cancel.cancel()

    outcome = run_stitch(
        work_dir,
        out_dir,
        run_id="stitch-run",
        overwrite=False,
        allow_partial=False,
        jobs=1,
        cancel=cancel,
        emit=emit,
    )

    assert outcome.status == "cancelled"
    assert outcome.published == ["IMG_00.tif"]
    assert outcome.failed == []

    # The completed negative survives; the abandoned one is not recorded
    # as failed and leaves no staging directory.
    assert (out_dir / "IMG_00.tif").exists()
    assert not (out_dir / "IMG_10.tif").exists()
    assert not [e for e in events if isinstance(e, NegativeFailed)]
    assert not [p for p in out_dir.iterdir() if p.is_dir()]

    roll = load_roll_manifest(out_dir)
    assert roll.run("stitch-run").status == "cancelled"
    assert roll.negative("stitch-negative-01").status == "completed"
    assert roll.negative("stitch-negative-02").status == "pending"


def test_work_equal_to_out_is_rejected(work_dir):

    with pytest.raises(StitchError) as exc_info:
        run_stitch_with_defaults(work_dir, work_dir)
    assert exc_info.value.code is Code.WORK_SAME_AS_OUTPUT


def test_unrelated_nonempty_output_folder_is_rejected(work_dir, tmp_path):
    out_dir = make_roll_dir(tmp_path)
    (out_dir / "holiday-snap.jpg").write_bytes(b"not ours")

    with pytest.raises(StitchError) as exc_info:
        run_stitch_with_defaults(work_dir, out_dir)
    assert exc_info.value.code is Code.OUTPUT_NOT_EMPTY


def test_stitch_without_a_registered_roll_is_rejected(work_dir, tmp_path):
    """Section 5.4 decision 1: `stitch` never creates a roll. An empty
    directory is not one."""
    out_dir = make_out_dir(tmp_path)

    with pytest.raises(StitchError) as exc_info:
        run_stitch_with_defaults(work_dir, out_dir)
    assert exc_info.value.code is Code.ROLL_NOT_FOUND
    assert "registered roll" in exc_info.value.message
    assert not [p for p in out_dir.iterdir()]


# --- the film-base state machine ------------------------------------------


def _baseless_roll(tmp_path: Path, name: str = "out") -> Path:
    """A real, registered roll with NO film-base reference: §3.2 rule 4's
    ABSENT state."""
    out = make_out_dir(tmp_path, name)
    write_roll_manifest(
        out, new_roll_manifest(roll_id="r-baseless", roll_name=name, film_kind="colour")
    )
    return out


def test_stitch_without_a_base_frame_is_rejected_before_any_pixel_work(
    work_dir,
    tmp_path,
):
    """§3.2 rule 4: run/stitch on an ABSENT roll fail FILM_BASE_REQUIRED
    after the roll manifest loads and its invariants are checked, and
    before any pixel work — asserted here on the absence of progress
    events, not just the code."""
    out_dir = _baseless_roll(tmp_path)
    events: list = []

    with pytest.raises(StitchError) as exc_info:
        run_stitch_with_defaults(work_dir, out_dir, events=events)

    assert exc_info.value.code is Code.FILM_BASE_REQUIRED
    assert "film-base reference" in exc_info.value.message
    assert not [e for e in events if isinstance(e, Progress)]
    assert load_roll_manifest(out_dir).film_base is None


def test_stitch_on_a_version_7_roll_is_rejected(work_dir, tmp_path, monkeypatch):
    """§9: a roll stitched before film-base anchoring cannot take new
    negatives. The library database does not persist
    `manifest_format_version` — every roll it produces reads back at the
    current version — so the v7 manifest is staged through the loader the
    run-time path actually calls (`plan_rerun` reads through
    `repo.load_roll`)."""
    out_dir = make_roll_dir(tmp_path)
    v7_manifest = load_roll_manifest(out_dir)
    v7_manifest.manifest_format_version = 7
    monkeypatch.setattr("scanny_boy.library.repo.load_roll", lambda _dir: v7_manifest)

    with pytest.raises(StitchError) as exc_info:
        run_stitch_with_defaults(work_dir, out_dir)

    assert exc_info.value.code is Code.ROLL_PREDATES_FILM_BASE


def test_a_successful_run_locks_the_base_frame(work_dir, tmp_path):
    """§3.2 rule 5: the roll's first published negative sets `locked_at`,
    in the same manifest write."""
    out_dir = make_roll_dir(tmp_path)

    assert run_stitch_with_defaults(work_dir, out_dir).status == "complete"

    roll = load_roll_manifest(out_dir)
    assert roll.film_base is not None
    assert roll.film_base["locked_at"] is not None


def test_a_run_that_fails_before_publishing_leaves_the_roll_attached(tmp_path):
    """§3.2 rule 7: the lock is set alongside the first published negative.
    Nothing publishes, so the roll stays ATTACHED."""
    work_dir = make_work_dir(
        tmp_path, negatives=2, overlapping=False, shots_per_negative=1
    )
    out_dir = make_roll_dir(tmp_path)

    outcome = run_stitch_with_defaults(work_dir, out_dir)

    assert outcome.status == "partial"
    assert outcome.published == []
    roll = load_roll_manifest(out_dir)
    assert roll.film_base is not None
    assert roll.film_base["locked_at"] is None


def test_a_differing_base_frame_camera_warns_once_the_roll_has_one(work_dir, tmp_path):
    """§3.3: the camera comparison's second home — `roll set-base-frame`
    found no `camera_color` on the fresh roll, so the first run (which
    seeds it) compares instead."""
    out_dir = make_roll_dir(tmp_path)
    roll = load_roll_manifest(out_dir)
    attach_base_frame(roll, camera_model="NIKON Z f")
    roll.camera_color = CameraColor(
        rgb_xyz_matrix=((0.7, 0.2, 0.1), (0.1, 0.75, 0.15), (0.05, 0.1, 0.85)),
        source="libraw",
        camera_model="NIKON Z 7",
    )
    write_roll_manifest(out_dir, roll)
    events: list = []

    assert (
        run_stitch_with_defaults(work_dir, out_dir, events=events).status == "complete"
    )

    warnings = [
        e
        for e in events
        if isinstance(e, WarningEvent)
        and e.code
        not in (
            Code.NORMALIZE_HEADROOM_CLIPPED,
            # The synthetic scene's blurred dark content forms a second dense
            # mode, so the film-extent pass reports an informational
            # withhold on it; it is not the warning this test is about.
            Code.NORMALIZE_FILM_EXTENT_WITHHELD,
            Code.NORMALIZE_FILM_EXTENT_EXCESSIVE,
        )
    ]
    assert [w.code for w in warnings] == [Code.FILM_BASE_CAMERA_CONFLICT]
    assert "NIKON Z f" in warnings[0].message
    assert "NIKON Z 7" in warnings[0].message


# --- the drift evidence ---------------------------------------------------


def _forced_rebate(base_density, *, clipped: bool = False):
    """A `detect_rebate` stand-in that fires on a thin band along one edge
    of the analysis region, with a known `base_density`. The synthetic
    scenes carry no rebate of their own (the real detector declines on
    them), so the recorded-arithmetic tests need one that always does."""
    from scanny_boy.normalization import Rebate

    def detect(grid_log, keep):
        band = keep & (np.arange(keep.shape[0])[:, None] >= keep.shape[0] - 8)
        kept = max(int(np.count_nonzero(keep)), 1)
        rebate = Rebate(
            detected=True,
            mask_fraction=float(np.count_nonzero(band)) / kept,
            base_density=None if clipped else base_density,
            clipped=clipped,
        )
        return keep & ~band, rebate

    return detect


def test_base_check_is_recorded_when_both_inputs_exist(work_dir, tmp_path, monkeypatch):
    """§6: present, with the level_offset/shape_residual arithmetic, when
    the roll has a locked anchor and the negative's rebate fired unclipped.
    The anchor is (-0.42, -0.12, -0.99); the forced rebate reads
    (-0.40, -0.10, -0.95): level_offset = median diff = +0.02, and the
    residuals agree per channel except blue's 0.02, so shape_residual
    = 0.02."""
    monkeypatch.setattr(
        "scanny_boy.composite.detect_rebate",
        _forced_rebate((-0.40, -0.10, -0.95)),
    )
    out_dir = make_roll_dir(tmp_path)
    roll = load_roll_manifest(out_dir)
    attach_base_frame(roll, locked_at="2026-09-06T19:00:00Z")
    write_roll_manifest(out_dir, roll)

    assert run_stitch_with_defaults(work_dir, out_dir).status == "complete"

    manifest = load_roll_manifest(out_dir)
    record = manifest.negatives[0].normalization
    assert record["base_check"]["level_offset"] == pytest.approx(0.02, abs=1e-9)
    assert record["base_check"]["shape_residual"] == pytest.approx(0.02, abs=1e-9)
    assert_matches_roll_manifest_schema(manifest.to_dict(), load_roll_manifest_schema())


def test_base_check_is_absent_without_a_locked_anchor(work_dir, tmp_path, monkeypatch):
    """§6: a roll still ATTACHED at composite time (the first negative of
    its first run) records no base_check even when the rebate fired."""
    monkeypatch.setattr(
        "scanny_boy.composite.detect_rebate",
        _forced_rebate((-0.40, -0.10, -0.95)),
    )
    out_dir = make_roll_dir(tmp_path)

    assert run_stitch_with_defaults(work_dir, out_dir).status == "complete"

    record = load_roll_manifest(out_dir).negatives[0].normalization
    assert "base_check" not in record


def test_base_check_is_absent_when_the_rebate_did_not_fire(work_dir, tmp_path):
    """§6: the synthetic scenes carry no rebate, so detect_rebate declines
    and the comparison is absent even with a locked anchor."""
    out_dir = make_roll_dir(tmp_path)
    roll = load_roll_manifest(out_dir)
    attach_base_frame(roll, locked_at="2026-09-06T19:00:00Z")
    write_roll_manifest(out_dir, roll)

    assert run_stitch_with_defaults(work_dir, out_dir).status == "complete"

    record = load_roll_manifest(out_dir).negatives[0].normalization
    assert record["rebate"]["detected"] is False
    assert "base_check" not in record


def test_base_check_is_absent_when_the_rebate_was_clipped(
    work_dir, tmp_path, monkeypatch
):
    """§6: clipped base is worthless base — `base_density` is None and the
    comparison is absent."""
    monkeypatch.setattr(
        "scanny_boy.composite.detect_rebate",
        _forced_rebate((0.0, 0.0, 0.0), clipped=True),
    )
    out_dir = make_roll_dir(tmp_path)
    roll = load_roll_manifest(out_dir)
    attach_base_frame(roll, locked_at="2026-09-06T19:00:00Z")
    write_roll_manifest(out_dir, roll)

    assert run_stitch_with_defaults(work_dir, out_dir).status == "complete"

    record = load_roll_manifest(out_dir).negatives[0].normalization
    assert record["rebate"]["detected"] is True
    assert record["rebate"]["clipped"] is True
    assert "base_check" not in record


# --- the two new meters -----------------------------------------------------


def test_normalization_record_carries_the_highlight_refs_and_residual(
    work_dir, tmp_path
):
    """A real stitch writes both keys — `highlight_refs` (3-wide or null)
    and `neutral_residual` (2-wide or null) — and the manifest validates
    against the updated schema."""
    out_dir = make_roll_dir(tmp_path)

    assert run_stitch_with_defaults(work_dir, out_dir).status == "complete"

    manifest = load_roll_manifest(out_dir)
    record = manifest.negatives[0].normalization
    assert record["highlight_refs"] is None or len(record["highlight_refs"]) == 3
    assert record["neutral_residual"] is None or len(record["neutral_residual"]) == 2
    assert_matches_roll_manifest_schema(manifest.to_dict(), load_roll_manifest_schema())


# --- docs/ROLL_HIGHLIGHT_LOCK.md ---------------------------------------------


def test_roll_highlight_lock_is_recomputed_from_the_roll_at_the_end_of_every_run(
    work_dir, tmp_path, monkeypatch
):
    """`run_stitch` must recompute `roll.highlight_lock` from the roll's
    current negative set on every run — never merge an old value forward —
    and the value it writes must be exactly what
    `highlight_lock.compute_roll_highlight_lock` would say about the same
    roll. Stubbed rather than driven off the synthetic scene's actual
    chroma content: whether that content happens to qualify for a
    trustworthy `highlight_refs` measurement is `_same_pixel_color_floor_
    refs`'s business, not this test's, and a flaky pass/fail on real pixel
    content would test the wrong thing."""
    from scanny_boy import highlight_lock

    base = (-0.42, -0.12, -0.99)  # work_dir_support.base_frame_block's density
    calls: list[int] = []
    first_lock = highlight_lock.HighlightLock(
        k=(1.5, 1.0, 0.5), base=base, qualifying_count=1
    )
    second_lock = highlight_lock.HighlightLock(
        k=(2.5, 1.0, -0.5), base=base, qualifying_count=2
    )
    stubbed = iter([first_lock, second_lock])

    def _stub(roll):
        calls.append(len(roll.negatives))
        return next(stubbed)

    monkeypatch.setattr(
        stitch_pipeline.highlight_lock, "compute_roll_highlight_lock", _stub
    )

    out_dir = make_roll_dir(tmp_path)
    assert run_stitch_with_defaults(work_dir, out_dir).status == "complete"
    manifest = load_roll_manifest(out_dir)
    assert manifest.highlight_lock == first_lock.to_dict()
    assert calls == [1]  # the one negative `work_dir` seeds

    # A second run over the same single-negative work dir re-adopts the
    # existing negative (no new one), but the estimate is still
    # recomputed wholesale — never left stale, and never merged with the
    # previous value.
    assert (
        run_stitch_with_defaults(work_dir, out_dir, run_id="stitch-run-2").status
        == "complete"
    )
    manifest = load_roll_manifest(out_dir)
    assert manifest.highlight_lock == second_lock.to_dict()
    assert calls == [1, 1]


def test_roll_highlight_lock_matches_recomputing_it_fresh(work_dir, tmp_path):
    """Whatever the synthetic scene's own chroma gate decided this run
    (`highlight_refs` null or not), the stored `roll.highlight_lock` must
    agree with calling `compute_roll_highlight_lock` (real, unstubbed)
    fresh on the same loaded roll — `run_stitch` never writes anything
    `compute_roll_highlight_lock` itself would not produce."""
    out_dir = make_roll_dir(tmp_path)
    assert run_stitch_with_defaults(work_dir, out_dir).status == "complete"
    manifest = load_roll_manifest(out_dir)
    from scanny_boy.highlight_lock import compute_roll_highlight_lock

    recomputed = compute_roll_highlight_lock(manifest)
    expected = None if recomputed is None else recomputed.to_dict()
    assert manifest.highlight_lock == expected


def test_real_stitch_with_a_qualifying_highlight_produces_a_non_none_lock(
    work_dir, tmp_path, monkeypatch
):
    """The unstubbed end-to-end case: a real `run_stitch`, over a roll with
    a real locked film base, whose negative's `highlight_refs` comes back
    non-null, must produce a non-`None` `roll.highlight_lock` — with
    `highlight_lock.compute_roll_highlight_lock` running for real (nothing
    in this module is stubbed).

    The synthetic scene's own content does not reliably drive this: its
    gray fill is neutral pixel-by-pixel before `frame_gains`, but the
    stitched result's own dense-end colour still does not pass
    `_same_pixel_color_floor_refs`'s gate as built by the ordinary test
    fixtures (confirmed empirically — a work dir with no hook, and one
    whose frames were overwritten with a large uniform bright neutral
    block, both still recorded `highlight_refs: null`). Rather than fight
    the synthetic-scene generator to manufacture qualifying chroma
    content — a `_same_pixel_color_floor_refs` calibration problem, not
    this feature's — the one lower-level measurement
    (`composite.measure_highlight_refs`) is monkeypatched to return a
    fixed, realistic reading for the one negative `work_dir` seeds; the
    stitch run, the manifest write, and `compute_roll_highlight_lock`
    itself all run unstubbed and for real."""
    import scanny_boy.composite as composite_module

    fixed_refs = (
        -2.12,
        -2.00,
        -2.88,
    )  # base (-0.42, -0.12, -0.99) + (-1.7, -1.88, -1.89)
    monkeypatch.setattr(
        composite_module,
        "measure_highlight_refs",
        lambda grid, keep, base_refs: fixed_refs,
    )

    out_dir = make_roll_dir(tmp_path)
    assert run_stitch_with_defaults(work_dir, out_dir).status == "complete"

    manifest = load_roll_manifest(out_dir)
    record = manifest.negatives[0].normalization
    assert record["highlight_refs"] == list(fixed_refs)
    assert manifest.highlight_lock is not None
    assert manifest.highlight_lock["qualifying_count"] == 1

    from scanny_boy.highlight_lock import compute_roll_highlight_lock

    recomputed = compute_roll_highlight_lock(manifest)
    assert recomputed is not None
    assert manifest.highlight_lock == recomputed.to_dict()


@pytest.mark.slow
def test_changed_shots_per_negative_is_accepted(work_dir, tmp_path):
    """`shots_per_negative` is each batch's own choice, never the roll's: a
    work directory stitched at 2 scans per negative publishes into a roll
    whose earlier batches stitched at 3, with no invariant complaint."""
    out_dir = make_roll_dir(tmp_path)
    assert run_stitch_with_defaults(work_dir, out_dir).status == "complete"

    (tmp_path / "second").mkdir()
    other = make_work_dir(tmp_path / "second", shots_per_negative=2)

    assert (
        run_stitch_with_defaults(other, out_dir, run_id="stitch-run-2").status
        == "complete"
    )

    roll = load_roll_manifest(out_dir)
    assert [r.run_id for r in roll.runs] == ["stitch-run", "stitch-run-2"]


def _negative_with_normalization(negative_id: str, run_id: str, floors, ceils):
    return NegativeRecord(
        negative_id=negative_id,
        run_id=run_id,
        members=[f"{negative_id}-f0.NEF"],
        expected_output=f"{negative_id}.tif",
        fill_color=(0, 0, 0),
        status="completed",
        normalization=None
        if floors is None
        else {
            "floors": list(floors),
            "ceils": list(ceils),
            "source": "per-negative",
        },
    )


def test_reference_bounds_collects_completed_negatives_blocks():
    """The clamp's reference population: every completed negative's
    normalization block, in manifest order — prior runs' negatives first,
    then this run's publishes as they land. Blocks without bounds (a
    pre-normalization record) are skipped, and an empty roll yields an
    empty population, which clamps nothing."""
    roll = new_roll_manifest(roll_id="r", roll_name="r", film_kind="colour")
    assert stitch_pipeline._reference_bounds(roll) == []

    roll.negatives.append(
        _negative_with_normalization(
            "neg-01", "run-1", (-1.5, -1.6, -1.7), (-0.4, -0.3, -0.5)
        )
    )
    roll.negatives.append(
        _negative_with_normalization(
            "neg-02", "run-1", (-1.6, -1.5, -1.8), (-0.35, -0.32, -0.48)
        )
    )
    roll.negatives.append(_negative_with_normalization("neg-03", "run-1", None, None))
    references = stitch_pipeline._reference_bounds(roll)
    assert len(references) == 2
    assert references[0].floors == (-1.5, -1.6, -1.7)
    assert references[1].ceils == (-0.35, -0.32, -0.48)
    assert all(isinstance(b, Bounds) for b in references)


@pytest.mark.slow
def test_anchor_consumption_changes_only_ceils_deviations(
    work_dir, tmp_path, monkeypatch
):
    """REBATE_ANCHORING B-5: stitching with the roll anchor differs from
    the same roll with consumption disabled, and only in the per-channel
    `ceils` deviations — floors and the ceils level stay put."""
    (tmp_path / "with").mkdir()
    (tmp_path / "without").mkdir()
    out_with = make_roll_dir(tmp_path / "with", "with")
    roll = load_roll_manifest(out_with)
    attach_base_frame(roll, density=[-0.50, -0.20, -0.90])
    write_roll_manifest(out_with, roll)

    assert run_stitch_with_defaults(work_dir, out_with).status == "complete"
    norm_with = load_roll_manifest(out_with).negatives[0].normalization

    out_without = make_roll_dir(tmp_path / "without", "without")
    roll2 = load_roll_manifest(out_with)
    without_shell = load_roll_manifest(out_without)
    roll2.roll_id = without_shell.roll_id
    roll2.roll_name = without_shell.roll_name
    attach_base_frame(roll2, density=[-0.50, -0.20, -0.90])
    write_roll_manifest(out_without, roll2)
    monkeypatch.setattr(stitch_pipeline, "_locked_base_refs", lambda _roll: None)

    assert (
        run_stitch_with_defaults(work_dir, out_without, run_id="stitch-run-2").status
        == "complete"
    )
    norm_without = load_roll_manifest(out_without).negatives[0].normalization

    assert norm_with["floors"] == pytest.approx(norm_without["floors"], abs=1e-4)
    assert np.median(norm_with["ceils"]) == pytest.approx(
        np.median(norm_without["ceils"]), abs=1e-3
    )
    dev_with = np.asarray(norm_with["ceils"]) - np.median(norm_with["ceils"])
    dev_without = np.asarray(norm_without["ceils"]) - np.median(norm_without["ceils"])
    assert not np.allclose(dev_with, dev_without, atol=1e-4)
    assert norm_with["ceils"] != norm_without["ceils"]


def test_roll_invariants_are_seeded_by_the_first_run(tmp_path):
    """Section 5.4 decision 1: an empty roll cannot know its
    `processing_params` or `stitch_params`, so the first run establishes
    them and every later run is compared against them."""
    out_dir = make_roll_dir(tmp_path)
    empty = load_roll_manifest(out_dir)
    assert empty.processing_params == {}
    assert empty.stitch_params == {}

    run_stitch_with_defaults(make_work_dir(tmp_path), out_dir)

    seeded = load_roll_manifest(out_dir)
    assert seeded.processing_params == {"gamma": [1.8, 16]}
    assert seeded.stitch_params["detector"] == DETECTOR
    assert seeded.icc_profile == profile_record(ProfileKind.LINEAR)


def test_second_stitch_adopts_the_first_negative(work_dir, tmp_path):
    """The replacement rule: re-stitching the same group adopts the covered
    negative in place — same `negative_id`, same output name, record
    updated with the new run's data. Two genuine runs, per section 4 — a
    hand-edited manifest would prove nothing."""
    out_dir = make_roll_dir(tmp_path)

    first = run_stitch_with_defaults(work_dir, out_dir)
    second = run_stitch_with_defaults(work_dir, out_dir, run_id="stitch-run-2")

    assert first.published == ["IMG_00.tif"]
    assert second.published == ["IMG_00.tif"]
    assert second.status == "complete"
    assert (out_dir / "IMG_00.tif").exists()

    roll = load_roll_manifest(out_dir)
    assert [r.run_id for r in roll.runs] == ["stitch-run", "stitch-run-2"]
    assert len(roll.negatives) == 1
    # The adopted record keeps its id and name, but the run behind it is
    # the new one.
    adopted = roll.negatives[0]
    assert adopted.negative_id == "stitch-negative-01"
    assert adopted.run_id == "stitch-run-2"
    assert adopted.status == "completed"
    assert adopted.output["name"] == "IMG_00.tif"
    assert adopted.expected_output == "IMG_00.tif"
    published = out_dir / "IMG_00.tif"
    assert hashing.sha256_file(published) == adopted.output["sha256"]


def test_second_stitch_keeps_a_suffixed_name_it_adopted(work_dir, tmp_path):
    """Adoption keeps whatever `expected_output` the covered negative held,
    even a `-2` suffix — the name is the negative's, not a re-derivation."""
    out_dir = make_roll_dir(tmp_path)

    run_stitch_with_defaults(work_dir, out_dir)
    roll = load_roll_manifest(out_dir)
    roll.negatives[0].expected_output = "IMG_00-2.tif"
    roll.negatives[0].output["name"] = "IMG_00-2.tif"
    write_roll_manifest(out_dir, roll)
    (out_dir / "IMG_00.tif").replace(out_dir / "IMG_00-2.tif")

    second = run_stitch_with_defaults(work_dir, out_dir, run_id="stitch-run-2")

    assert second.published == ["IMG_00-2.tif"]
    roll = load_roll_manifest(out_dir)
    assert len(roll.negatives) == 1
    assert roll.negatives[0].negative_id == "stitch-negative-01"
    assert roll.negatives[0].output["name"] == "IMG_00-2.tif"


@pytest.mark.slow
def test_negatives_filter_restricts_stitch_to_the_named_negative(tmp_path):
    """Section 3.5's `--negatives` re-stitch path: a work directory with two
    negatives, restricted to one already-published `negative_id`, publishes
    only that one — adopting it in place, same `negative_id`, per the
    replacement rule."""
    work_dir = make_work_dir(tmp_path, negatives=2)
    out_dir = make_roll_dir(tmp_path)

    first = run_stitch_with_defaults(work_dir, out_dir)
    assert first.status == "complete"
    roll = load_roll_manifest(out_dir)
    target = roll.negatives[0]

    second = run_stitch_with_defaults(
        work_dir, out_dir, run_id="stitch-run-2", negatives=[target.negative_id]
    )

    assert second.status == "complete"
    assert len(second.published) == 1

    roll = load_roll_manifest(out_dir)
    republished = [n for n in roll.negatives if n.run_id == "stitch-run-2"]
    assert len(republished) == 1
    assert republished[0].members == target.members
    assert republished[0].negative_id == target.negative_id


# --- re-apply after re-stitch ----------------------------------------------

_REAPPLY_INTENDED = "2026-01-15T09:30:00.250000"


def _apply_manually(
    out_dir: Path, negative_id: str, intended: str = _REAPPLY_INTENDED
) -> None:
    """Sets `intended_datetime_original` and drives it through the real
    `apply-metadata` path, so `applied_datetime_original` is genuinely
    non-null before a re-stitch, per section 4."""
    roll = load_roll_manifest(out_dir)
    roll.negative(negative_id).capture_time.intended_datetime_original = intended
    write_roll_manifest(out_dir, roll)
    outcome = run_apply_metadata(out_dir, emit=lambda e: None)
    assert outcome.applied == [negative_id]


def test_restitch_reapplies_metadata(work_dir, tmp_path):
    out_dir = make_roll_dir(tmp_path)

    first = run_stitch_with_defaults(work_dir, out_dir)
    assert first.status == "complete"
    old_id = load_roll_manifest(out_dir).negatives[0].negative_id
    _apply_manually(out_dir, old_id)

    events: list = []
    second = run_stitch_with_defaults(
        work_dir, out_dir, run_id="stitch-run-2", events=events
    )
    assert second.status == "complete"

    roll = load_roll_manifest(out_dir)
    adopted = roll.negative(old_id)
    assert adopted.run_id == "stitch-run-2"
    # The adopted negative's applied time carried forward: it stays applied,
    # not dirty.
    assert adopted.capture_time.intended_datetime_original == _REAPPLY_INTENDED
    assert adopted.capture_time.applied_datetime_original == _REAPPLY_INTENDED

    tiff_path = out_dir / adopted.output["name"]
    info = tifftools.read_tiff(str(tiff_path))
    exif = info["ifds"][0]["tags"][Tag.ExifIFD.value]["ifds"][0][0]["tags"]
    assert exif[DATE_TIME_ORIGINAL]["data"] == "2026:01:15 09:30:00"
    assert exif[SUBSEC_TIME_ORIGINAL]["data"] == "25"

    applied_events = [e for e in events if isinstance(e, MetadataApplied)]
    assert [e.negative_id for e in applied_events] == [old_id]


def test_restitch_of_never_applied_negative_does_not_apply(work_dir, tmp_path):
    """No prior applied capture time to inherit -- the re-stitch is a
    no-op for metadata, and nothing dirty appears out of nowhere."""
    out_dir = make_roll_dir(tmp_path)

    first = run_stitch_with_defaults(work_dir, out_dir)
    assert first.status == "complete"
    old_id = load_roll_manifest(out_dir).negatives[0].negative_id

    events: list = []
    second = run_stitch_with_defaults(
        work_dir, out_dir, run_id="stitch-run-2", events=events
    )
    assert second.status == "complete"

    roll = load_roll_manifest(out_dir)
    adopted = roll.negative(old_id)
    assert adopted.capture_time.intended_datetime_original is None
    assert adopted.capture_time.applied_datetime_original is None

    assert not [e for e in events if isinstance(e, (MetadataApplied, MetadataSkipped))]


def test_failed_reapply_leaves_negative_dirty_not_failed(
    work_dir, tmp_path, monkeypatch
):
    out_dir = make_roll_dir(tmp_path)

    first = run_stitch_with_defaults(work_dir, out_dir)
    assert first.status == "complete"
    old_id = load_roll_manifest(out_dir).negatives[0].negative_id
    _apply_manually(out_dir, old_id)

    def _failing_rewrite(tiff_path, intended):
        raise ApplyMetadataFailure(
            Code.METADATA_WRITE_FAILED, "simulated rewrite failure"
        )

    monkeypatch.setattr(
        "scanny_boy.stitch_pipeline.rewrite_date_time_original", _failing_rewrite
    )

    events: list = []
    second = run_stitch_with_defaults(
        work_dir, out_dir, run_id="stitch-run-2", events=events
    )

    # A stitch is never failed by a metadata problem.
    assert second.status == "complete"

    roll = load_roll_manifest(out_dir)
    adopted = roll.negative(old_id)
    assert adopted.status == "completed"
    # The intent is still recorded -- it is what makes the negative dirty
    # and recoverable with Apply -- but it was never actually applied.
    assert adopted.capture_time.intended_datetime_original == _REAPPLY_INTENDED
    assert adopted.capture_time.applied_datetime_original is None

    skipped_events = [e for e in events if isinstance(e, MetadataSkipped)]
    assert len(skipped_events) == 1
    assert skipped_events[0].negative_id == adopted.negative_id
    assert skipped_events[0].code is Code.METADATA_WRITE_FAILED


# --- the regression guard on the output_folder.py refactor ---------------


def test_phase_one_output_folder_behaviour_is_unchanged(work_dir, tmp_path):
    """The explicit guard on the `output_folder.py` refactor: generalising
    it over which manifest it reads must not change what it
    does for Phase 1's. `output_folder_test.py`, `manifest_test.py`, and
    `pipeline_test.py` all still pass unmodified; this adds the direct
    statement that the default is Phase 1's rules and that the two manifest
    kinds do not see each other's folders."""
    convert_out = make_out_dir(tmp_path, "convert-out")

    # A Phase 1 output folder: its own manifest, and its own outputs.
    phase_one = load_manifest(work_dir)
    write_manifest(convert_out, phase_one)
    for group in phase_one.groups:
        for output in group.outputs:
            (convert_out / output.name).write_bytes(b"stand-in")

    # Default rules are Phase 1's rules, and passing them explicitly is
    # identical to omitting them.
    implicit = plan_rerun(convert_out, phase_one)
    explicit = plan_rerun(convert_out, phase_one, rules=PREPARE_RULES)
    assert implicit == explicit
    assert implicit.existing_manifest is not None
    assert sorted(implicit.conflicting_outputs) == sorted(
        phase_one.all_expected_outputs()
    )
    assert implicit.stale_outputs == []

    # The roll rules do not recognise a Phase 1 folder: it is not registered
    # as a roll, so it reads as unrelated content rather than as a rerun.
    roll_out = make_roll_dir(tmp_path, "roll-out")
    run_stitch_with_defaults(work_dir, roll_out)
    with pytest.raises(OutputFolderError) as exc_info:
        plan_rerun(convert_out, roll_invariants(work_dir), rules=ROLL_RULES)
    assert exc_info.value.code is Code.OUTPUT_NOT_EMPTY

    # And a stitched folder is likewise not a Phase 1 folder.
    with pytest.raises(OutputFolderError) as exc_info:
        plan_rerun(roll_out, phase_one)
    assert exc_info.value.code is Code.OUTPUT_NOT_EMPTY


# --- the memory estimate's frame_bbox_size input ----------------------------


def _make_grid_frames(*, across: int, down: int, seed: int = 3):
    """`across*down` uint16 frames cut from one synthetic scene at the
    2/3-step grid geometry (1/3 overlap), frames unrotated, in row-major
    order: index i sits at cell `(i // across, i % across)`."""
    frame_height, frame_width = FRAME_SIZE
    step_x = round(frame_width * 2 / 3)
    step_y = round(frame_height * 2 / 3)
    scene = synthetic_scene(
        frame_height + (down - 1) * step_y,
        frame_width + (across - 1) * step_x,
        seed=seed,
    )
    frames = []
    for index in range(across * down):
        row, col = divmod(index, across)
        x0 = col * step_x
        y0 = row * step_y
        patch = scene[y0 : y0 + frame_height, x0 : x0 + frame_width]
        frames.append(np.stack([patch, patch, patch], axis=-1))
    return [encode_from_linear(frame.astype(np.float32)) for frame in frames]


def _grid_pair_placements(across: int, down: int) -> dict[str, np.ndarray]:
    """Ground-truth placements matching `_make_grid_frames`' cutting, keyed
    by the intermediates' names (`IMG_<index>.tif`, row-major order)."""
    frame_height, frame_width = FRAME_SIZE
    step_x = round(frame_width * 2 / 3)
    step_y = round(frame_height * 2 / 3)
    placements = {}
    for index in range(across * down):
        row, col = divmod(index, across)
        name = f"IMG_{index:02d}.tif"
        t = np.array([col * step_x, row * step_y], dtype=np.float64)
        placements[name] = np.hstack([np.eye(2), t.reshape(2, 1)])
    return placements


def _grid_registration_fixtures(across: int, down: int):
    """Fake `_detect_all`/`register_pair` pair reproducing the grid's
    ground-truth geometry without paying for real registration."""
    placements = _grid_pair_placements(across, down)

    def fake_detect_all(paths, workers, cancel, *, use_clahe):
        return [
            registration.FrameFeatures(
                name=path.name,
                keypoints=(),
                descriptors=np.zeros((0, 1), dtype=np.float32),
                scale=1.0,
            )
            for path in paths
        ]

    def fake_register_pair(a, b, undistorter=None):
        return _ground_truth_similarity_pair(
            a.name, b.name, placements[a.name], placements[b.name], FRAME_SIZE
        )

    return fake_detect_all, fake_register_pair


def _ground_truth_similarity_pair(
    name_a: str,
    name_b: str,
    placement_a: np.ndarray,
    placement_b: np.ndarray,
    frame_size: tuple[int, int],
    *,
    n_points: int = 60,
    seed: int = 0,
):
    """A PairResult whose similarity fit is exactly the ground-truth
    relation between two placements — the registration `register_pair`
    would have produced for perfectly-placed frames."""
    rng = np.random.default_rng(seed)
    height, width = frame_size
    rotation_a, translation_a = placement_a[:, :2], placement_a[:, 2]
    rotation_b, translation_b = placement_b[:, :2], placement_b[:, 2]
    phi_ab = np.arctan2(rotation_b[1, 0], rotation_b[0, 0]) - np.arctan2(
        rotation_a[1, 0], rotation_a[0, 0]
    )
    u_ab = rotation_a.T @ (translation_b - translation_a)
    rotation_ab = np.array(
        [[np.cos(phi_ab), -np.sin(phi_ab)], [np.sin(phi_ab), np.cos(phi_ab)]]
    )
    pts_b = rng.uniform([0, 0], [width, height], size=(n_points, 2))
    pts_a = pts_b @ rotation_ab.T + u_ab
    transform = np.hstack([rotation_ab, u_ab.reshape(2, 1)])
    return registration.PairResult(
        a=name_a,
        b=name_b,
        transform=transform,
        good_matches=n_points,
        inliers=n_points,
        inlier_ratio=1.0,
        rms_residual_px=0.0,
        scale_drift=0.0,
        accepted=True,
        reject_code=None,
        reject_message=None,
        inlier_points_a=pts_a,
        inlier_points_b=pts_b,
        overlap_fraction=None,
        overlap_mad=None,
        overlap_mad_pregain=None,
        similarity_transform=transform,
        similarity_scale=1.0,
    )


def _make_grid_work_dir(tmp_path: Path, *, across: int, down: int) -> Path:
    """A work directory of across*down intermediates cut from one synthetic
    scene at the 2/3-step grid geometry, one group, real Phase 1
    manifest."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    frames = _make_grid_frames(across=across, down=down)
    members, outputs, source_order, sources = [], [], [], []
    for frame_index, frame in enumerate(frames):
        source_name = f"IMG_{frame_index:02d}.NEF"
        output_name = f"IMG_{frame_index:02d}.tif"
        write_intermediate(work_dir / output_name, frame, source_name)
        path = work_dir / output_name
        outputs.append(
            OutputRecord(
                name=output_name,
                size=path.stat().st_size,
                sha256=hashing.sha256_file(path),
            )
        )
        members.append(source_name)
        source_order.append(source_name)
        sources.append(
            SourceRecord(
                filename=source_name,
                absolute_path=f"/tmp/in/{source_name}",
                size=1000 + frame_index,
                mtime=1.0,
                sha256=str(frame_index).ljust(64, "c"),
            )
        )
    write_manifest(
        work_dir,
        Manifest(
            scanny_boy_version=current_scanny_boy_version(),
            run_id="convert-run",
            status="complete",
            input_folder="/tmp/in",
            film_date=FILM_DATE,
            shots_per_negative=across * down,
            grid={"across": across, "down": down},
            processing_params={"gamma": [1.8, 16]},
            icc_profile=profile_record(ProfileKind.LINEAR),
            source_order=source_order,
            sources=sources,
            curated_metadata=CuratedMetadata(
                exposure_time="1/30",
                f_number="8",
                iso=100,
                focal_length="55",
                lens_model="55mm f/2.8",
                orientation=1,
                camera_whitebalance=(1.69, 1.0, 1.38, 1.0),
            ),
            groups=[
                GroupRecord(
                    group_id="negative-01",
                    members=members,
                    expected_outputs=[f"{Path(m).stem}.tif" for m in members],
                    status="completed",
                    outputs=outputs,
                )
            ],
            started_at="2026-08-02T00:00:00Z",
            finished_at="2026-08-02T00:01:00Z",
        ),
    )
    return work_dir


def test_peak_estimate_scales_with_the_frame_box_not_the_canvas(tmp_path, monkeypatch):
    """A 5x2 grid: before the fix, `_attempt_solve` charged every frame the
    whole canvas as its bounding box, so the estimate scaled with the canvas;
    after it, with the frame. A machine with room for the true peak but not
    the canvas-inflated one must now pass where it used to raise
    INSUFFICIENT_MEMORY."""
    import scanny_boy.composite as composite_module

    across, down = 5, 2
    work_dir = _make_grid_work_dir(tmp_path, across=across, down=down)
    out_dir = make_roll_dir(tmp_path, "gridmem")

    # Ground-truth 5x2 geometry: 2/3 step on both axes (1/3 overlap), no
    # rotation, so the solve recovers it exactly and every frame bbox is
    # exactly one frame.
    fake_detect_all, fake_register_pair = _grid_registration_fixtures(across, down)

    monkeypatch.setattr(stitch_pipeline, "_detect_all", fake_detect_all)
    monkeypatch.setattr(stitch_pipeline, "register_pair", fake_register_pair)

    # First run with an unbounded budget, spying on what the gate is
    # actually told: the frame bbox must come out frame-sized, not
    # canvas-sized.
    captured: list[tuple] = []
    real_estimate = composite_module.estimate_peak_bytes

    def spy(canvas_size, f_size, bbox_size, f_count, **kwargs):
        captured.append((canvas_size, f_size, bbox_size, f_count))
        return real_estimate(canvas_size, f_size, bbox_size, f_count, **kwargs)

    monkeypatch.setattr(composite_module, "physical_memory_bytes", lambda: 10**15)
    monkeypatch.setattr(stitch_pipeline, "estimate_peak_bytes", spy)

    events: list = []
    outcome = run_stitch_with_defaults(work_dir, out_dir, events=events)
    assert outcome.published == ["IMG_00.tif"]

    canvas_size, f_size, bbox_size, f_count = captured[0]
    # The bbox is one frame (plus at most a pixel of rotation rounding),
    # nowhere near the canvas — this is the behaviour change itself.
    assert bbox_size[0] <= f_size[0] + 2 and bbox_size[1] <= f_size[1] + 2
    assert canvas_size[0] > f_size[1] * 2 and canvas_size[1] > f_size[0]

    old_peak = real_estimate(
        canvas_size, f_size, (canvas_size[1], canvas_size[0]), f_count
    )
    new_peak = real_estimate(canvas_size, f_size, bbox_size, f_count)
    assert old_peak > new_peak * 3  # the bug is worth a large factor here

    # A second run, now with room for the true peak but not the inflated
    # one: `usable` sits strictly between the two estimates.
    monkeypatch.setattr(
        composite_module, "physical_memory_bytes", lambda: 2 * (new_peak + 1)
    )
    monkeypatch.setattr(stitch_pipeline, "estimate_peak_bytes", real_estimate)

    events = []
    outcome = run_stitch_with_defaults(
        work_dir, out_dir, run_id="stitch-run-2", events=events
    )

    memory_failures = [
        e
        for e in events
        if isinstance(e, NegativeFailed) and e.code is Code.INSUFFICIENT_MEMORY
    ]
    assert memory_failures == []
    # The negative reaches compositing and publishes: the gate let it through.
    assert outcome.published == ["IMG_00.tif"]


def test_peak_estimate_5x2_target_workload_matches_the_formula():
    """The §1a.3 number pinned: at the plan's target workload (6000x4000
    frames, 1/3 overlap, 5x2 grid), the estimate is computed from a
    frame-sized bbox — required RAM (2x peak, the half-of-physical gate)
    must sit near 47.5 GB, not the 197.9 GB the canvas-as-bbox bug
    charged."""
    canvas_size = (22000, 6667)
    frame_size = (4000, 6000)  # (height, width)
    bbox_size = (4000, 6000)  # (height, width)

    canvas_width, canvas_height = canvas_size
    frame_height, frame_width = frame_size
    canvas_pixels = canvas_width * canvas_height
    frame_pixels = frame_width * frame_height
    bbox_pixels = frame_pixels

    accum = canvas_pixels * 3 * 4
    weight = canvas_pixels * 4
    result = canvas_pixels * 3 * 2
    log_density = canvas_pixels * 3 * 4
    normalized = canvas_pixels * 3 * 4
    source = frame_pixels * 3 * 2 + frame_pixels * 3 * 4
    warped = bbox_pixels * 3 * 4
    warp_aux = bbox_pixels * 2
    feather_scratch = bbox_pixels * 4 * 3

    all_warped = 10 * (warped + warp_aux)
    live_bytes = max(
        accum + weight + source + all_warped + feather_scratch,
        accum + weight + log_density + normalized + result,
    )
    expected = math.ceil(live_bytes * MEMORY_SAFETY_FACTOR)

    actual = estimate_peak_bytes(canvas_size, frame_size, bbox_size, 10)
    assert actual == expected

    required_gb = 2 * actual / 1024**3
    assert 40 < required_gb < 56  # §1a.3: 47.5 GB required, 26% headroom on 64 GB
    inflated = estimate_peak_bytes(
        canvas_size, frame_size, (canvas_height, canvas_width), 10
    )
    assert inflated > 3 * actual  # the pre-G-0 bug charged the canvas per frame


# --- auto-rotation seeding -------------------------------------------------


def test_auto_rotation_seeds_one_fine_op_on_a_new_negative(
    work_dir, tmp_path, monkeypatch
):
    """A newly published negative gets the estimated rebate tilt seeded as
    one `rotate_fine` ops-log entry — emitted as `edit_recorded`, preview
    regenerated through the net transform that already carries it — while
    the published TIFF itself is never rotated."""
    out_dir = make_roll_dir(tmp_path, "autorot")
    monkeypatch.setattr(stitch_pipeline, "estimate_rotation", lambda image: 1.5)
    events: list = []

    outcome = run_stitch_with_defaults(work_dir, out_dir, events=events)

    assert outcome.status == "complete"
    edits = repo.edits_for(out_dir, "stitch-negative-01")
    rotate_edits = [e for e in edits if e["op"] == repo.ROTATE_FINE_OP]
    (edit,) = rotate_edits
    assert edit["op"] == repo.ROTATE_FINE_OP
    assert edit["params"] == {"angle_deg": 1.5, "source": "auto"}
    assert repo.net_edit_state(out_dir, "stitch-negative-01") == repo.EditState(
        quarter_turns=0,
        flipped=False,
        fine_angle_deg=1.5,
        tone=None,
        color=None,
        scratches={
            "detector_version": 1,
            "source": "auto",
            "enabled": True,
            "canvas": [2080, 730],
            "scratches": [],
        },
    )
    (recorded,) = [e for e in events if isinstance(e, EditRecorded)]
    assert recorded.negative_id == "stitch-negative-01"
    assert recorded.fine_rotation_deg == pytest.approx(1.5)
    assert recorded.preview_path is not None
    assert Path(recorded.preview_path).exists()

    manifest = load_roll_manifest(out_dir)
    negative = manifest.negative("stitch-negative-01")
    assert negative.preview_path == recorded.preview_path


def test_auto_rotation_seeds_nothing_when_the_estimator_declines(
    work_dir, tmp_path, monkeypatch
):
    """The synthetic fixtures carry no rebate, so the real estimator
    declines; the seeded op log is empty either way."""
    out_dir = make_roll_dir(tmp_path, "norebate")

    outcome = run_stitch_with_defaults(work_dir, out_dir)

    assert outcome.status == "complete"
    edits = repo.edits_for(out_dir, "stitch-negative-01")
    rotate_edits = [e for e in edits if e["op"] == repo.ROTATE_FINE_OP]
    assert rotate_edits == []


def test_auto_rotation_off_seeds_nothing(work_dir, tmp_path, monkeypatch):
    def _fail(image):
        raise AssertionError("the estimator must not run with auto-rotate off")

    out_dir = make_roll_dir(tmp_path, "norot")
    monkeypatch.setattr(stitch_pipeline, "estimate_rotation", _fail)

    outcome = run_stitch_with_defaults(work_dir, out_dir, auto_rotate=False)

    assert outcome.status == "complete"
    edits = repo.edits_for(out_dir, "stitch-negative-01")
    rotate_edits = [e for e in edits if e["op"] == repo.ROTATE_FINE_OP]
    assert rotate_edits == []


def test_a_re_stitch_never_re_seeds_the_auto_rotation(work_dir, tmp_path, monkeypatch):
    """An adopted negative keeps the rotation its first publish seeded:
    re-seeding would stack a second fine rotation on top of it."""
    out_dir = make_roll_dir(tmp_path, "reseeds")
    monkeypatch.setattr(stitch_pipeline, "estimate_rotation", lambda image: 1.5)

    run_stitch_with_defaults(work_dir, out_dir)

    def _fail(image):
        raise AssertionError("an adopted negative must not be re-seeded")

    monkeypatch.setattr(stitch_pipeline, "estimate_rotation", _fail)
    second = run_stitch_with_defaults(work_dir, out_dir, run_id="restitch-run")

    assert second.status == "complete"
    edits = repo.edits_for(out_dir, "stitch-negative-01")
    rotate_edits = [e for e in edits if e["op"] == repo.ROTATE_FINE_OP]
    assert len(rotate_edits) == 1


# --- auto-crop seeding -----------------------------------------------------


def _tick_auto_crop(roll_dir, format="35mm"):
    from scanny_boy.roll_folder import set_setup

    set_setup(roll_dir, format=format, auto_crop=True)


def _detector(monkeypatch, *, rect=(100, 60, 1200, 600), result=None):
    """Stand in for `auto_crop.estimate_crop`: the synthetic scans carry no
    rebate for the real detector to read, and the pipeline's seeding is what
    is under test (the detector has its own tests). Returns the call log."""
    from scanny_boy import auto_crop

    calls: list[dict] = []

    def fake(analysis, *, full_size, ratio, exclude=None):
        calls.append({"ratio": ratio, "full_size": full_size, "exclude": exclude})
        if result is not None:
            return result
        return auto_crop.AutoCrop(
            rect=rect, ratio=ratio, picture_fraction=0.81, fill_fraction=0.93
        )

    monkeypatch.setattr(auto_crop, "estimate_crop", fake)
    return calls


def _user_ops(out_dir, negative_id="stitch-negative-01"):
    """The negative's ops, less the scratch detector's own re-detection
    (a stitch refreshes that state op independently of auto-crop)."""
    return [
        e for e in repo.edits_for(out_dir, negative_id) if e["op"] != repo.SCRATCHES_OP
    ]


def _crop_ops(out_dir, negative_id="stitch-negative-01"):
    return [e for e in repo.edits_for(out_dir, negative_id) if e["op"] == repo.CROP_OP]


def test_auto_crop_seeds_a_crop_after_the_rotation_on_a_new_negative(
    work_dir, tmp_path, monkeypatch
):
    from scanny_boy import edits

    out_dir = make_roll_dir(tmp_path, "autocrop")
    _tick_auto_crop(out_dir, "35mm")
    calls = _detector(monkeypatch)
    monkeypatch.setattr(stitch_pipeline, "estimate_rotation", lambda image: 1.5)
    events: list = []

    outcome = run_stitch_with_defaults(work_dir, out_dir, events=events)

    assert outcome.status == "complete"
    ops = [e["op"] for e in repo.edits_for(out_dir, "stitch-negative-01")]
    assert ops.index(repo.ROTATE_FINE_OP) < ops.index(repo.CROP_OP)
    (crop,) = _crop_ops(out_dir)
    params = crop["params"]
    assert params["source"] == "auto"
    assert params["preset"] == "35mm"
    assert params["canvas"] == [2080, 730]
    assert calls[0]["ratio"] == pytest.approx(1.5)
    # Two ordinary edit_recorded events; the second carries the net crop.
    recorded = [e for e in events if isinstance(e, EditRecorded)]
    assert [e.edit["op"] for e in recorded] == [repo.ROTATE_FINE_OP, repo.CROP_OP]
    assert recorded[0].crop is None
    assert recorded[1].crop["source"] == "auto"
    assert recorded[1].crop["preset"] == "35mm"
    assert recorded[1].preview_path is not None
    # The evidence block.
    negative = load_roll_manifest(out_dir).negative("stitch-negative-01")
    assert negative.auto_crop == {
        "result": "seeded",
        "reason": None,
        "picture_fraction": 0.81,
        "fill_fraction": 0.93,
        "version": 1,
    }
    # Crop mode round trip: `edit crop --full-frame` with the reported
    # x/y/width/height reproduces the stored window.
    report = recorded[1].crop
    before = repo.net_edit_state(out_dir, "stitch-negative-01").crop
    edits.run_edit_crop(
        out_dir,
        "stitch-negative-01",
        rect=(report["x"], report["y"], report["width"], report["height"]),
        tilt_deg=report["tilt_deg"],
        full_frame=True,
        emit=lambda event: None,
    )
    after = repo.net_edit_state(out_dir, "stitch-negative-01").crop
    for key in ("x", "y", "w", "h"):
        assert after[key] == pytest.approx(before[key], abs=2)
    assert after["tilt_deg"] == pytest.approx(before["tilt_deg"], abs=0.05)


def test_auto_crop_is_measured_under_the_seeded_rotation(
    work_dir, tmp_path, monkeypatch
):
    out_dir = make_roll_dir(tmp_path, "cropangle")
    _tick_auto_crop(out_dir)
    _detector(monkeypatch)
    monkeypatch.setattr(stitch_pipeline, "estimate_rotation", lambda image: 1.5)
    seen: list[dict] = []
    real = stitch_pipeline.auto_crop_module.analysis_display

    def spy(image, **kwargs):
        seen.append(kwargs)
        return real(image, **kwargs)

    monkeypatch.setattr(stitch_pipeline.auto_crop_module, "analysis_display", spy)

    run_stitch_with_defaults(work_dir, out_dir)

    assert seen == [{"flipped": False, "fine_angle_deg": 1.5}]


def test_auto_crop_off_or_unset_seeds_nothing_and_records_no_evidence(
    work_dir, tmp_path, monkeypatch
):
    from scanny_boy import auto_crop
    from scanny_boy.roll_folder import set_setup

    def _fail(*args, **kwargs):
        raise AssertionError("the detector must not run with auto-crop off")

    monkeypatch.setattr(auto_crop, "estimate_crop", _fail)
    out_dir = make_roll_dir(tmp_path, "cropoff")
    set_setup(out_dir, format="35mm")  # a format alone does not turn it on

    events: list = []
    outcome = run_stitch_with_defaults(work_dir, out_dir, events=events)

    assert outcome.status == "complete"
    assert _crop_ops(out_dir) == []
    assert load_roll_manifest(out_dir).negative("stitch-negative-01").auto_crop is None
    assert not [
        e
        for e in events
        if isinstance(e, WarningEvent) and e.code == Code.AUTO_CROP_NO_FORMAT
    ]


def test_no_auto_crop_flag_overrides_a_ticked_roll(work_dir, tmp_path, monkeypatch):
    from scanny_boy import auto_crop

    def _fail(*args, **kwargs):
        raise AssertionError("--no-auto-crop must not run the detector")

    monkeypatch.setattr(auto_crop, "estimate_crop", _fail)
    out_dir = make_roll_dir(tmp_path, "nocropflag")
    _tick_auto_crop(out_dir)

    outcome = run_stitch_with_defaults(work_dir, out_dir, auto_crop=False)

    assert outcome.status == "complete"
    assert _crop_ops(out_dir) == []
    assert load_roll_manifest(out_dir).negative("stitch-negative-01").auto_crop is None


def test_auto_crop_without_a_format_warns_once_per_run_and_records_no_format(
    tmp_path, monkeypatch
):
    from scanny_boy.roll_folder import set_setup

    work_dir = make_work_dir(tmp_path, negatives=2)
    out_dir = make_roll_dir(tmp_path, "noformat")
    set_setup(out_dir, auto_crop=True)
    calls = _detector(monkeypatch)
    events: list = []

    outcome = run_stitch_with_defaults(work_dir, out_dir, events=events)

    assert outcome.status == "complete"
    warnings = [
        e
        for e in events
        if isinstance(e, WarningEvent) and e.code == Code.AUTO_CROP_NO_FORMAT
    ]
    assert len(warnings) == 1
    assert calls == []
    manifest = load_roll_manifest(out_dir)
    assert len(manifest.negatives) == 2
    for negative in manifest.negatives:
        assert negative.auto_crop["result"] == "no_format"
        assert _crop_ops(out_dir, negative.negative_id) == []


def test_a_detector_exception_warns_and_the_stitch_still_succeeds(
    work_dir, tmp_path, monkeypatch
):
    from scanny_boy import auto_crop

    def boom(*args, **kwargs):
        raise RuntimeError("no picture for you")

    monkeypatch.setattr(auto_crop, "estimate_crop", boom)
    out_dir = make_roll_dir(tmp_path, "cropboom")
    _tick_auto_crop(out_dir)
    events: list = []

    outcome = run_stitch_with_defaults(work_dir, out_dir, events=events)

    assert outcome.status == "complete"
    (warning,) = [
        e
        for e in events
        if isinstance(e, WarningEvent) and e.code == Code.AUTO_CROP_FAILED
    ]
    assert "no picture for you" in warning.message
    assert _crop_ops(out_dir) == []
    negative = load_roll_manifest(out_dir).negative("stitch-negative-01")
    assert negative.auto_crop["result"] == "refused"
    assert negative.auto_crop["reason"] == "error"


def test_a_refusal_seeds_nothing_and_is_recorded_as_evidence(
    work_dir, tmp_path, monkeypatch
):
    from scanny_boy import auto_crop

    out_dir = make_roll_dir(tmp_path, "cropref")
    _tick_auto_crop(out_dir)
    _detector(
        monkeypatch,
        result=auto_crop.Refusal("ragged", picture_fraction=0.5, fill_fraction=0.4),
    )
    events: list = []

    outcome = run_stitch_with_defaults(work_dir, out_dir, events=events)

    assert outcome.status == "complete"
    assert _crop_ops(out_dir) == []
    assert not [
        e
        for e in events
        if isinstance(e, WarningEvent) and e.code == Code.AUTO_CROP_FAILED
    ]
    negative = load_roll_manifest(out_dir).negative("stitch-negative-01")
    assert negative.auto_crop == {
        "result": "refused",
        "reason": "ragged",
        "picture_fraction": 0.5,
        "fill_fraction": 0.4,
        "version": 1,
    }


def test_a_re_stitch_replaces_a_crop_that_is_still_automatic(
    work_dir, tmp_path, monkeypatch
):
    out_dir = make_roll_dir(tmp_path, "reseed")
    _tick_auto_crop(out_dir)
    _detector(monkeypatch, rect=(100, 60, 1200, 600))
    run_stitch_with_defaults(work_dir, out_dir)
    (first,) = _crop_ops(out_dir)
    events: list = []

    _detector(monkeypatch, rect=(140, 80, 1100, 550))
    outcome = run_stitch_with_defaults(
        work_dir, out_dir, run_id="restitch-run", events=events
    )

    assert outcome.status == "complete"
    ops = _crop_ops(out_dir)
    assert len(ops) == 2
    assert ops[1]["params"]["source"] == "auto"
    assert (ops[1]["params"]["x"], ops[1]["params"]["w"]) != (
        first["params"]["x"],
        first["params"]["w"],
    )
    assert (
        load_roll_manifest(out_dir).negative("stitch-negative-01").auto_crop["result"]
        == "reseeded"
    )
    recorded = [e for e in events if isinstance(e, EditRecorded)]
    assert [e.edit["op"] for e in recorded] == [repo.CROP_OP]


def test_a_re_stitch_with_an_identical_crop_appends_nothing(
    work_dir, tmp_path, monkeypatch
):
    out_dir = make_roll_dir(tmp_path, "noreseed")
    _tick_auto_crop(out_dir)
    _detector(monkeypatch)
    run_stitch_with_defaults(work_dir, out_dir)
    events: list = []

    run_stitch_with_defaults(work_dir, out_dir, run_id="restitch-run", events=events)

    assert len(_crop_ops(out_dir)) == 1
    assert not [e for e in events if isinstance(e, EditRecorded)]


def test_a_re_stitch_leaves_a_user_crop_alone(work_dir, tmp_path, monkeypatch):
    from scanny_boy import edits

    out_dir = make_roll_dir(tmp_path, "usercrop")
    _tick_auto_crop(out_dir)
    _detector(monkeypatch)
    run_stitch_with_defaults(work_dir, out_dir)
    edits.run_edit_crop(
        out_dir,
        "stitch-negative-01",
        rect=(200, 100, 900, 500),
        full_frame=True,
        emit=lambda event: None,
    )
    calls = _detector(monkeypatch, rect=(1, 1, 500, 300))
    before = _user_ops(out_dir)

    run_stitch_with_defaults(work_dir, out_dir, run_id="restitch-run")

    assert calls == []
    assert _user_ops(out_dir) == before


def test_a_re_stitch_leaves_a_cleared_crop_alone(work_dir, tmp_path, monkeypatch):
    """`Original` then Apply records a reset: the latest crop op carries no
    `source`, so the reseed rule reads it as the user's."""
    from scanny_boy import edits

    out_dir = make_roll_dir(tmp_path, "clearcrop")
    _tick_auto_crop(out_dir)
    _detector(monkeypatch)
    run_stitch_with_defaults(work_dir, out_dir)
    edits.run_edit_crop(
        out_dir, "stitch-negative-01", reset=True, emit=lambda event: None
    )
    calls = _detector(monkeypatch, rect=(1, 1, 500, 300))
    before = _user_ops(out_dir)

    run_stitch_with_defaults(work_dir, out_dir, run_id="restitch-run")

    assert calls == []
    assert _user_ops(out_dir) == before


def test_a_re_stitch_never_fills_in_a_crop_that_was_never_seeded(
    work_dir, tmp_path, monkeypatch
):
    out_dir = make_roll_dir(tmp_path, "nevercrop")
    run_stitch_with_defaults(work_dir, out_dir)  # auto-crop off: no crop
    _tick_auto_crop(out_dir)
    calls = _detector(monkeypatch)

    run_stitch_with_defaults(work_dir, out_dir, run_id="restitch-run")

    assert calls == []
    assert _crop_ops(out_dir) == []


def test_a_refused_reseed_leaves_the_old_op_alone(work_dir, tmp_path, monkeypatch):
    from scanny_boy import auto_crop

    out_dir = make_roll_dir(tmp_path, "refusereseed")
    _tick_auto_crop(out_dir)
    _detector(monkeypatch)
    run_stitch_with_defaults(work_dir, out_dir)
    before = _user_ops(out_dir)
    _detector(monkeypatch, result=auto_crop.Refusal("little_picture"))

    run_stitch_with_defaults(work_dir, out_dir, run_id="restitch-run")

    assert _user_ops(out_dir) == before
    negative = load_roll_manifest(out_dir).negative("stitch-negative-01")
    assert negative.auto_crop["result"] == "refused"
    assert negative.auto_crop["reason"] == "little_picture"


def test_a_reseed_is_measured_under_the_negatives_own_mirror_and_rotation(
    work_dir, tmp_path, monkeypatch
):
    """The mirror negates the net fine angle, so an adopted negative is
    measured flipped and under its existing net rotation — never the
    seeded one (rotation is not reseeded)."""
    out_dir = make_roll_dir(tmp_path, "reseedflip")
    _tick_auto_crop(out_dir)
    _detector(monkeypatch)
    monkeypatch.setattr(stitch_pipeline, "estimate_rotation", lambda image: 1.5)
    run_stitch_with_defaults(work_dir, out_dir)
    repo.append_edit(out_dir, "stitch-negative-01", repo.FLIP_OP, {})
    _detector(monkeypatch, rect=(140, 80, 1100, 550))
    monkeypatch.setattr(stitch_pipeline, "estimate_rotation", lambda image: 4.0)
    seen: list[dict] = []
    real = stitch_pipeline.auto_crop_module.analysis_display

    def spy(image, **kwargs):
        seen.append(kwargs)
        return real(image, **kwargs)

    monkeypatch.setattr(stitch_pipeline.auto_crop_module, "analysis_display", spy)

    run_stitch_with_defaults(work_dir, out_dir, run_id="restitch-run")

    assert seen == [{"flipped": True, "fine_angle_deg": -1.5}]
    assert repo.net_edit_state(out_dir, "stitch-negative-01").fine_angle_deg == -1.5


def test_stitch_params_record_the_detector_constants_without_locking_the_roll(
    work_dir, tmp_path
):
    """The auto-crop entry is non-invariant: a roll stitched before it
    existed must still accept a run that carries it."""
    out_dir = make_roll_dir(tmp_path, "cropparams")
    run_stitch_with_defaults(work_dir, out_dir)
    roll = load_roll_manifest(out_dir)
    assert roll.stitch_params["auto_crop"]["version"] == 1

    del roll.stitch_params["auto_crop"]  # a roll from before this feature
    write_roll_manifest(out_dir, roll)

    outcome = run_stitch_with_defaults(work_dir, out_dir, run_id="restitch-run")

    assert outcome.status == "complete"
    assert load_roll_manifest(out_dir).stitch_params["auto_crop"]["version"] == 1


# --- explicit film kind at roll init -------------------------------------


def test_mono_roll_publishes_one_channel_and_density_grey_profile(tmp_path):
    """A roll created with `film_kind=monochrome` publishes a true 2-D TIFF
    and seeds the grey density profile at init."""
    import tifffile

    from scanny_boy.icc_profile import DENSITY_GREY_PROFILE_SHA256

    work_dir = make_work_dir(tmp_path)
    out_dir = make_roll_dir(tmp_path, film_kind="monochrome")

    assert run_stitch_with_defaults(work_dir, out_dir).status == "complete"

    roll = load_roll_manifest(out_dir)
    assert roll.film == {"kind": "monochrome"}
    assert roll.published_icc_profile["sha256"] == DENSITY_GREY_PROFILE_SHA256
    published = out_dir / roll.negatives[0].output["name"]
    assert tifffile.imread(published).ndim == 2
    assert repo.net_edit_state(out_dir, roll.negatives[0].negative_id).scratches is None


def test_film_kind_required_when_roll_has_no_film_block(tmp_path):
    """An unseeded roll with no `film` block cannot be stitched."""
    work_dir = make_work_dir(tmp_path)
    out_dir = make_out_dir(tmp_path)
    manifest = new_roll_manifest(
        roll_id="00000000-0000-4000-8000-000000000099",
        roll_name="no-kind",
        film_kind="colour",
    )
    manifest.film = None
    attach_base_frame(manifest)
    write_roll_manifest(out_dir, manifest)

    with pytest.raises(StitchError) as exc_info:
        run_stitch_with_defaults(work_dir, out_dir)
    assert exc_info.value.code == Code.FILM_KIND_REQUIRED


def test_legacy_roll_with_runs_but_no_film_block_is_treated_as_colour(tmp_path):
    """§5.2: a roll that already has runs but no `film` block predates
    explicit film kind and is treated as frozen colour."""
    work_dir = make_work_dir(tmp_path)
    out_dir = make_roll_dir(tmp_path)
    roll = load_roll_manifest(out_dir)
    work_manifest = load_manifest(work_dir)
    append_run(
        roll,
        RunRecord(
            run_id="legacy-run",
            kind="stitch",
            status="complete",
            started_at="2026-01-01T00:00:00Z",
        ),
    )
    roll.processing_params = work_manifest.processing_params
    roll.icc_profile = work_manifest.icc_profile
    roll.stitch_params = stitch_pipeline._stitch_params(None)
    roll.film = None
    write_roll_manifest(out_dir, roll)

    assert (
        run_stitch_with_defaults(work_dir, out_dir, run_id="stitch-run-2").status
        == "complete"
    )
    roll_after = load_roll_manifest(out_dir)
    assert (
        roll_after.published_icc_profile["sha256"]
        == profile_record(ProfileKind.DENSITY)["sha256"]
    )


# --- the camera_color block -------------------------------------------


def test_seed_camera_color_writes_the_block_on_the_first_run():
    roll = new_roll_manifest(roll_id="r", roll_name="roll", film_kind="colour")
    manifest = _work_manifest(
        curated_metadata=_curated_with_matrix(),
    )
    events: list[WarningEvent] = []

    _seed_camera_color(roll, manifest, emit=events.append)

    assert roll.camera_color == CameraColor(
        rgb_xyz_matrix=_matrix(),
        source="libraw",
        camera_model="NIKON Z 7",
    )
    assert events == []


def test_seed_camera_color_is_frozen_and_warns_on_a_conflict():
    roll = new_roll_manifest(roll_id="r", roll_name="roll", film_kind="colour")
    roll.camera_color = CameraColor(
        rgb_xyz_matrix=_matrix(),
        source="libraw",
        camera_model="NIKON Z 7",
    )
    events: list[WarningEvent] = []

    # A later run whose source reports a different matrix — a different
    # body mid-roll — warns and keeps the frozen value.
    _seed_camera_color(
        roll,
        _work_manifest(curated_metadata=_curated_with_matrix(scale=0.9)),
        emit=events.append,
    )

    assert roll.camera_color.rgb_xyz_matrix == _matrix()
    assert roll.camera_color.camera_model == "NIKON Z 7"
    assert [event.code for event in events] == [Code.CAMERA_MATRIX_CONFLICT]


def test_seed_camera_color_tolerates_an_identical_matrix_silently():
    roll = new_roll_manifest(roll_id="r", roll_name="roll", film_kind="colour")
    roll.camera_color = CameraColor(
        rgb_xyz_matrix=_matrix(),
        source="libraw",
        camera_model="NIKON Z 7",
    )
    events: list[WarningEvent] = []

    _seed_camera_color(
        roll,
        _work_manifest(curated_metadata=_curated_with_matrix()),
        emit=events.append,
    )

    assert events == []


def test_seed_camera_color_no_ops_without_a_matrix():
    roll = new_roll_manifest(roll_id="r", roll_name="roll", film_kind="colour")
    _seed_camera_color(
        roll,
        _work_manifest(),
        emit=lambda event: None,  # type: ignore[arg-type]
    )
    assert roll.camera_color is None


# --- re-stitch refits the deband regions -------------------------------------


def _fake_deband_fit(window, axis="vertical", *, blocks=3):
    from scanny_boy import deband

    n_cols = math.ceil(
        (window[2] if axis == "vertical" else window[3]) / deband.PITCH_PX
    )
    return deband.RegionFit(
        window=tuple(float(v) for v in window),
        axis=axis,
        block_px=deband.BLOCK_PX,
        pitch_px=deband.PITCH_PX,
        blocks=blocks,
        corr=np.zeros((blocks, n_cols, 3), dtype=np.float32),
    )


def _plant_deband_op(
    out_dir, record, windows, *, canvas=(2200, 790), axis="vertical", **settings
):
    """Record a `deband` op the way an earlier edit would have, against a
    canvas that differs from the stitched one (so it is stale)."""
    from scanny_boy import deband

    norm = record.normalization
    spans = tuple(norm["ceils"][ch] - norm["floors"][ch] for ch in range(3))
    params = deband.deband_params(
        canvas,
        axis,
        [_fake_deband_fit(window, axis) for window in windows],
        settings.get("enabled", True),
        settings.get("strength", 1.0),
        spans,
        ids=settings.get("ids"),
    )
    repo.append_deband_edit(out_dir, record.negative_id, params)
    return params


def _stitched_with_stale_deband(work_dir, tmp_path, windows, **kwargs):
    out_dir = make_roll_dir(tmp_path, "debandroll")
    assert run_stitch_with_defaults(work_dir, out_dir).status == "complete"
    record = load_roll_manifest(out_dir).negatives[0]
    params = _plant_deband_op(out_dir, record, windows, **kwargs)
    return out_dir, record, params


def test_restitch_refits_the_deband_regions_onto_the_new_canvas(
    work_dir, tmp_path, monkeypatch
):
    from scanny_boy import deband

    out_dir, record, _planted = _stitched_with_stale_deband(
        work_dir,
        tmp_path,
        [(0.0, 0.0, 2200.0, 790.0, 0.0), (300.0, 100.0, 900.0, 600.0, 1.5)],
        axis="horizontal",
        enabled=False,
        strength=0.6,
        ids=[3, 8],
    )
    assert repo.net_edit_state(out_dir, record.negative_id).deband["canvas"] == [
        2200,
        790,
    ]
    calls: list[dict] = []

    def stub_fit(image, spans, window, axis, *, prior_regions=None, **kwargs):
        calls.append(
            {
                "shape": image.shape,
                "spans": tuple(spans),
                "window": window,
                "axis": axis,
                "prior": len(prior_regions or []),
            }
        )
        return _fake_deband_fit(window, axis)

    monkeypatch.setattr(deband, "fit_region", stub_fit)
    events: list = []

    outcome = run_stitch_with_defaults(
        work_dir, out_dir, events=events, run_id="restitch-run"
    )

    assert outcome.status == "complete"
    assert not [
        e for e in events if getattr(e, "code", None) is Code.DEBAND_REFIT_FAILED
    ]
    spans = tuple(
        record.normalization["ceils"][c] - record.normalization["floors"][c]
        for c in range(3)
    )
    assert [(c["shape"], c["axis"], c["prior"]) for c in calls] == [
        ((730, 2080, 3), "horizontal", 0),
        ((730, 2080, 3), "horizontal", 1),
    ]
    assert calls[0]["spans"] == pytest.approx(spans)
    # Each window is clamped to the new 2080 x 730 canvas.
    assert calls[0]["window"] == (0.0, 0.0, 2080.0, 730.0, 0.0)
    assert calls[1]["window"] == (300.0, 100.0, 900.0, 600.0, 1.5)
    op = repo.net_edit_state(out_dir, record.negative_id).deband
    assert op["canvas"] == [2080, 730]
    assert (op["enabled"], op["strength"], op["axis"]) == (False, 0.6, "horizontal")
    assert [r["id"] for r in op["regions"]] == [3, 8]
    assert op["spans"] == pytest.approx(spans)
    assert deband.fits_from_params(op)  # the new tables decode


def test_restitch_with_no_deband_op_records_none_and_does_not_fit(
    work_dir, tmp_path, monkeypatch
):
    from scanny_boy import deband

    out_dir = make_roll_dir(tmp_path, "nodeband")
    assert run_stitch_with_defaults(work_dir, out_dir).status == "complete"

    def boom(*args, **kwargs):
        raise AssertionError("nothing to refit")

    monkeypatch.setattr(deband, "fit_region", boom)

    run_stitch_with_defaults(work_dir, out_dir, run_id="restitch-run")

    ops = [e["op"] for e in repo.edits_for(out_dir, "stitch-negative-01")]
    assert repo.DEBAND_OP not in ops


def test_restitch_carries_an_empty_deband_op_onto_the_new_canvas(
    work_dir, tmp_path, monkeypatch
):
    """A cleared op keeps its settings; the refit just moves it to the new
    canvas so it is not reported stale."""
    from scanny_boy import deband

    out_dir, record, _ = _stitched_with_stale_deband(
        work_dir, tmp_path, [], enabled=False, strength=1.2
    )

    def boom(*args, **kwargs):
        raise AssertionError("no regions to refit")

    monkeypatch.setattr(deband, "fit_region", boom)

    run_stitch_with_defaults(work_dir, out_dir, run_id="restitch-run")

    op = repo.net_edit_state(out_dir, record.negative_id).deband
    assert op["canvas"] == [2080, 730]
    assert op["regions"] == []
    assert (op["enabled"], op["strength"]) == (False, 1.2)


@pytest.mark.parametrize("failure", ["too_little_background", "crash"])
def test_a_failed_deband_refit_warns_and_never_fails_the_stitch(
    work_dir, tmp_path, monkeypatch, failure
):
    """The synthetic scene is all texture, so the real fit refuses it for
    lack of flat background; a crash anywhere in the refit is handled the
    same way. Either way the stitch publishes, the warning names the
    negative, and the old op is left exactly as it was."""
    from scanny_boy import deband

    out_dir, record, planted = _stitched_with_stale_deband(
        work_dir,
        tmp_path,
        [(0.0, 0.0, 2200.0, 790.0, 0.0), (300.0, 100.0, 900.0, 600.0, 0.0)],
        ids=[4, 5],
    )
    if failure == "crash":

        def crash(*args, **kwargs):
            raise RuntimeError("fit exploded")

        monkeypatch.setattr(deband, "fit_region", crash)
    events: list = []

    outcome = run_stitch_with_defaults(
        work_dir, out_dir, events=events, run_id="restitch-run"
    )

    assert outcome.status == "complete"
    warnings = [
        e
        for e in events
        if isinstance(e, WarningEvent) and e.code is Code.DEBAND_REFIT_FAILED
    ]
    assert len(warnings) == 1
    assert record.negative_id in warnings[0].message
    if failure == "crash":
        assert "fit exploded" in warnings[0].message
    else:
        assert "region 4" in warnings[0].message
    # Recorded nothing: the op is the planted one, stale against the new canvas.
    op = repo.net_edit_state(out_dir, record.negative_id).deband
    assert op == repo.validated_deband_params(planted)
    assert op["canvas"] == [2200, 790]
    # The negative was still published.
    assert load_roll_manifest(out_dir).negatives[0].status == "completed"
    assert any(isinstance(e, NegativeDone) for e in events)


# --- transactional roll writes (docs/TRANSACTIONAL_WRITES_PLAN.md §4.2) ----


def _add_pending_negative(out_dir: Path, negative_id: str, member: str) -> None:
    """A roll-level negative this stitch has no business touching."""
    record = NegativeRecord(
        negative_id=negative_id,
        run_id="earlier-run",
        members=[member],
        expected_output=f"{Path(member).stem}.tif",
        fill_color=stitch_pipeline.FILL_COLOR,
    )
    mutate_roll_manifest(out_dir, lambda fresh: fresh.negatives.append(record))


def test_concurrent_roll_changes_survive_every_stitch_write(
    work_dir, tmp_path, monkeypatch
):
    """The stitch keeps a working copy of the roll, but it must write only its
    own pieces. A change another writer commits between the stitch's reads
    and its writes — roll setup, a metadata field on a negative the run never
    touches, a brand-new negative with an edit — must survive the step-7
    `running` write, the publish write and the end-of-run write, and the
    stitch's own results must be intact."""
    # `previews.sync_previews` is another module's writer, covered by its own
    # tests; keep this one about the stitch's own writes.
    monkeypatch.setattr(stitch_pipeline.previews, "sync_previews", lambda *a, **k: None)
    out_dir = make_roll_dir(tmp_path)
    _add_pending_negative(out_dir, "keeper", "KEEPER.NEF")

    fired: list[str] = []

    def concurrent_before_the_running_write() -> None:
        # Solving emits progress before step 7 writes the `running` roll.
        def apply(fresh):
            fresh.setup = {"format": "35mm", "auto_crop": False}
            fresh.negative("keeper").metadata.caption = "kept by the other writer"

        mutate_roll_manifest(out_dir, apply)

    def concurrent_during_the_composite() -> None:
        # After the `running` write, before the publish write.
        def apply(fresh):
            fresh.metadata.city = "Lisbon"
            fresh.negatives.append(
                NegativeRecord(
                    negative_id="late",
                    run_id="earlier-run",
                    members=["LATE.NEF"],
                    expected_output="LATE.tif",
                    fill_color=stitch_pipeline.FILL_COLOR,
                )
            )

        mutate_roll_manifest(out_dir, apply)
        repo.append_edit(out_dir, "late", repo.FLIP_OP, {})

    def emit(event) -> None:
        if not isinstance(event, Progress):
            return
        if "solve" not in fired and event.step.value == "solve":
            fired.append("solve")
            concurrent_before_the_running_write()
        if "composite" not in fired and event.step.value == "write_stitched":
            fired.append("composite")
            concurrent_during_the_composite()

    outcome = run_stitch(
        work_dir,
        out_dir,
        run_id="stitch-run",
        overwrite=False,
        allow_partial=False,
        jobs=1,
        cancel=CancellationToken(),
        emit=emit,
    )

    assert fired == ["solve", "composite"]
    # The stitch's own results.
    assert outcome.status == "complete"
    assert outcome.published == ["IMG_00.tif"]
    roll = load_roll_manifest(out_dir)
    assert [r.status for r in roll.runs] == ["complete"]
    assert roll.runs[0].finished_at is not None
    published = next(n for n in roll.negatives if n.status == "completed")
    assert published.output["name"] == "IMG_00.tif"
    assert hashing.sha256_file(out_dir / "IMG_00.tif") == published.output["sha256"]
    assert roll.film_base["locked_at"] is not None
    # The other writer's changes.
    assert roll.setup == {"format": "35mm", "auto_crop": False}
    assert roll.negative("keeper").metadata.caption == "kept by the other writer"
    assert roll.negative("keeper").status == "pending"
    assert roll.metadata.city == "Lisbon"
    assert roll.negative("late").members == ["LATE.NEF"]
    assert [e["op"] for e in repo.edits_for(out_dir, "late")] == [repo.FLIP_OP]


def test_a_concurrent_change_survives_a_recorded_failure(tmp_path, monkeypatch):
    """`_record_failure` writes one record's failure and nothing else: a
    change committed just before it must survive."""
    work_dir = make_work_dir(
        tmp_path, negatives=2, overlapping=False, shots_per_negative=1
    )
    out_dir = make_roll_dir(tmp_path)
    _add_pending_negative(out_dir, "keeper", "KEEPER.NEF")

    real_message = stitch_pipeline._friendly_failure_message
    injected: list[str] = []

    def message_after_a_concurrent_change(*args, **kwargs):
        # `_record_failure` builds its message, then writes: this is the gap.
        if not injected:
            injected.append("done")

            def apply(fresh):
                fresh.setup = {"format": "6x7", "auto_crop": False}
                fresh.negative("keeper").metadata.caption = "kept by the other writer"

            mutate_roll_manifest(out_dir, apply)
        return real_message(*args, **kwargs)

    monkeypatch.setattr(
        stitch_pipeline, "_friendly_failure_message", message_after_a_concurrent_change
    )
    events: list = []

    outcome = run_stitch_with_defaults(work_dir, out_dir, events=events)

    assert injected == ["done"]
    # The stitch's own results: every group failed, and the run says so.
    assert outcome.status == "partial"
    assert outcome.published == []
    assert [e for e in events if isinstance(e, NegativeFailed)]
    roll = load_roll_manifest(out_dir)
    assert [r.status for r in roll.runs] == ["partial"]
    failed = [n for n in roll.negatives if n.status == "failed"]
    assert failed
    assert all(n.error_code for n in failed)
    # The other writer's changes.
    assert roll.setup == {"format": "6x7", "auto_crop": False}
    assert roll.negative("keeper").metadata.caption == "kept by the other writer"


# --- PS-2: the compose artifact (docs/PARALLEL_STITCH_PLAN.md §3.2/§3.3) ---


def _freeze_time(monkeypatch) -> None:
    """Pin `datetime.now` inside the pipeline, so two stitches of the same
    inputs write byte-identical TIFFs and equal timestamps."""
    import datetime
    import types

    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 7, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(
        stitch_pipeline,
        "datetime",
        types.SimpleNamespace(datetime=Frozen, UTC=datetime.UTC),
    )


def _copy_work(work_dir: Path, destination: Path) -> Path:
    import shutil

    shutil.copytree(work_dir, destination)
    return destination


def _run_compose(work, roll, *, events=None, cancel=None, **kwargs):
    defaults = {"run_id": "compose-run", "jobs": 1}
    defaults.update(kwargs)
    return stitch_pipeline.run_compose(
        work,
        roll,
        cancel=cancel if cancel is not None else CancellationToken(),
        emit=(events.append if events is not None else (lambda event: None)),
        **defaults,
    )


def _artifact(work: Path, group_id: str = "negative-01") -> Path:
    return work / "composed" / group_id


def _published_hashes(roll: Path) -> dict[str, str]:
    return {p.name: hashing.sha256_file(p) for p in sorted(roll.glob("*.tif"))}


# Two rolls in one library cannot share negative ids, and an id starts with
# the run's first six characters, so the stitches being compared get
# different (six-character) run ids; the dump masks them.
_PLAIN_RUN = "plain0"
_ARTIFACT_RUN = "arti01"


def _roll_dump(roll: Path, work: Path, run_id: str) -> str:
    """The roll's records as stable text, with what legitimately differs
    between two copies masked: the roll and work folder paths, the run id,
    the roll's identity and its bookkeeping timestamps."""
    import json

    manifest = load_roll_manifest(roll).to_dict()
    roll_id = manifest["roll_id"]
    for key in ("roll_id", "roll_name", "created_at", "updated_at"):
        manifest.pop(key, None)
    text = json.dumps(manifest, sort_keys=True, default=str)
    return (
        text.replace(roll_id, "<roll-id>")
        .replace(str(work), "<work>")
        .replace(str(roll), "<roll>")
        .replace(run_id, "<run>")
    )


def _stale_warnings(events) -> list[WarningEvent]:
    return [
        e
        for e in events
        if isinstance(e, WarningEvent) and e.code is Code.COMPOSE_ARTIFACT_STALE
    ]


def _plain_baseline(work_dir, tmp_path, monkeypatch):
    """A plain stitch of a private copy of `work_dir` into its own roll:
    the reference every artifact-consuming stitch must equal."""
    _freeze_time(monkeypatch)
    work = _copy_work(work_dir, tmp_path / "plain-work")
    roll = make_roll_dir(tmp_path, "plain")
    events: list = []
    assert (
        run_stitch_with_defaults(work, roll, events=events, run_id=_PLAIN_RUN).status
        == "complete"
    )
    return work, roll, events


def test_compose_only_writes_the_artifact_and_nothing_to_the_roll(work_dir, tmp_path):
    from scanny_boy.library.db import library_db_path

    roll = make_roll_dir(tmp_path, "composer")
    db_bytes = library_db_path().read_bytes()
    roll_before = load_roll_manifest(roll).to_dict()
    roll_files = sorted(p.name for p in roll.iterdir())
    events: list = []

    outcome = _run_compose(work_dir, roll, events=events)

    assert outcome.status == "complete"
    assert outcome.published == ["negative-01"]
    assert outcome.failed == []
    assert library_db_path().read_bytes() == db_bytes
    assert load_roll_manifest(roll).to_dict() == roll_before
    assert sorted(p.name for p in roll.iterdir()) == roll_files

    artifact = _artifact(work_dir)
    assert sorted(p.name for p in artifact.iterdir()) == [
        "composed.pkl",
        "covered.npy",
        "grid.npy",
        "inputs.json",
        "keep.npy",
        "log.npy",
    ]
    log = np.load(artifact / "log.npy", mmap_mode="r")
    assert log.dtype == np.float32 and log.ndim == 3
    assert not [p for p in (work_dir / "composed").iterdir() if p.name.startswith(".")]

    composed = [e for e in events if isinstance(e, stitch_pipeline.NegativeComposed)]
    assert len(composed) == 1
    assert (composed[0].group_id, composed[0].height, composed[0].width) == (
        "negative-01",
        log.shape[0],
        log.shape[1],
    )
    assert composed[0].artifact_bytes == sum(
        p.stat().st_size for p in artifact.iterdir()
    )
    steps = {e.step.value for e in events if isinstance(e, Progress)}
    assert steps == {"load", "detect", "match", "solve", "warp", "blend"}
    assert all(e.stage is Stage.STITCH for e in events if isinstance(e, Progress))
    progress = [e for e in events if isinstance(e, Progress)]
    assert progress[-1].completed == progress[-1].total


def test_a_stitch_consuming_the_artifact_matches_a_plain_stitch(
    work_dir, tmp_path, monkeypatch
):
    _plain_work, plain_roll, plain_events = _plain_baseline(
        work_dir, tmp_path, monkeypatch
    )

    work = _copy_work(work_dir, tmp_path / "composed-work")
    roll = make_roll_dir(tmp_path, "composed")
    assert _run_compose(work, roll).status == "complete"
    assert _artifact(work).is_dir()

    # The solve and the roll-independent compose must not run again.
    def forbidden(*args, **kwargs):
        raise AssertionError("the artifact should have stood in for this")

    monkeypatch.setattr(stitch_pipeline, "_solve_negative", forbidden)
    monkeypatch.setattr(stitch_pipeline, "compose_negative", forbidden)
    events: list = []
    outcome = run_stitch_with_defaults(work, roll, events=events, run_id=_ARTIFACT_RUN)

    assert outcome.status == "complete"
    assert _stale_warnings(events) == []
    assert _published_hashes(roll) == _published_hashes(plain_roll)
    assert _roll_dump(roll, work, _ARTIFACT_RUN) == _roll_dump(
        plain_roll, _plain_work, _PLAIN_RUN
    )
    # Same observable stream: every event but the roll's own names.
    assert [type(e) for e in events] == [type(e) for e in plain_events]
    progress = [e for e in events if isinstance(e, Progress)]
    assert progress[-1].completed == progress[-1].total
    assert [e.completed for e in progress] == sorted(e.completed for e in progress)
    # The artifact has done its job.
    assert not (work / "composed").exists()


def test_the_artifact_path_matches_when_the_clamp_engages(tmp_path, monkeypatch):
    """Three negatives with a clamp reference population: the artifact path's
    `finish_negative` must see exactly the references a plain stitch's
    `composite` does, in capture order."""
    from scanny_boy import normalization

    monkeypatch.setattr(normalization, "CLAMP_MIN_SAMPLES", 1)
    monkeypatch.setattr(normalization, "CLAMP_MIN_WINDOW", 0.0)
    monkeypatch.setattr(normalization, "CLAMP_K_MAD", 0.0)
    base = make_work_dir(tmp_path, negatives=3)
    _freeze_time(monkeypatch)

    plain_work = _copy_work(base, tmp_path / "plain-work")
    plain_roll = make_roll_dir(tmp_path, "plain")
    assert (
        run_stitch_with_defaults(plain_work, plain_roll, run_id=_PLAIN_RUN).status
        == "complete"
    )
    clamped = [
        n.normalization["clamped"] for n in load_roll_manifest(plain_roll).negatives
    ]
    assert any(clamped), "the clamp never engaged, so this test proves nothing"

    work = _copy_work(base, tmp_path / "composed-work")
    roll = make_roll_dir(tmp_path, "composed")
    assert _run_compose(work, roll).published == [
        "negative-01",
        "negative-02",
        "negative-03",
    ]
    events: list = []
    assert (
        run_stitch_with_defaults(work, roll, events=events, run_id=_ARTIFACT_RUN).status
        == "complete"
    )

    assert _stale_warnings(events) == []
    assert _published_hashes(roll) == _published_hashes(plain_roll)
    assert _roll_dump(roll, work, _ARTIFACT_RUN) == _roll_dump(
        plain_roll, plain_work, _PLAIN_RUN
    )


def _fingerprint_of(work: Path) -> dict:
    import json

    return json.loads((_artifact(work) / "inputs.json").read_text())


def _edit_fingerprint(work: Path, **changes) -> None:
    import json

    path = _artifact(work) / "inputs.json"
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


_CHANGED_FINGERPRINT_VALUES = {
    "compose_format_version": 0,
    "scanny_boy_version": "Scanny Boy 0.0.0",
    "work_manifest_sha256": "0" * 64,
    "group_id": "negative-77",
    "members": ["IMG_00.NEF"],
    "rig_profile_id": "some-rig",
    "rig_profile_sha256": "1" * 64,
    "stitch_params_sha256": "2" * 64,
    "film_kind": "monochrome",
    "film_base_source_sha256": "4" * 64,
    "film_base_density": [-0.1, -0.2, -0.3],
    "flat_field_gain_map_sha256": "5" * 64,
}


def test_the_fingerprint_names_every_input_the_plan_lists(work_dir, tmp_path):
    roll = make_roll_dir(tmp_path, "fp")
    _run_compose(work_dir, roll)
    fingerprint = _fingerprint_of(work_dir)
    assert set(fingerprint) == set(_CHANGED_FINGERPRINT_VALUES)
    base_block = load_roll_manifest(roll).film_base
    assert fingerprint["film_base_source_sha256"] == base_block["source_sha256"]
    assert fingerprint["film_base_density"] == base_block["density"]
    assert fingerprint["flat_field_gain_map_sha256"] is None
    assert fingerprint["rig_profile_id"] is None
    assert (
        fingerprint["compose_format_version"] == stitch_pipeline.COMPOSE_FORMAT_VERSION
    )


@pytest.mark.parametrize("field", sorted(_CHANGED_FINGERPRINT_VALUES))
def test_each_fingerprint_field_alone_makes_the_stitch_recompute(
    field, work_dir, tmp_path, monkeypatch
):
    roll = make_roll_dir(tmp_path, "fp")
    assert _run_compose(work_dir, roll).status == "complete"
    _edit_fingerprint(work_dir, **{field: _CHANGED_FINGERPRINT_VALUES[field]})

    solves: list[str] = []
    real_solve = stitch_pipeline._solve_negative

    def spy(*args, **kwargs):
        solves.append("solved")
        return real_solve(*args, **kwargs)

    monkeypatch.setattr(stitch_pipeline, "_solve_negative", spy)
    events: list = []
    outcome = run_stitch_with_defaults(work_dir, roll, events=events)

    assert outcome.status == "complete"
    assert solves == ["solved"]
    stale = _stale_warnings(events)
    assert len(stale) == 1
    assert "negative-01" in stale[0].message
    assert not (work_dir / "composed").exists()


def test_real_input_changes_make_the_stitch_recompute(work_dir, tmp_path):
    """The same check with the inputs changed for real rather than by editing
    `inputs.json`: the roll's film base, and the work manifest."""
    roll = make_roll_dir(tmp_path, "real")
    assert _run_compose(work_dir, roll).status == "complete"

    def move_density(fresh):
        fresh.film_base["density"] = [-0.5, -0.12, -0.99]

    mutate_roll_manifest(roll, move_density)
    events: list = []
    assert run_stitch_with_defaults(work_dir, roll, events=events).status == "complete"
    assert len(_stale_warnings(events)) == 1

    # A work manifest that differs by even a byte is a different input.
    other = _copy_work(work_dir, tmp_path / "other-work")
    other_roll = make_roll_dir(tmp_path, "other")
    assert _run_compose(other, other_roll).status == "complete"
    manifest_path = other / "scanny-boy-manifest.json"
    manifest_path.write_text(manifest_path.read_text() + "\n")
    events = []
    assert (
        run_stitch_with_defaults(other, other_roll, events=events).status == "complete"
    )
    assert len(_stale_warnings(events)) == 1


def test_a_different_rig_profile_makes_the_stitch_recompute(
    work_dir, tmp_path, monkeypatch
):
    from scanny_boy.calibration import RigProfile

    frame_height, frame_width = FRAME_SIZE
    geometry = {
        "format_version": 1,
        "frame_width": frame_width,
        "frame_height": frame_height,
        "fx": float(max(frame_width, frame_height)),
        "fy": float(max(frame_width, frame_height)),
        "cx": frame_width / 2.0,
        "cy": frame_height / 2.0,
        "k1": 0.0,
        "k2": 0.0,
    }
    repo.save_rig_profile(
        RigProfile(
            profile_id="pid-geo",
            name="Geo",
            scanny_boy_version="0.3.0",
            created_at="2026-09-01T00:00:00Z",
            geometry=geometry,
        )
    )
    roll = make_roll_dir(tmp_path, "rig")
    assert _run_compose(work_dir, roll, rig_profile_id="pid-geo").status == "complete"
    assert _fingerprint_of(work_dir)["rig_profile_id"] == "pid-geo"

    # Matching rig: consumed. (The roll then carries the geometry bucket.)
    solves: list[str] = []
    real_solve = stitch_pipeline._solve_negative
    monkeypatch.setattr(
        stitch_pipeline,
        "_solve_negative",
        lambda *a, **k: solves.append("x") or real_solve(*a, **k),
    )
    events: list = []
    run_stitch_with_defaults(work_dir, roll, events=events, rig_profile_id="pid-geo")
    assert solves == [] and _stale_warnings(events) == []

    # Composed with the rig, stitched without it: stale. (That stitch then
    # refuses the roll's geometry invariant, but only after the solve loop
    # would have run; use a fresh roll to isolate the artifact check.)
    work = _copy_work(work_dir, tmp_path / "again-work")
    roll2 = make_roll_dir(tmp_path, "rig-two")
    assert _run_compose(work, roll2, rig_profile_id="pid-geo").status == "complete"
    events = []
    run_stitch_with_defaults(work, roll2, events=events)
    assert len(_stale_warnings(events)) == 1


def test_a_corrupt_artifact_array_falls_back_with_identical_output(
    work_dir, tmp_path, monkeypatch
):
    plain_work, plain_roll, _ = _plain_baseline(work_dir, tmp_path, monkeypatch)

    work = _copy_work(work_dir, tmp_path / "corrupt-work")
    roll = make_roll_dir(tmp_path, "corrupt")
    assert _run_compose(work, roll).status == "complete"
    log = _artifact(work) / "log.npy"
    log.write_bytes(log.read_bytes()[:200])
    events: list = []

    assert (
        run_stitch_with_defaults(work, roll, events=events, run_id=_ARTIFACT_RUN).status
        == "complete"
    )

    assert len(_stale_warnings(events)) == 1
    assert _published_hashes(roll) == _published_hashes(plain_roll)
    assert _roll_dump(roll, work, _ARTIFACT_RUN) == _roll_dump(
        plain_roll, plain_work, _PLAIN_RUN
    )


@pytest.mark.parametrize("victim", ["composed.pkl", "covered.npy", "inputs.json"])
def test_other_unreadable_artifact_files_fall_back(work_dir, tmp_path, victim):
    roll = make_roll_dir(tmp_path, "unreadable")
    assert _run_compose(work_dir, roll).status == "complete"
    (_artifact(work_dir) / victim).write_bytes(b"not what was written")
    events: list = []

    assert run_stitch_with_defaults(work_dir, roll, events=events).status == "complete"

    assert len(_stale_warnings(events)) == 1


def test_a_fingerprint_matching_fallback_is_identical_to_a_plain_stitch(
    work_dir, tmp_path, monkeypatch
):
    plain_work, plain_roll, _ = _plain_baseline(work_dir, tmp_path, monkeypatch)

    work = _copy_work(work_dir, tmp_path / "stale-work")
    roll = make_roll_dir(tmp_path, "stale-roll")
    assert _run_compose(work, roll).status == "complete"
    _edit_fingerprint(work, film_base_source_sha256="f" * 64)
    events: list = []

    assert (
        run_stitch_with_defaults(work, roll, events=events, run_id=_ARTIFACT_RUN).status
        == "complete"
    )

    assert len(_stale_warnings(events)) == 1
    assert _published_hashes(roll) == _published_hashes(plain_roll)
    assert _roll_dump(roll, work, _ARTIFACT_RUN) == _roll_dump(
        plain_roll, plain_work, _PLAIN_RUN
    )


def test_a_failure_artifact_produces_the_same_failed_record_and_event(
    tmp_path, monkeypatch
):
    base = make_work_dir(tmp_path, overlapping=False)
    _freeze_time(monkeypatch)

    plain_work = _copy_work(base, tmp_path / "plain-work")
    plain_roll = make_roll_dir(tmp_path, "plain")
    plain_events: list = []
    plain = run_stitch_with_defaults(
        plain_work, plain_roll, events=plain_events, run_id=_PLAIN_RUN
    )
    assert plain.status == "partial"

    work = _copy_work(base, tmp_path / "composed-work")
    roll = make_roll_dir(tmp_path, "composed")
    compose_events: list = []
    composed = _run_compose(work, roll, events=compose_events)
    assert composed.status == "partial"
    assert composed.failed == ["negative-01"]
    assert (_artifact(work) / "failure.json").is_file()
    assert not (_artifact(work) / "log.npy").exists()
    compose_errors = [e for e in compose_events if type(e).__name__ == "ErrorEvent"]
    assert [e.code for e in compose_errors] == [Code.STITCH_UNDERCONSTRAINED]
    assert not [
        e for e in compose_events if isinstance(e, stitch_pipeline.NegativeComposed)
    ]

    def forbidden(*args, **kwargs):
        raise AssertionError("the failure artifact should have stood in")

    monkeypatch.setattr(stitch_pipeline, "_solve_negative", forbidden)
    events: list = []
    outcome = run_stitch_with_defaults(work, roll, events=events, run_id=_ARTIFACT_RUN)

    assert outcome.status == "partial"
    assert outcome.failed == plain.failed
    failed = [e for e in events if isinstance(e, NegativeFailed)]
    plain_failed = [e for e in plain_events if isinstance(e, NegativeFailed)]
    assert len(failed) == len(plain_failed) == 1
    assert failed[0].code is plain_failed[0].code
    assert failed[0].message.replace(_ARTIFACT_RUN, "<run>") == plain_failed[
        0
    ].message.replace(_PLAIN_RUN, "<run>")
    assert _roll_dump(roll, work, _ARTIFACT_RUN) == _roll_dump(
        plain_roll, plain_work, _PLAIN_RUN
    )
    # The solve's own warning (the CLAHE retry) is replayed by the commit.
    codes = [e.code for e in events if isinstance(e, WarningEvent)]
    plain_codes = [e.code for e in plain_events if isinstance(e, WarningEvent)]
    assert codes == plain_codes
    assert Code.STITCH_CLAHE_FALLBACK_USED in codes
    assert not (work / "composed").exists()


def test_compose_reports_insufficient_disk_before_writing_anything(
    work_dir, tmp_path, monkeypatch
):
    from scanny_boy import disk_check

    seen: list[tuple] = []

    def full(path, required):
        seen.append((path, required))
        raise disk_check.DiskCheckError(required, 0)

    monkeypatch.setattr(disk_check, "check_disk_space", full)
    roll = make_roll_dir(tmp_path, "full")

    with pytest.raises(StitchError) as exc_info:
        _run_compose(work_dir, roll)

    assert exc_info.value.code is Code.INSUFFICIENT_DISK
    assert [path for path, _ in seen] == [work_dir]
    # Sized from the artifact, not from nothing.
    assert seen[0][1] > 1024 * 1024
    assert not (work_dir / "composed").exists()


def test_compose_refuses_what_a_stitch_refuses(work_dir, tmp_path):
    roll_without_base = make_roll_dir(tmp_path, "nobase")
    mutate_roll_manifest(
        roll_without_base, lambda fresh: setattr(fresh, "film_base", None)
    )
    with pytest.raises(StitchError) as exc_info:
        _run_compose(work_dir, roll_without_base)
    assert exc_info.value.code is Code.FILM_BASE_REQUIRED

    with pytest.raises(StitchError) as exc_info:
        _run_compose(work_dir, work_dir)
    assert exc_info.value.code is Code.WORK_SAME_AS_OUTPUT

    unregistered = tmp_path / "nowhere"
    unregistered.mkdir()
    with pytest.raises(StitchError) as exc_info:
        _run_compose(work_dir, unregistered)
    assert exc_info.value.code is Code.ROLL_NOT_FOUND
    assert not (work_dir / "composed").exists()


def test_a_cancelled_compose_leaves_no_artifact(work_dir, tmp_path):
    roll = make_roll_dir(tmp_path, "cancel")
    cancel = CancellationToken()
    events: list = []

    def emit(event):
        events.append(event)
        if isinstance(event, Progress) and event.step.value == "warp":
            cancel.cancel()

    outcome = stitch_pipeline.run_compose(
        work_dir, roll, run_id="c", jobs=1, cancel=cancel, emit=emit
    )

    assert outcome.status == "cancelled"
    assert outcome.published == []
    assert not (work_dir / "composed").exists() or not list(
        (work_dir / "composed").iterdir()
    )
    assert not [e for e in events if isinstance(e, stitch_pipeline.NegativeComposed)]


def test_recomposing_replaces_an_existing_artifact(work_dir, tmp_path):
    roll = make_roll_dir(tmp_path, "twice")
    _run_compose(work_dir, roll)
    marker = _artifact(work_dir) / "stale-leftover.txt"
    marker.write_text("x")

    _run_compose(work_dir, roll)

    assert not marker.exists()
    assert (_artifact(work_dir) / "log.npy").is_file()


def test_the_solve_assigns_only_the_record_fields_the_artifact_carries(
    work_dir, tmp_path, monkeypatch
):
    """The artifact restores exactly the record fields the solve assigned,
    found by tracking assignments on the throwaway record. This pins that
    list: if a new field starts being set by `_solve_negative`, the tracker
    carries it automatically, and this test names it so the change is a
    deliberate one."""
    from scanny_boy.manifest import load_manifest

    manifest = load_manifest(work_dir)
    group = manifest.groups[0]
    record = stitch_pipeline._tracked_record(group)
    entry = stitch_pipeline._SolvedNegative(group=group, record=record, pairs=[])
    progress = stitch_pipeline._StitchProgress(
        total=100, emit=lambda event: None, run_id="r"
    )

    stitch_pipeline._solve_negative(
        work_dir,
        entry,
        grid=manifest.grid_spec,
        workers=1,
        cancel=CancellationToken(),
        progress=progress,
        source_index=0,
        on_warning=lambda code, message: None,
    )

    assert set(record.assigned_fields()) == {
        "pairs",
        "grid_cells",
        "grid_pitch_ratio",
        "grid_alignment_ratio",
    }

    # The CLAHE retry adds its flag; a fitted rectification adds its block.
    fallback_record = stitch_pipeline._tracked_record(group)
    fallback_entry = stitch_pipeline._SolvedNegative(
        group=group, record=fallback_record, pairs=[]
    )
    clahe_by_call: list[bool] = []
    real_detect_all = stitch_pipeline._detect_all

    def fake_detect_all(paths, workers, cancel, *, use_clahe):
        clahe_by_call.append(use_clahe)
        return real_detect_all(paths, workers, cancel, use_clahe=use_clahe)

    def fake_register_pair(a, b, undistorter=None):
        result = register_pair(a, b)
        if not clahe_by_call[-1]:
            return dataclasses.replace(
                result, accepted=False, reject_code=Code.STITCH_INSUFFICIENT_MATCHES
            )
        return result

    monkeypatch.setattr(stitch_pipeline, "_detect_all", fake_detect_all)
    monkeypatch.setattr(stitch_pipeline, "register_pair", fake_register_pair)
    stitch_pipeline._solve_negative(
        work_dir,
        fallback_entry,
        grid=manifest.grid_spec,
        workers=1,
        cancel=CancellationToken(),
        progress=progress,
        source_index=0,
        on_warning=lambda code, message: None,
    )
    assert "used_clahe_fallback" in fallback_record.assigned_fields()


def test_the_compose_artifact_format_covers_every_composed_negative_field():
    """`composed.pkl` is generated from the dataclass: a field added to
    `ComposedNegative` is either an array (saved as .npy, and named in
    `compose_artifact`) or travels in the pickle without further work."""
    from scanny_boy import compose_artifact
    from scanny_boy.composite import ComposedNegative

    names = {f.name for f in dataclasses.fields(ComposedNegative)}
    assert set(compose_artifact._ARRAY_FIELDS) <= names


@requires_real_samples
@pytest.mark.slow
def test_real_samples_composed_then_stitched_match_a_plain_stitch(
    tmp_path, monkeypatch
):
    """The artifact path on a real 3-frame negative: compose, then stitch from
    the artifact, equals a plain stitch of an identical work folder — same
    TIFF bytes, same roll records."""
    from scanny_boy.pipeline import run_convert
    from scanny_boy.sample_nef_support import stage_samples

    input_dir = stage_samples(tmp_path, NEGATIVE_1)
    converted = tmp_path / "converted"
    converted.mkdir()
    run_convert(
        input_dir,
        NEGATIVE_1,
        converted,
        3,
        run_id="convert-run",
        jobs=1,
        cancel=CancellationToken(),
        emit=lambda event: None,
    )
    _freeze_time(monkeypatch)

    plain_work = _copy_work(converted, tmp_path / "plain-work")
    plain_roll = make_roll_dir(tmp_path, "plain")
    assert (
        run_stitch_with_defaults(plain_work, plain_roll, run_id=_PLAIN_RUN).status
        == "complete"
    )

    work = _copy_work(converted, tmp_path / "composed-work")
    roll = make_roll_dir(tmp_path, "composed")
    assert _run_compose(work, roll, jobs=1).status == "complete"
    events: list = []
    assert (
        run_stitch_with_defaults(work, roll, events=events, run_id=_ARTIFACT_RUN).status
        == "complete"
    )

    assert _stale_warnings(events) == []
    assert _published_hashes(roll) == _published_hashes(plain_roll)
    assert _roll_dump(roll, work, _ARTIFACT_RUN) == _roll_dump(
        plain_roll, plain_work, _PLAIN_RUN
    )
    assert not (work / "composed").exists()
