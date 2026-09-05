import dataclasses
import datetime
import math
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
import tifftools
from tifftools.constants import Tag

from scanny_boy import hashing, registration, stitch_pipeline
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
from scanny_boy.icc_profile import ProfileKind, load_icc_profile, profile_record
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
    NegativeRecord,
    RollInvariants,
    load_roll_manifest,
    new_roll_manifest,
    write_roll_manifest,
)
from scanny_boy.roll_manifest_schema_test_support import (
    assert_matches_roll_manifest_schema,
    load_roll_manifest_schema,
)
from scanny_boy.sample_nef_support import (
    FIXTURES_DIR,
    NEGATIVE_2,
    requires_real_samples,
)
from scanny_boy.stitch_pipeline import run_stitch
from scanny_boy.synthetic_scene_support import cut_frames, synthetic_scene
from scanny_boy.tiff_exif import (
    DATE_TIME_ORIGINAL,
    SUBSEC_TIME_ORIGINAL,
    NestedExifFields,
    finalize_tiff,
)
from scanny_boy.tiff_writer import (
    BaseTiffTags,
    image_description,
    software_tag_value,
    write_base_tiff,
)

# Small enough to keep the suite fast, large enough that AKAZE clears
# MIN_PAIR_INLIERS on a film-like synthetic scene: measured 68-101 inliers
# per overlapping pair at this size and overlap.
_FRAME_SIZE = (700, 900)  # (height, width)
_SCENE_SIZE = (900, 2000)
_OVERLAP = 0.35
_FILM_DATE = "2026-08-02"


def _write_intermediate(path: Path, pixels: np.ndarray, source_name: str) -> None:
    """Write one intermediate exactly as Phase 1's pipeline does, so
    `run_stitch` reads a genuine Phase 1 output rather than a stand-in."""
    base_path = path.with_name(f"{path.stem}.base.tif")
    write_base_tiff(
        base_path,
        pixels,
        BaseTiffTags(
            description=image_description(source_name),
            software=software_tag_value(),
            conversion_time=datetime.datetime(2026, 8, 28, 10, 0, 0),  # noqa: DTZ001
            icc_profile=load_icc_profile(),
            make="NIKON CORPORATION",
            model="NIKON Z f",
        ),
    )
    finalize_tiff(
        base_path,
        path,
        NestedExifFields(
            date_time_original=datetime.datetime(2026, 8, 2, 12, 33, 41, 450000),  # noqa: DTZ001
            exposure_time=Fraction(1, 30),
            f_number=Fraction(8, 1),
            iso=100,
            focal_length=Fraction(55, 1),
            lens_model="55mm f/2.8",
            date_time_digitized="2026:08:02 12:33:41",
            subsec_time_digitized="45",
            offset_time_digitized="-05:00",
        ),
    )


def _negative_frames(
    *, overlapping: bool, seed: int, count: int = 3, frame_gains=None
) -> list[np.ndarray]:
    """`count` uint16 frames. Overlapping frames come from one scene and
    register; non-overlapping ones come from unrelated scenes and must be
    refused. `frame_gains`, when given, scales each frame's linear values
    per channel (downward only, so nothing clips) — lamp drift between
    shots."""
    if overlapping:
        scene = synthetic_scene(*_SCENE_SIZE, seed=seed)
        frames, _ = cut_frames(
            scene,
            frame_size=_FRAME_SIZE,
            count=count,
            overlap=_OVERLAP,
            rotations_deg=[0.0, 2.0, -1.5][:count],
            seed=seed,
        )
    else:
        frames = [
            synthetic_scene(*_FRAME_SIZE, seed=seed * 100 + i) for i in range(count)
        ]
    stacked = [np.stack([f, f, f], axis=-1) for f in frames]
    if frame_gains is not None:
        stacked = [
            frame * np.asarray(gain, dtype=np.float32)
            for frame, gain in zip(stacked, frame_gains, strict=True)
        ]
    return [encode_from_linear(frame.astype(np.float32)) for frame in stacked]


def _make_work_dir(
    tmp_path: Path,
    *,
    negatives: int = 1,
    overlapping: bool = True,
    status: str = "complete",
    group_statuses: list[str] | None = None,
    film_date: str = _FILM_DATE,
    shots_per_negative: int = 3,
    frame_gains: list[tuple[float, float, float]] | None = None,
    frame_hook=None,
) -> Path:
    """A work directory holding real Phase 1 intermediates and a real
    Phase 1 manifest, built without paying for RAW decoding.

    `frame_hook`, when given, is applied to every uint16 frame after the
    gains — the tilt-injection tests use it to warp each frame through a
    known W, making the true inter-frame map `W⁻¹·S·W`."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    sources: list[SourceRecord] = []
    groups: list[GroupRecord] = []
    source_order: list[str] = []

    for negative_index in range(negatives):
        frames = _negative_frames(
            overlapping=overlapping,
            seed=11 + negative_index * 7,
            frame_gains=frame_gains,
        )
        if frame_hook is not None:
            frames = [frame_hook(frame) for frame in frames]
        members: list[str] = []
        outputs: list[OutputRecord] = []
        for frame_index, pixels in enumerate(frames):
            source_name = f"IMG_{negative_index}{frame_index}.NEF"
            output_name = f"IMG_{negative_index}{frame_index}.tif"
            _write_intermediate(work_dir / output_name, pixels, source_name)
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
                    sha256=f"{negative_index}{frame_index}".ljust(64, "c"),
                )
            )

        group_status = group_statuses[negative_index] if group_statuses else "completed"
        groups.append(
            GroupRecord(
                group_id=f"negative-{negative_index + 1:02d}",
                members=members,
                expected_outputs=[f"{Path(m).stem}.tif" for m in members],
                status=group_status,
                outputs=outputs if group_status == "completed" else [],
            )
        )

    write_manifest(
        work_dir,
        Manifest(
            scanny_boy_version=current_scanny_boy_version(),
            run_id="convert-run",
            status=status,
            input_folder="/tmp/in",
            film_date=film_date,
            shots_per_negative=shots_per_negative,
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
            groups=groups,
            started_at="2026-08-02T00:00:00Z",
            finished_at="2026-08-02T00:01:00Z",
        ),
    )
    return work_dir


def _out_dir(tmp_path: Path, name: str = "out") -> Path:
    out = tmp_path / name
    out.mkdir()
    return out


def _roll_dir(tmp_path: Path, name: str = "out") -> Path:
    """A real, empty roll, written through P3-2's own writer.

    Section 5.4 decision 1: `stitch` never creates a roll, so every stitch
    test needs one to exist first. `roll init` does not arrive until P3-4, so
    this is `new_roll_manifest` — the same constructor `roll init` will call —
    and not hand-authored JSON."""
    roll = _out_dir(tmp_path, name)
    write_roll_manifest(
        roll,
        new_roll_manifest(
            roll_id=f"00000000-0000-4000-8000-0000000000{len(name):02d}",
            roll_name=name,
        ),
    )
    return roll


def _roll_invariants(work_dir: Path) -> RollInvariants:
    """The invariants a stitch of `work_dir` would present, for the tests
    that drive `plan_rerun` directly."""
    work = load_manifest(work_dir)
    return RollInvariants(
        processing_params=work.processing_params,
        icc_profile_sha256=work.icc_profile["sha256"],
        published_icc_profile_sha256=work.icc_profile["sha256"],
        stitch_params={},
    )


def _stitch(work_dir, out_dir, *, events=None, cancel=None, **kwargs):
    defaults = {
        "run_id": "stitch-run",
        "overwrite": False,
        "allow_partial": False,
        "jobs": 1,
    }
    defaults.update(kwargs)
    return run_stitch(
        work_dir,
        out_dir,
        cancel=cancel if cancel is not None else CancellationToken(),
        emit=(events.append if events is not None else (lambda event: None)),
        **defaults,
    )


# --- the happy path ------------------------------------------------------


@pytest.mark.slow
def test_end_to_end_on_real_samples(tmp_path):
    """The chunk's headline test. Real Phase 1 intermediates in, one
    stitched TIFF per negative out, named after each group's first frame,
    with a roll manifest that validates against its published schema and
    records a hash matching the file actually on disk."""
    work_dir = _make_work_dir(tmp_path, negatives=2)
    out_dir = _roll_dir(tmp_path)
    events: list = []

    outcome = _stitch(work_dir, out_dir, events=events)

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
        # Named after the group's first frame, per section 3.7.
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


# --- the rig-tilt rectification (docs/RECTIFICATION_PLAN.md section 4.2) ---


def _tilt_hook(l_x, l_y):
    """Warps every frame through W(l), making the true inter-frame map
    `W⁻¹·S·W` — the capture a tilted rig actually produces."""
    import cv2

    from scanny_boy.registration import Rectification, rectify

    rect = Rectification(
        l=np.array([l_x, l_y]),
        centre=np.array([_FRAME_SIZE[1] / 2.0, _FRAME_SIZE[0] / 2.0]),
        frame_size=_FRAME_SIZE,
        rms_before_px=1.0,
        rms_after_px=0.5,
        relative_improvement=0.5,
        pair_count=2,
    )
    height, width = _FRAME_SIZE
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
    work_dir = _make_work_dir(tmp_path, frame_hook=hook)
    out_dir = _roll_dir(tmp_path)

    outcome = _stitch(work_dir, out_dir)

    assert outcome.status == "complete"
    manifest = load_roll_manifest(out_dir)
    negative = manifest.negatives[0]
    assert negative.rectification is not None
    block = negative.rectification
    assert np.allclose(block["l"], rect.l, rtol=0.25)
    assert block["pair_count"] >= 2
    assert block["relative_improvement"] > 0.15
    assert block["rms_after_px"] < block["rms_before_px"]


def test_a_healthy_capture_stitches_without_a_rectification(tmp_path):
    """The additive guarantee: a similarity-consistent synthetic negative
    must not grow a tilt. The fit runs and is rejected by the improvement
    gate; no second pass, no manifest block."""
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)

    outcome = _stitch(work_dir, out_dir)

    assert outcome.status == "complete"
    manifest = load_roll_manifest(out_dir)
    assert manifest.negatives[0].rectification is None


def test_gain_correction_is_recorded_in_the_roll_manifest(tmp_path):
    """Lamp drift between a negative's frames is reconciled by solved
    per-frame gains, and the manifest records both the gains and the two
    overlap-MAD measurements (pre-gain explains why, post-gain is what the
    gate checks)."""
    work_dir = _make_work_dir(
        tmp_path,
        frame_gains=[(1.0, 1.0, 1.0), (0.85, 0.9, 0.95), (1.0, 1.0, 1.0)],
    )
    out_dir = _roll_dir(tmp_path)

    outcome = _stitch(work_dir, out_dir)

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
    tmp_path, monkeypatch
):
    """A stitch run through a calibrated profile must hand that profile's
    geometry to `composite`, so the warp matches the undistorted coordinates
    the solve happened in. (The profile keyword was never passed at the call
    site, so the geometry-aware warp was dead in production.)"""
    from scanny_boy import flatfield
    from scanny_boy.library import repo

    # Zero distortion: the warp is a no-op, so the synthetic scene still
    # stitches normally — only the plumbing, not the correction, is tested.
    geometry = {
        "format_version": 1,
        "frame_width": _FRAME_SIZE[0],
        "frame_height": _FRAME_SIZE[1],
        "fx": float(_FRAME_SIZE[0]),
        "fy": float(_FRAME_SIZE[0]),
        "cx": _FRAME_SIZE[0] / 2.0,
        "cy": _FRAME_SIZE[1] / 2.0,
        "k1": 0.0,
        "k2": 0.0,
    }
    path, sha256 = flatfield.save_gain_map(
        "pid-geo", np.full((8, 8, 3), 1.0, dtype=np.float32)
    )
    repo.save_flatfield_profile(
        flatfield.FlatFieldProfile(
            profile_id="pid-geo",
            name="Profile Geo",
            gain_map_path=str(path),
            gain_map_sha256=sha256,
            source_path=None,
            reference_width=_FRAME_SIZE[0],
            reference_height=_FRAME_SIZE[1],
            params=flatfield.build_params(),
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

    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)

    outcome = _stitch(work_dir, out_dir, flatfield_profile_id="pid-geo")

    assert outcome.status == "complete"
    assert captured
    assert captured[0]["geometry"] == geometry
    assert captured[0]["ca"] is None


def test_gain_drift_warning_fires_when_solved_gains_leave_unity(tmp_path, monkeypatch):
    """A solved gain far from unity means something is wrong with the
    capture: warn, by the same pattern as STITCH_SCALE_DRIFT."""
    monkeypatch.setattr(stitch_pipeline, "GAIN_DRIFT_WARN", 1e-6)
    work_dir = _make_work_dir(
        tmp_path,
        frame_gains=[(1.0, 1.0, 1.0), (0.85, 0.9, 0.95), (1.0, 1.0, 1.0)],
    )
    out_dir = _roll_dir(tmp_path)
    events: list = []

    outcome = _stitch(work_dir, out_dir, events=events)

    assert outcome.status == "complete"
    drift_warnings = [
        event
        for event in events
        if isinstance(event, WarningEvent) and event.code is Code.STITCH_GAIN_DRIFT
    ]
    assert drift_warnings
    assert any("IMG_01.tif" in event.message for event in drift_warnings)


def test_progress_events_carry_the_stitch_stage(tmp_path):
    work_dir = _make_work_dir(tmp_path, negatives=1)
    out_dir = _roll_dir(tmp_path)
    events: list = []

    _stitch(work_dir, out_dir, events=events)

    progress = [e for e in events if isinstance(e, Progress)]
    assert progress
    assert all(e.stage is Stage.STITCH for e in progress)
    completed = [e.completed for e in progress]
    assert completed == sorted(completed)
    assert progress[-1].completed <= progress[-1].total


# --- work-manifest gating ------------------------------------------------


def test_running_work_manifest_is_rejected(tmp_path):
    work_dir = _make_work_dir(tmp_path, status="running")
    out_dir = _roll_dir(tmp_path)

    with pytest.raises(StitchError) as exc_info:
        _stitch(work_dir, out_dir)
    assert exc_info.value.code is Code.WORK_MANIFEST_UNUSABLE


def test_partial_work_manifest_needs_allow_partial(tmp_path):
    work_dir = _make_work_dir(
        tmp_path, negatives=2, status="partial", group_statuses=["completed", "failed"]
    )
    out_dir = _roll_dir(tmp_path)

    with pytest.raises(StitchError) as exc_info:
        _stitch(work_dir, out_dir)
    assert exc_info.value.code is Code.WORK_MANIFEST_UNUSABLE


def test_partial_work_manifest_stitches_completed_groups_only(tmp_path):
    work_dir = _make_work_dir(
        tmp_path, negatives=2, status="partial", group_statuses=["completed", "failed"]
    )
    out_dir = _roll_dir(tmp_path)

    outcome = _stitch(work_dir, out_dir, allow_partial=True)

    assert outcome.status == "complete"
    assert outcome.published == ["IMG_00.tif"]
    manifest = load_roll_manifest(out_dir)
    assert [n.negative_id for n in manifest.negatives] == ["stitch-negative-01"]


def test_cancelled_work_manifest_is_rejected(tmp_path):
    work_dir = _make_work_dir(tmp_path, status="cancelled")
    out_dir = _roll_dir(tmp_path)

    with pytest.raises(StitchError) as exc_info:
        _stitch(work_dir, out_dir)
    assert exc_info.value.code is Code.WORK_MANIFEST_UNUSABLE


# --- intermediate verification -------------------------------------------


def test_missing_intermediate_is_caught(tmp_path):
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)
    (work_dir / "IMG_01.tif").unlink()

    with pytest.raises(StitchError) as exc_info:
        _stitch(work_dir, out_dir)
    assert exc_info.value.code is Code.INTERMEDIATE_MISSING
    assert "IMG_01.tif" in exc_info.value.message


def test_changed_intermediate_is_caught(tmp_path):
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)

    # Same byte count, different content: only the SHA-256 can catch this,
    # which is why section 3.7 requires both checks and not just the size.
    target = work_dir / "IMG_01.tif"
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))

    with pytest.raises(StitchError) as exc_info:
        _stitch(work_dir, out_dir)
    assert exc_info.value.code is Code.INTERMEDIATE_CHANGED


# --- failure, cancellation, and the output folder ------------------------


@pytest.mark.slow
def test_failing_negative_does_not_stop_the_run(tmp_path):
    """Section 3.5: a negative that cannot be stitched fails alone, the run
    continues, and the run ends `partial`."""
    good = _make_work_dir(tmp_path, negatives=1)
    # Add a second negative whose three frames share no content at all, so
    # its pair graph is disconnected and it must fail.
    bad_frames = _negative_frames(overlapping=False, seed=5)
    manifest = load_manifest(good)
    outputs = []
    members = []
    for i, pixels in enumerate(bad_frames):
        name = f"IMG_9{i}.tif"
        _write_intermediate(good / name, pixels, f"IMG_9{i}.NEF")
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

    out_dir = _roll_dir(tmp_path)
    events: list = []
    outcome = _stitch(good, out_dir, events=events)

    assert outcome.status == "partial"
    assert outcome.published == ["IMG_00.tif"]
    assert outcome.failed == ["negative-99"]

    # The good negative is published; the failed one left nothing behind.
    assert (out_dir / "IMG_00.tif").exists()
    assert not (out_dir / "IMG_90.tif").exists()
    assert not [p for p in out_dir.iterdir() if p.is_dir()]

    failures = [e for e in events if isinstance(e, NegativeFailed)]
    # The event carries the roll's `negative_id` (section 3.4), not the work
    # manifest's group id, which is what `outcome.failed` still reports.
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
        blank = np.full(_FRAME_SIZE, 0.2, dtype=np.float32)
        return [
            _encode(np.stack([blank] * 3, axis=-1).astype(np.float32))
            for _ in range(count)
        ]

    import sys

    monkeypatch.setattr(sys.modules[__name__], "_negative_frames", blank_frames)
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)

    events: list = []
    outcome = _stitch(work_dir, out_dir, events=events)

    assert outcome.status == "partial"
    failures = [e for e in events if isinstance(e, NegativeFailed)]
    assert failures
    assert failures[0].code in stitch_pipeline._CLAHE_RETRY_CODES


def test_clahe_fallback_recovers_an_underconstrained_negative(tmp_path, monkeypatch):
    """A negative whose plain-pass registration disconnects the pair graph
    is retried once with CLAHE; a graph that connects on that pass still
    stitches, and the manifest says the fallback was needed."""
    work_dir = _make_work_dir(tmp_path, negatives=1)
    out_dir = _roll_dir(tmp_path)

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
    outcome = _stitch(work_dir, out_dir, events=events)

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


def test_clahe_fallback_is_not_used_for_an_oversized_canvas(tmp_path, monkeypatch):
    """`STITCH_OUTPUT_TOO_LARGE` is not in `_CLAHE_RETRY_CODES`: a canvas
    that is already too big to write stays too big under CLAHE too, so the
    negative fails on the first pass with no retry."""
    work_dir = _make_work_dir(tmp_path, negatives=1)
    out_dir = _roll_dir(tmp_path)

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
    outcome = _stitch(work_dir, out_dir, events=events)

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

    out_dir = _roll_dir(tmp_path)
    events = []
    outcome = _stitch(work_dir, out_dir, events=events)

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


def test_cancellation_keeps_completed_negatives(tmp_path):
    """Section 3.5: a cancelled negative is abandoned, not failed — no
    `negative_failed` event, and the manifest ends `cancelled`."""
    work_dir = _make_work_dir(tmp_path, negatives=2)
    out_dir = _roll_dir(tmp_path)
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


def test_work_equal_to_out_is_rejected(tmp_path):
    work_dir = _make_work_dir(tmp_path)

    with pytest.raises(StitchError) as exc_info:
        _stitch(work_dir, work_dir)
    assert exc_info.value.code is Code.WORK_SAME_AS_OUTPUT


def test_unrelated_nonempty_output_folder_is_rejected(tmp_path):
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)
    (out_dir / "holiday-snap.jpg").write_bytes(b"not ours")

    with pytest.raises(StitchError) as exc_info:
        _stitch(work_dir, out_dir)
    assert exc_info.value.code is Code.OUTPUT_NOT_EMPTY


def test_stitch_without_a_registered_roll_is_rejected(tmp_path):
    """Section 5.4 decision 1: `stitch` never creates a roll. An empty
    directory is not one."""
    work_dir = _make_work_dir(tmp_path)
    out_dir = _out_dir(tmp_path)

    with pytest.raises(StitchError) as exc_info:
        _stitch(work_dir, out_dir)
    assert exc_info.value.code is Code.ROLL_NOT_FOUND
    assert "registered roll" in exc_info.value.message
    assert not [p for p in out_dir.iterdir()]


@pytest.mark.slow
def test_changed_shots_per_negative_is_accepted(tmp_path):
    """`shots_per_negative` is each batch's own choice, never the roll's: a
    work directory stitched at 2 scans per negative publishes into a roll
    whose earlier batches stitched at 3, with no invariant complaint."""
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)
    assert _stitch(work_dir, out_dir).status == "complete"

    (tmp_path / "second").mkdir()
    other = _make_work_dir(tmp_path / "second", shots_per_negative=2)

    assert _stitch(other, out_dir, run_id="stitch-run-2").status == "complete"

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
    roll = new_roll_manifest(roll_id="r", roll_name="r")
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
    roll.negatives.append(
        _negative_with_normalization("neg-03", "run-1", None, None)
    )
    references = stitch_pipeline._reference_bounds(roll)
    assert len(references) == 2
    assert references[0].floors == (-1.5, -1.6, -1.7)
    assert references[1].ceils == (-0.35, -0.32, -0.48)
    assert all(isinstance(b, Bounds) for b in references)


def test_roll_invariants_are_seeded_by_the_first_run(tmp_path):
    """Section 5.4 decision 1: an empty roll cannot know its
    `processing_params` or `stitch_params`, so the first run establishes
    them and every later run is compared against them."""
    out_dir = _roll_dir(tmp_path)
    empty = load_roll_manifest(out_dir)
    assert empty.processing_params == {}
    assert empty.stitch_params == {}

    _stitch(_make_work_dir(tmp_path), out_dir)

    seeded = load_roll_manifest(out_dir)
    assert seeded.processing_params == {"gamma": [1.8, 16]}
    assert seeded.stitch_params["detector"] == DETECTOR
    assert seeded.icc_profile == profile_record(ProfileKind.LINEAR)


def test_second_stitch_adopts_the_first_negative(tmp_path):
    """The replacement rule: re-stitching the same group adopts the covered
    negative in place — same `negative_id`, same output name, record
    updated with the new run's data. Two genuine runs, per section 4 — a
    hand-edited manifest would prove nothing."""
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)

    first = _stitch(work_dir, out_dir)
    second = _stitch(work_dir, out_dir, run_id="stitch-run-2")

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


def test_second_stitch_keeps_a_suffixed_name_it_adopted(tmp_path):
    """Adoption keeps whatever `expected_output` the covered negative held,
    even a `-2` suffix — the name is the negative's, not a re-derivation."""
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)

    _stitch(work_dir, out_dir)
    roll = load_roll_manifest(out_dir)
    roll.negatives[0].expected_output = "IMG_00-2.tif"
    roll.negatives[0].output["name"] = "IMG_00-2.tif"
    write_roll_manifest(out_dir, roll)
    (out_dir / "IMG_00.tif").replace(out_dir / "IMG_00-2.tif")

    second = _stitch(work_dir, out_dir, run_id="stitch-run-2")

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
    work_dir = _make_work_dir(tmp_path, negatives=2)
    out_dir = _roll_dir(tmp_path)

    first = _stitch(work_dir, out_dir)
    assert first.status == "complete"
    roll = load_roll_manifest(out_dir)
    target = roll.negatives[0]

    second = _stitch(
        work_dir, out_dir, run_id="stitch-run-2", negatives=[target.negative_id]
    )

    assert second.status == "complete"
    assert len(second.published) == 1

    roll = load_roll_manifest(out_dir)
    republished = [n for n in roll.negatives if n.run_id == "stitch-run-2"]
    assert len(republished) == 1
    assert republished[0].members == target.members
    assert republished[0].negative_id == target.negative_id


# --- P3-8: re-apply after re-stitch (section 3.9) -------------------------

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


def test_restitch_reapplies_metadata(tmp_path):
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)

    first = _stitch(work_dir, out_dir)
    assert first.status == "complete"
    old_id = load_roll_manifest(out_dir).negatives[0].negative_id
    _apply_manually(out_dir, old_id)

    events: list = []
    second = _stitch(work_dir, out_dir, run_id="stitch-run-2", events=events)
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


def test_restitch_of_never_applied_negative_does_not_apply(tmp_path):
    """No prior applied capture time to inherit -- the re-stitch is a
    no-op for metadata, and nothing dirty appears out of nowhere."""
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)

    first = _stitch(work_dir, out_dir)
    assert first.status == "complete"
    old_id = load_roll_manifest(out_dir).negatives[0].negative_id

    events: list = []
    second = _stitch(work_dir, out_dir, run_id="stitch-run-2", events=events)
    assert second.status == "complete"

    roll = load_roll_manifest(out_dir)
    adopted = roll.negative(old_id)
    assert adopted.capture_time.intended_datetime_original is None
    assert adopted.capture_time.applied_datetime_original is None

    assert not [e for e in events if isinstance(e, (MetadataApplied, MetadataSkipped))]


def test_failed_reapply_leaves_negative_dirty_not_failed(tmp_path, monkeypatch):
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)

    first = _stitch(work_dir, out_dir)
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
    second = _stitch(work_dir, out_dir, run_id="stitch-run-2", events=events)

    # A stitch is never failed by a metadata problem (section 3.9).
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


def test_phase_one_output_folder_behaviour_is_unchanged(tmp_path):
    """The explicit guard on the section 3.7 refactor: generalising
    `output_folder.py` over which manifest it reads must not change what it
    does for Phase 1's. `output_folder_test.py`, `manifest_test.py`, and
    `pipeline_test.py` all still pass unmodified; this adds the direct
    statement that the default is Phase 1's rules and that the two manifest
    kinds do not see each other's folders."""
    work_dir = _make_work_dir(tmp_path)
    convert_out = _out_dir(tmp_path, "convert-out")

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
    roll_out = _roll_dir(tmp_path, "roll-out")
    _stitch(work_dir, roll_out)
    with pytest.raises(OutputFolderError) as exc_info:
        plan_rerun(convert_out, _roll_invariants(work_dir), rules=ROLL_RULES)
    assert exc_info.value.code is Code.OUTPUT_NOT_EMPTY

    # And a stitched folder is likewise not a Phase 1 folder.
    with pytest.raises(OutputFolderError) as exc_info:
        plan_rerun(roll_out, phase_one)
    assert exc_info.value.code is Code.OUTPUT_NOT_EMPTY


# --- the memory estimate's frame_bbox_size input (GRID_STITCH_PLAN §1a) ----


def _make_grid_frames(*, across: int, down: int, seed: int = 3):
    """`across*down` uint16 frames cut from one synthetic scene at the
    2/3-step grid geometry (1/3 overlap), frames unrotated, in serpentine
    capture order (row 0 left-to-right, row 1 right-to-left, ...): index i
    sits at cell `_serpentine_cell(i, across)`."""
    frame_height, frame_width = _FRAME_SIZE
    step_x = round(frame_width * 2 / 3)
    step_y = round(frame_height * 2 / 3)
    scene = synthetic_scene(
        frame_height + (down - 1) * step_y,
        frame_width + (across - 1) * step_x,
        seed=seed,
    )
    frames = []
    for index in range(across * down):
        row, col = stitch_pipeline._serpentine_cell(index, across)
        x0 = col * step_x
        y0 = row * step_y
        patch = scene[y0 : y0 + frame_height, x0 : x0 + frame_width]
        frames.append(np.stack([patch, patch, patch], axis=-1))
    return [encode_from_linear(frame.astype(np.float32)) for frame in frames]


def _grid_pair_placements(across: int, down: int) -> dict[str, np.ndarray]:
    """Ground-truth placements matching `_make_grid_frames`' cutting, keyed
    by the intermediates' names (`IMG_<index>.tif`, serpentine order)."""
    frame_height, frame_width = _FRAME_SIZE
    step_x = round(frame_width * 2 / 3)
    step_y = round(frame_height * 2 / 3)
    placements = {}
    for index in range(across * down):
        row, col = stitch_pipeline._serpentine_cell(index, across)
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
            a.name, b.name, placements[a.name], placements[b.name], _FRAME_SIZE
        )

    return fake_detect_all, fake_register_pair


def test_grid_order_warning_fires_for_reversed_members_and_not_for_serpentine(
    tmp_path, monkeypatch
):
    """§4.4: serpentine is a documented assumption used only for the
    warning — the solved assignment always wins, and the warning names the
    frames that landed elsewhere."""
    across, down = 3, 2
    work_dir = _make_grid_work_dir(tmp_path, across=across, down=down)
    out_dir = _roll_dir(tmp_path, "gridorder")
    fake_detect_all, fake_register_pair = _grid_registration_fixtures(
        across, down
    )
    monkeypatch.setattr(stitch_pipeline, "_detect_all", fake_detect_all)
    monkeypatch.setattr(stitch_pipeline, "register_pair", fake_register_pair)

    # Serpentine order: no warning.
    events: list = []
    outcome = _stitch(work_dir, out_dir, events=events)
    order_warnings = [
        e
        for e in events
        if isinstance(e, WarningEvent)
        and e.code is Code.STITCH_GRID_ORDER_UNEXPECTED
    ]
    assert order_warnings == []
    assert outcome.published == ["IMG_00.tif"]

    # Reversed member list: the warning fires and names a frame.
    manifest = load_manifest(work_dir)
    manifest.groups[0].members.reverse()
    write_manifest(work_dir, manifest)

    events = []
    outcome = _stitch(work_dir, out_dir, run_id="stitch-run-2", events=events)
    order_warnings = [
        e
        for e in events
        if isinstance(e, WarningEvent)
        and e.code is Code.STITCH_GRID_ORDER_UNEXPECTED
    ]
    assert len(order_warnings) == 1
    assert "IMG_" in order_warnings[0].message

    # The solved assignment was recorded regardless.
    roll = load_roll_manifest(out_dir)
    negative = roll.negative("stitch-negative-01")
    assert negative.grid_cells is not None
    assert len(negative.grid_cells) == across * down


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
        _write_intermediate(work_dir / output_name, frame, source_name)
        path = work_dir / output_name
        outputs.append(
            OutputRecord(
                name=output_name, size=path.stat().st_size, sha256=hashing.sha256_file(path)
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
            film_date=_FILM_DATE,
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
    INSUFFICIENT_MEMORY (docs/GRID_STITCH_PLAN.md section 1a.4)."""
    import scanny_boy.composite as composite_module

    across, down = 5, 2
    work_dir = _make_grid_work_dir(tmp_path, across=across, down=down)
    out_dir = _roll_dir(tmp_path, "gridmem")

    # Ground-truth 5x2 geometry: 2/3 step on both axes (1/3 overlap), no
    # rotation, so the solve recovers it exactly and every frame bbox is
    # exactly one frame.
    fake_detect_all, fake_register_pair = _grid_registration_fixtures(
        across, down
    )

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
    outcome = _stitch(work_dir, out_dir, events=events)
    assert outcome.published == ["IMG_00.tif"]

    canvas_size, f_size, bbox_size, f_count = captured[0]
    # The bbox is one frame (plus at most a pixel of rotation rounding),
    # nowhere near the canvas — this is the behaviour change itself.
    assert bbox_size[0] <= f_size[0] + 2 and bbox_size[1] <= f_size[1] + 2
    assert canvas_size[0] > f_size[1] * 2 and canvas_size[1] > f_size[0]

    old_peak = real_estimate(canvas_size, f_size, (canvas_size[1], canvas_size[0]), f_count)
    new_peak = real_estimate(canvas_size, f_size, bbox_size, f_count)
    assert old_peak > new_peak * 3  # the bug is worth a large factor here

    # A second run, now with room for the true peak but not the inflated
    # one: `usable` sits strictly between the two estimates.
    monkeypatch.setattr(
        composite_module, "physical_memory_bytes", lambda: 2 * (new_peak + 1)
    )
    monkeypatch.setattr(stitch_pipeline, "estimate_peak_bytes", real_estimate)

    events = []
    outcome = _stitch(work_dir, out_dir, run_id="stitch-run-2", events=events)

    memory_failures = [
        e for e in events if isinstance(e, NegativeFailed)
        and e.code is Code.INSUFFICIENT_MEMORY
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
    inflated = estimate_peak_bytes(canvas_size, frame_size, (canvas_height, canvas_width), 10)
    assert inflated > 3 * actual  # the pre-G-0 bug charged the canvas per frame


# --- auto-rotation seeding -------------------------------------------------


def test_auto_rotation_seeds_one_fine_op_on_a_new_negative(tmp_path, monkeypatch):
    """A newly published negative gets the estimated rebate tilt seeded as
    one `rotate_fine` ops-log entry — emitted as `edit_recorded`, preview
    regenerated through the net transform that already carries it — while
    the published TIFF itself is never rotated."""
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path, "autorot")
    monkeypatch.setattr(stitch_pipeline, "estimate_rotation", lambda image: 1.5)
    events: list = []

    outcome = _stitch(work_dir, out_dir, events=events)

    assert outcome.status == "complete"
    (edit,) = repo.edits_for(out_dir, "stitch-negative-01")
    assert edit["op"] == repo.ROTATE_FINE_OP
    assert edit["params"] == {"angle_deg": 1.5, "source": "auto"}
    assert repo.net_edit_state(out_dir, "stitch-negative-01") == (0, False, 1.5, None)
    (recorded,) = [e for e in events if isinstance(e, EditRecorded)]
    assert recorded.negative_id == "stitch-negative-01"
    assert recorded.fine_rotation_deg == pytest.approx(1.5)
    assert recorded.preview_path is not None
    assert Path(recorded.preview_path).exists()

    manifest = load_roll_manifest(out_dir)
    negative = manifest.negative("stitch-negative-01")
    assert negative.preview_path == recorded.preview_path


def test_auto_rotation_seeds_nothing_when_the_estimator_declines(
    tmp_path, monkeypatch
):
    """The synthetic fixtures carry no rebate, so the real estimator
    declines; the seeded op log is empty either way."""
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path, "norebate")

    outcome = _stitch(work_dir, out_dir)

    assert outcome.status == "complete"
    assert repo.edits_for(out_dir, "stitch-negative-01") == []


def test_auto_rotation_off_seeds_nothing(tmp_path, monkeypatch):
    def _fail(image):
        raise AssertionError("the estimator must not run with auto-rotate off")

    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path, "norot")
    monkeypatch.setattr(stitch_pipeline, "estimate_rotation", _fail)

    outcome = _stitch(work_dir, out_dir, auto_rotate=False)

    assert outcome.status == "complete"
    assert repo.edits_for(out_dir, "stitch-negative-01") == []


def test_a_re_stitch_never_re_seeds_the_auto_rotation(tmp_path, monkeypatch):
    """An adopted negative keeps the rotation its first publish seeded:
    re-seeding would stack a second fine rotation on top of it."""
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path, "reseeds")
    monkeypatch.setattr(stitch_pipeline, "estimate_rotation", lambda image: 1.5)

    _stitch(work_dir, out_dir)

    def _fail(image):
        raise AssertionError("an adopted negative must not be re-seeded")

    monkeypatch.setattr(stitch_pipeline, "estimate_rotation", _fail)
    second = _stitch(work_dir, out_dir, run_id="restitch-run")

    assert second.status == "complete"
    assert len(repo.edits_for(out_dir, "stitch-negative-01")) == 1


# --- MONOCHROME_PLAN section 1: the detector pre-pass ---------------------------


def test_mono_statistic_is_recorded_for_every_sampled_negative(tmp_path):
    """§1: the pre-pass samples up to MONO_DETECT_MAX_SAMPLES negatives and
    records the statistic in each one's normalization block — sampled,
    acted on by nothing. The synthetic scenes are three copies of one
    plane, so the chroma is near zero, which is the evidence the roll
    would carry into §2's threshold decision."""
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)

    assert _stitch(work_dir, out_dir).status == "complete"

    roll = load_roll_manifest(out_dir)
    assert len(roll.negatives) == 1
    block = roll.negatives[0].normalization
    assert block is not None
    mono = block["mono"]
    assert mono["sampled"] is True
    assert isinstance(mono["chroma"], float)
    assert mono["chroma"] < 0.01
    assert len(mono["channel_correlation"]) == 2


def test_mono_sampling_spreads_across_the_run_and_caps_at_max_samples(tmp_path):
    """§1.2: six bounded reads, spread evenly across canonical order —
    with more groups than MONO_DETECT_MAX_SAMPLES, exactly that many
    distinct groups are sampled, first and last among them. Driven
    directly against a real work directory; the statistic itself is
    covered by the tests above."""
    work_dir = _make_work_dir(tmp_path, negatives=8, overlapping=False)
    manifest = load_manifest(work_dir)

    samples = stitch_pipeline._measure_mono_samples(
        work_dir, manifest.groups, CancellationToken()
    )

    assert len(samples) == stitch_pipeline.MONO_DETECT_MAX_SAMPLES
    group_ids = [g.group_id for g in manifest.groups]
    assert group_ids[0] in samples
    assert group_ids[-1] in samples
    assert set(samples) <= set(group_ids)
