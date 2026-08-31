import datetime
import json
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
import tifftools
from tifftools.constants import Tag

from scanny_boy import hashing
from scanny_boy.apply_metadata import ApplyMetadataFailure, run_apply_metadata
from scanny_boy.cancellation import CancellationToken
from scanny_boy.events import (
    Code,
    MetadataApplied,
    MetadataSkipped,
    NegativeDone,
    NegativeFailed,
    Progress,
    Stage,
)
from scanny_boy.icc_profile import PROFILE_FILENAME, PROFILE_SHA256, load_icc_profile
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
from scanny_boy.output_folder import (
    CONVERT_RULES,
    ROLL_RULES,
    OutputFolderError,
    plan_rerun,
)
from scanny_boy.registration import DETECTOR, StitchError
from scanny_boy.roll_manifest import (
    ROLL_MANIFEST_FILENAME,
    RollInvariants,
    load_roll_manifest,
    new_roll_manifest,
    write_roll_manifest,
)
from scanny_boy.roll_manifest_schema_test_support import (
    assert_matches_roll_manifest_schema,
    load_roll_manifest_schema,
)
from scanny_boy.romm import encode_from_linear
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


def _negative_frames(*, overlapping: bool, seed: int, count: int = 3) -> list[np.ndarray]:
    """`count` uint16 frames. Overlapping frames come from one scene and
    register; non-overlapping ones come from unrelated scenes and must be
    refused."""
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
    return [encode_from_linear(np.stack([f, f, f], axis=-1)) for f in frames]


def _make_work_dir(
    tmp_path: Path,
    *,
    negatives: int = 1,
    overlapping: bool = True,
    status: str = "complete",
    group_statuses: list[str] | None = None,
    film_date: str = _FILM_DATE,
    shots_per_negative: int = 3,
) -> Path:
    """A work directory holding real Phase 1 intermediates and a real
    Phase 1 manifest, built without paying for RAW decoding."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    sources: list[SourceRecord] = []
    groups: list[GroupRecord] = []
    source_order: list[str] = []

    for negative_index in range(negatives):
        frames = _negative_frames(
            overlapping=overlapping, seed=11 + negative_index * 7
        )
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

        group_status = (
            group_statuses[negative_index] if group_statuses else "completed"
        )
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
            icc_profile={"name": PROFILE_FILENAME, "sha256": PROFILE_SHA256},
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


def _roll_dir(tmp_path: Path, name: str = "out", *, shots_per_negative: int = 3) -> Path:
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
            shots_per_negative=shots_per_negative,
        ),
    )
    return roll


def _roll_invariants(work_dir: Path) -> RollInvariants:
    """The invariants a stitch of `work_dir` would present, for the tests
    that drive `plan_rerun` directly."""
    work = load_manifest(work_dir)
    return RollInvariants(
        shots_per_negative=work.shots_per_negative,
        processing_params=work.processing_params,
        icc_profile_sha256=work.icc_profile["sha256"],
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
    assert produced == ["IMG_00.tif", "IMG_10.tif", ROLL_MANIFEST_FILENAME]

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
        assert negative.superseded_by is None
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
        json.loads((out_dir / ROLL_MANIFEST_FILENAME).read_text()),
        load_roll_manifest_schema(),
    )

    done = [e for e in events if isinstance(e, NegativeDone)]
    assert len(done) == 2
    assert not [e for e in events if isinstance(e, NegativeFailed)]

    # No staging directory survives a successful run.
    assert not [p for p in out_dir.iterdir() if p.is_dir()]


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


def test_stitch_without_a_roll_manifest_is_rejected(tmp_path):
    """Section 5.4 decision 1: `stitch` never creates a roll. An empty
    directory is not one."""
    work_dir = _make_work_dir(tmp_path)
    out_dir = _out_dir(tmp_path)

    with pytest.raises(StitchError) as exc_info:
        _stitch(work_dir, out_dir)
    assert exc_info.value.code is Code.ROLL_NOT_FOUND
    assert ROLL_MANIFEST_FILENAME in exc_info.value.message
    assert not [p for p in out_dir.iterdir()]


def test_roll_manifest_of_the_wrong_version_is_rejected(tmp_path):
    """Section 0: there is no migration. A Phase 2 folder is not importable."""
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)
    path = out_dir / ROLL_MANIFEST_FILENAME
    data = json.loads(path.read_text())
    data["manifest_format_version"] = 1
    path.write_text(json.dumps(data))

    with pytest.raises(StitchError) as exc_info:
        _stitch(work_dir, out_dir)
    assert exc_info.value.code is Code.ROLL_MANIFEST_UNSUPPORTED


def test_roll_invariant_mismatch_is_rejected(tmp_path):
    """Section 3.4, replacing Phase 2's film-date mismatch test (section 5.4
    decision 3): a run whose parameters differ from the roll's invariants is
    refused, and it is `ROLL_INVARIANT_MISMATCH`, not `MANIFEST_MISMATCH` —
    that code stays with the Phase 1 work manifest."""
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path, shots_per_negative=3)
    assert _stitch(work_dir, out_dir).status == "complete"

    (tmp_path / "second").mkdir()
    other = _make_work_dir(tmp_path / "second", shots_per_negative=2)

    with pytest.raises(StitchError) as exc_info:
        _stitch(other, out_dir, run_id="stitch-run-2")
    assert exc_info.value.code is Code.ROLL_INVARIANT_MISMATCH

    # Refused before anything was published or recorded.
    roll = load_roll_manifest(out_dir)
    assert [r.run_id for r in roll.runs] == ["stitch-run"]


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
    assert seeded.icc_profile == {"name": PROFILE_FILENAME, "sha256": PROFILE_SHA256}


def test_second_stitch_publishes_under_a_suffixed_name(tmp_path):
    """Section 3.4, replacing Phase 2's overwrite-conflict test (section 5.4
    decision 3). A roll is additive: a second run publishes alongside the
    first under `-2`, and nothing is overwritten in place, ever. Two genuine
    runs, per section 4 — a hand-edited manifest would prove nothing."""
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)

    first = _stitch(work_dir, out_dir)
    second = _stitch(work_dir, out_dir, run_id="stitch-run-2")

    assert first.published == ["IMG_00.tif"]
    assert second.published == ["IMG_00-2.tif"]
    assert second.status == "complete"
    assert (out_dir / "IMG_00.tif").exists()
    assert (out_dir / "IMG_00-2.tif").exists()

    roll = load_roll_manifest(out_dir)
    assert [r.run_id for r in roll.runs] == ["stitch-run", "stitch-run-2"]
    # Section 3.4: `short_id`s are unique, so `negative_id`s are too.
    assert [n.negative_id for n in roll.negatives] == [
        "stitch-negative-01",
        "stitch-r-negative-01",
    ]
    assert all(n.status == "completed" for n in roll.negatives)
    # Nothing is superseded: P3-2 records the rule, and P3-5 executes it.
    assert all(n.superseded_by is None for n in roll.negatives)
    assert roll.negative("stitch-r-negative-01").output["name"] == "IMG_00-2.tif"
    # The first negative's name never moved.
    assert roll.negative("stitch-negative-01").output["name"] == "IMG_00.tif"


def test_negatives_filter_restricts_stitch_to_the_named_negative(tmp_path):
    """Section 3.5's `--negatives` re-stitch path: a work directory with two
    negatives, restricted to one already-published `negative_id`, publishes
    only that one — under a fresh `negative_id` of its own, per section
    3.4's additive model."""
    work_dir = _make_work_dir(tmp_path, negatives=2)
    out_dir = _roll_dir(tmp_path)

    first = _stitch(work_dir, out_dir)
    assert first.status == "complete"
    roll = load_roll_manifest(out_dir)
    target = roll.negatives[0]

    second = _stitch(work_dir, out_dir, run_id="stitch-run-2", negatives=[target.negative_id])

    assert second.status == "complete"
    assert len(second.published) == 1

    roll = load_roll_manifest(out_dir)
    republished = [n for n in roll.negatives if n.run_id == "stitch-run-2"]
    assert len(republished) == 1
    assert republished[0].members == target.members
    assert republished[0].negative_id != target.negative_id


# --- P3-8: re-apply after re-stitch (section 3.9) -------------------------

_REAPPLY_INTENDED = "2026-01-15T09:30:00.250000"


def _apply_manually(out_dir: Path, negative_id: str, intended: str = _REAPPLY_INTENDED) -> None:
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
    new = next(n for n in roll.negatives if n.negative_id != old_id)
    assert new.capture_time.intended_datetime_original == _REAPPLY_INTENDED
    assert new.capture_time.applied_datetime_original == _REAPPLY_INTENDED

    tiff_path = out_dir / new.output["name"]
    info = tifftools.read_tiff(str(tiff_path))
    exif = info["ifds"][0]["tags"][Tag.ExifIFD.value]["ifds"][0][0]["tags"]
    assert exif[DATE_TIME_ORIGINAL]["data"] == "2026:01:15 09:30:00"
    assert exif[SUBSEC_TIME_ORIGINAL]["data"] == "25"

    applied_events = [e for e in events if isinstance(e, MetadataApplied)]
    assert [e.negative_id for e in applied_events] == [new.negative_id]


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
    new = next(n for n in roll.negatives if n.negative_id != old_id)
    assert new.capture_time.intended_datetime_original is None
    assert new.capture_time.applied_datetime_original is None

    assert not [e for e in events if isinstance(e, (MetadataApplied, MetadataSkipped))]


def test_failed_reapply_leaves_negative_dirty_not_failed(tmp_path, monkeypatch):
    work_dir = _make_work_dir(tmp_path)
    out_dir = _roll_dir(tmp_path)

    first = _stitch(work_dir, out_dir)
    assert first.status == "complete"
    old_id = load_roll_manifest(out_dir).negatives[0].negative_id
    _apply_manually(out_dir, old_id)

    def _failing_rewrite(tiff_path, intended):
        raise ApplyMetadataFailure(Code.METADATA_WRITE_FAILED, "simulated rewrite failure")

    monkeypatch.setattr(
        "scanny_boy.stitch_pipeline.rewrite_date_time_original", _failing_rewrite
    )

    events: list = []
    second = _stitch(work_dir, out_dir, run_id="stitch-run-2", events=events)

    # A stitch is never failed by a metadata problem (section 3.9).
    assert second.status == "complete"

    roll = load_roll_manifest(out_dir)
    new = next(n for n in roll.negatives if n.negative_id != old_id)
    assert new.status == "completed"
    # The intent is still recorded -- it is what makes the negative dirty
    # and recoverable with Apply -- but it was never actually applied.
    assert new.capture_time.intended_datetime_original == _REAPPLY_INTENDED
    assert new.capture_time.applied_datetime_original is None

    skipped_events = [e for e in events if isinstance(e, MetadataSkipped)]
    assert len(skipped_events) == 1
    assert skipped_events[0].negative_id == new.negative_id
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
    explicit = plan_rerun(convert_out, phase_one, rules=CONVERT_RULES)
    assert implicit == explicit
    assert implicit.existing_manifest is not None
    assert sorted(implicit.conflicting_outputs) == sorted(
        phase_one.all_expected_outputs()
    )
    assert implicit.stale_outputs == []

    # The roll rules do not recognise a Phase 1 folder: there is no
    # scanny-boy-roll.json in it, so it reads as unrelated content rather
    # than as a rerun.
    roll_out = _roll_dir(tmp_path, "roll-out")
    _stitch(work_dir, roll_out)
    with pytest.raises(OutputFolderError) as exc_info:
        plan_rerun(convert_out, _roll_invariants(work_dir), rules=ROLL_RULES)
    assert exc_info.value.code is Code.OUTPUT_NOT_EMPTY

    # And a stitched folder is likewise not a Phase 1 folder.
    with pytest.raises(OutputFolderError) as exc_info:
        plan_rerun(roll_out, phase_one)
    assert exc_info.value.code is Code.OUTPUT_NOT_EMPTY
