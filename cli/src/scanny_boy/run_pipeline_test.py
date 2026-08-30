"""`run_full`'s own job is orchestration -- calling `run_convert` then
`run_stitch`, wrapping `emit` for a combined progress span, and cleaning up
the work directory -- not RAW decoding or registration, both already
covered by `pipeline_test.py` and `stitch_pipeline_test.py`. Following
`pipeline_test.py`'s own pattern (see its module docstring): real sample
NEFs stay on disk so metadata/whitebalance consistency checks run against
real `rawpy`, but `raw_decode.decode_raw` is patched to a small, genuinely
registerable synthetic frame so a `run_full` invocation -- decode, stitch,
and all -- takes a fraction of a second instead of minutes.
"""

from __future__ import annotations

import datetime
import itertools
from pathlib import Path

import numpy as np
import pytest

from scanny_boy import raw_decode
from scanny_boy.cancellation import CancellationToken
from scanny_boy.catalogue import read_capture_timestamp
from scanny_boy.disk_check import DiskCheckError
from scanny_boy.events import Code, NegativeSuperseded, Progress, Stage, WarningEvent
from scanny_boy.pipeline import STEPS_PER_FRAME
from scanny_boy.roll_manifest import (
    load_roll_manifest,
    new_roll_manifest,
    write_roll_manifest,
)
from scanny_boy.romm import encode_from_linear
from scanny_boy.run_pipeline import (
    STITCH_UNITS_PER_FRAME,
    STITCH_UNITS_PER_NEGATIVE,
    RunFailure,
    run_full,
)
from scanny_boy.sample_nef_support import (
    FIXTURES_DIR,
    NEGATIVE_1,
    NEGATIVE_2,
    requires_real_samples,
)
from scanny_boy.synthetic_scene_support import cut_frames, synthetic_scene

_FRAME_SIZE = (700, 900)  # (height, width)
_SCENE_SIZE = (900, 2000)
_OVERLAP = 0.35
_PER_NEGATIVE = 3


def _install_fast_registerable_decode(monkeypatch, files: list[str], *, seed: int = 1) -> None:
    """Cuts one film-like scene into `len(files)` overlapping, mildly
    rotated frames -- real enough for AKAZE to register -- and serves them
    from `raw_decode.decode_raw` keyed by filename, in place of a real RAW
    decode. `files` must already be in canonical order (as `NEGATIVE_1`/
    `NEGATIVE_2` are), since that is the order `cut_frames` lays frames out
    in.
    """
    scene = synthetic_scene(*_SCENE_SIZE, seed=seed)
    rotations = [0.0, 2.0, -1.5, 1.0, -0.5, 1.8][: len(files)]
    frames, _ = cut_frames(
        scene,
        frame_size=_FRAME_SIZE,
        count=len(files),
        overlap=_OVERLAP,
        rotations_deg=rotations,
        seed=seed,
    )
    pixels_by_name = {
        name: encode_from_linear(np.stack([frame, frame, frame], axis=-1))
        for name, frame in zip(files, frames, strict=True)
    }

    def _fake_decode(path: Path) -> raw_decode.DecodedFrame:
        pixels = pixels_by_name[path.name]
        height, width = pixels.shape[0], pixels.shape[1]
        return raw_decode.DecodedFrame(pixels=pixels, width=width, height=height)

    monkeypatch.setattr(raw_decode, "decode_raw", _fake_decode)


def _out_dir(tmp_path: Path, name: str = "out") -> Path:
    """A real, empty roll to publish into.

    Section 5.4 decision 1: `stitch` -- and therefore `run`, which calls it --
    never creates a roll, so one has to exist first. `roll init` arrives in
    P3-4; until then this is `new_roll_manifest`, the same constructor it will
    call, rather than hand-authored JSON."""
    out = tmp_path / name
    out.mkdir()
    write_roll_manifest(
        out,
        new_roll_manifest(
            roll_id="00000000-0000-4000-8000-00000000000a",
            roll_name=name,
            shots_per_negative=_PER_NEGATIVE,
        ),
    )
    return out


def _run(tmp_path, monkeypatch, *, files, events=None, cancel=None, work_dir=None, **kwargs):
    _install_fast_registerable_decode(monkeypatch, files)
    defaults = {
        "run_id": "run-run",
        "keep_intermediates": False,
        "skip_sources": [],
        "jobs": 1,
    }
    defaults.update(kwargs)
    return run_full(
        FIXTURES_DIR,
        files,
        _out_dir(tmp_path),
        _PER_NEGATIVE,
        work_dir=work_dir,
        cancel=cancel if cancel is not None else CancellationToken(),
        emit=(events.append if events is not None else (lambda event: None)),
        **defaults,
    )


def _run_into_roll(
    roll_dir, monkeypatch, files, *, run_id, events=None, decode_files=None, **kwargs
):
    """Like `_run`, but into a roll the caller already created -- for tests
    that run more than once against the same roll (section 3.4's additive
    model can only be proven by a genuine second run, per section 4).

    `decode_files` lets a caller install the fake decode for a different
    (typically smaller) set than `files` — the set that will actually
    survive `skip_sources` filtering and reach `raw_decode.decode_raw` —
    so the synthetic scene it is cut from is sized for what really gets
    registered, not for the pre-skip selection.
    """
    _install_fast_registerable_decode(monkeypatch, decode_files if decode_files is not None else files)
    defaults = {"keep_intermediates": False, "skip_sources": [], "jobs": 1}
    defaults.update(kwargs)
    return run_full(
        FIXTURES_DIR,
        files,
        roll_dir,
        _PER_NEGATIVE,
        run_id=run_id,
        work_dir=None,
        cancel=CancellationToken(),
        emit=(events.append if events is not None else (lambda event: None)),
        **defaults,
    )


def _publish_and_supersede(tmp_path, monkeypatch):
    """Two genuine runs of the same roll over the identical selection: the
    second's negative covers the first's exactly, so it supersedes it
    (section 3.4). Returns `(roll_dir, old_negative_id, events_from_the_second_run)`."""
    roll_dir = _out_dir(tmp_path)
    _run_into_roll(roll_dir, monkeypatch, NEGATIVE_1, run_id="run-1")
    old_negative_id = load_roll_manifest(roll_dir).negatives[0].negative_id

    events: list = []
    _run_into_roll(roll_dir, monkeypatch, NEGATIVE_1, run_id="run-2", events=events)
    return roll_dir, old_negative_id, events


# --- cleanup ---------------------------------------------------------------


@requires_real_samples
def test_full_run_leaves_no_work_directory(tmp_path, monkeypatch):
    events: list = []
    outcome = _run(tmp_path, monkeypatch, files=NEGATIVE_1, events=events)

    assert outcome.status == "complete"
    assert outcome.work_dir_kept is False
    assert not outcome.work_dir.exists()
    assert not [
        e for e in events if isinstance(e, WarningEvent) and e.code is Code.INTERMEDIATES_KEPT
    ]


@requires_real_samples
def test_user_supplied_work_directory_is_never_deleted(tmp_path, monkeypatch):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    events: list = []
    outcome = _run(tmp_path, monkeypatch, files=NEGATIVE_1, work_dir=work_dir, events=events)

    assert outcome.status == "complete"
    assert outcome.work_dir == work_dir
    assert outcome.work_dir_kept is True
    assert work_dir.exists()
    # Section 3.5: complete success with a user-supplied --work is kept
    # silently -- no INTERMEDIATES_KEPT warning, since the user already
    # knows where it is.
    assert not [
        e for e in events if isinstance(e, WarningEvent) and e.code is Code.INTERMEDIATES_KEPT
    ]


@requires_real_samples
def test_keep_intermediates_keeps_it(tmp_path, monkeypatch):
    events: list = []
    outcome = _run(
        tmp_path, monkeypatch, files=NEGATIVE_1, events=events, keep_intermediates=True
    )

    assert outcome.status == "complete"
    assert outcome.work_dir_kept is True
    assert outcome.work_dir.exists()
    assert [
        e for e in events if isinstance(e, WarningEvent) and e.code is Code.INTERMEDIATES_KEPT
    ]


@requires_real_samples
def test_work_directory_survives_a_failed_negative(tmp_path, monkeypatch):
    """`NEGATIVE_1` and `NEGATIVE_2` are cut from one shared scene as six
    consecutive, overlapping frames, so both groups of three register and
    stitch cleanly here -- unlike `stitch_pipeline_test.py`'s dedicated
    disconnected-graph case, nothing in *this* fixture is designed to fail.
    So the failure is induced directly: the third negative's members are
    given non-overlapping content, which cannot register."""
    events: list = []
    files = NEGATIVE_1 + NEGATIVE_2

    def install(monkeypatch):
        scene = synthetic_scene(*_SCENE_SIZE, seed=3)
        good_frames, _ = cut_frames(
            scene,
            frame_size=_FRAME_SIZE,
            count=3,
            overlap=_OVERLAP,
            rotations_deg=[0.0, 2.0, -1.5],
            seed=3,
        )
        pixels_by_name = {
            name: encode_from_linear(np.stack([frame, frame, frame], axis=-1))
            for name, frame in zip(NEGATIVE_1, good_frames, strict=True)
        }
        for i, name in enumerate(NEGATIVE_2):
            # Independent scenes: no two of these share any content, so
            # every pair fails and the negative ends STITCH_UNDERCONSTRAINED.
            bad_scene = synthetic_scene(*_FRAME_SIZE, seed=100 + i)
            pixels_by_name[name] = encode_from_linear(
                np.stack([bad_scene, bad_scene, bad_scene], axis=-1)
            )

        def _fake_decode(path: Path) -> raw_decode.DecodedFrame:
            pixels = pixels_by_name[path.name]
            height, width = pixels.shape[0], pixels.shape[1]
            return raw_decode.DecodedFrame(pixels=pixels, width=width, height=height)

        monkeypatch.setattr(raw_decode, "decode_raw", _fake_decode)

    install(monkeypatch)
    outcome = run_full(
        FIXTURES_DIR,
        files,
        _out_dir(tmp_path),
        _PER_NEGATIVE,
        run_id="run-run",
        work_dir=None,
        keep_intermediates=False,
        skip_sources=[],
        jobs=1,
        cancel=CancellationToken(),
        emit=events.append,
    )

    assert outcome.status == "partial"
    assert outcome.work_dir_kept is True
    assert outcome.work_dir.exists()
    assert outcome.stitch is not None
    assert outcome.stitch.failed == ["negative-02"]
    assert [
        e for e in events if isinstance(e, WarningEvent) and e.code is Code.INTERMEDIATES_KEPT
    ]


@requires_real_samples
def test_work_directory_survives_cancellation(tmp_path, monkeypatch):
    _install_fast_registerable_decode(monkeypatch, NEGATIVE_1)
    cancel = CancellationToken()
    events: list = []

    def emit(event) -> None:
        events.append(event)
        if isinstance(event, Progress) and event.stage is Stage.CONVERT:
            cancel.cancel()

    outcome = run_full(
        FIXTURES_DIR,
        NEGATIVE_1,
        _out_dir(tmp_path),
        _PER_NEGATIVE,
        run_id="run-run",
        work_dir=None,
        keep_intermediates=False,
        skip_sources=[],
        jobs=1,
        cancel=cancel,
        emit=emit,
    )

    assert outcome.status == "cancelled"
    assert outcome.work_dir_kept is True
    assert outcome.work_dir.exists()
    assert outcome.stitch is None
    assert [
        e for e in events if isinstance(e, WarningEvent) and e.code is Code.INTERMEDIATES_KEPT
    ]


# --- the combined progress span --------------------------------------------


@requires_real_samples
def test_cancellation_during_convert_skips_stitch_entirely(tmp_path, monkeypatch):
    _install_fast_registerable_decode(monkeypatch, NEGATIVE_1)
    cancel = CancellationToken()
    events: list = []

    def emit(event) -> None:
        events.append(event)
        if isinstance(event, Progress) and event.stage is Stage.CONVERT:
            cancel.cancel()

    outcome = run_full(
        FIXTURES_DIR,
        NEGATIVE_1,
        _out_dir(tmp_path),
        _PER_NEGATIVE,
        run_id="run-run",
        work_dir=None,
        keep_intermediates=False,
        skip_sources=[],
        jobs=1,
        cancel=cancel,
        emit=emit,
    )

    assert outcome.stitch is None
    assert not [
        e for e in events if e.__class__.__name__ in ("NegativeDone", "NegativeFailed")
    ]
    assert not [e for e in events if isinstance(e, Progress) and e.stage is Stage.STITCH]


@requires_real_samples
def test_progress_total_spans_both_stages(tmp_path, monkeypatch):
    events: list = []
    _run(tmp_path, monkeypatch, files=NEGATIVE_1, events=events)

    progress = [e for e in events if isinstance(e, Progress)]
    assert progress
    totals = {e.total for e in progress}
    # Every progress event across the whole run shares the same total.
    assert len(totals) == 1
    total = totals.pop()

    frame_count = len(NEGATIVE_1)
    negative_count = frame_count // _PER_NEGATIVE
    expected = (
        frame_count * STEPS_PER_FRAME
        + STITCH_UNITS_PER_FRAME * frame_count
        + STITCH_UNITS_PER_NEGATIVE * negative_count
    )
    assert total == expected
    assert progress[-1].completed <= total


@requires_real_samples
def test_stage_transitions_exactly_once(tmp_path, monkeypatch):
    events: list = []
    _run(tmp_path, monkeypatch, files=NEGATIVE_1, events=events)

    stages = [e.stage for e in events if isinstance(e, Progress)]
    assert stages
    transitions = sum(1 for a, b in itertools.pairwise(stages) if a != b)
    assert transitions == 1
    assert stages[0] is Stage.CONVERT
    assert stages[-1] is Stage.STITCH


@requires_real_samples
def test_completed_never_decreases(tmp_path, monkeypatch):
    events: list = []
    _run(tmp_path, monkeypatch, files=NEGATIVE_1, events=events)

    completed = [e.completed for e in events if isinstance(e, Progress)]
    assert completed
    assert completed == sorted(completed)


# --- the two-volume disk check ---------------------------------------------


@requires_real_samples
def test_insufficient_work_volume_fails_before_converting(tmp_path, monkeypatch):
    import scanny_boy.disk_check as disk_check_module

    def fail(*args, **kwargs):
        raise DiskCheckError(required_bytes=10**15, available_bytes=1)

    monkeypatch.setattr(disk_check_module, "check_disk_space", fail)

    with pytest.raises(RunFailure) as exc_info:
        _run(tmp_path, monkeypatch, files=NEGATIVE_1)
    assert exc_info.value.code is Code.INSUFFICIENT_DISK


@requires_real_samples
def test_insufficient_output_volume_fails_before_stitching(tmp_path, monkeypatch):
    import scanny_boy.disk_check as disk_check_module

    real_check = disk_check_module.check_disk_space
    call_count = {"n": 0}

    def fail_on_second_call(output_dir, required_bytes):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return real_check(output_dir, required_bytes)
        raise DiskCheckError(required_bytes=10**15, available_bytes=1)

    monkeypatch.setattr(disk_check_module, "check_disk_space", fail_on_second_call)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    with pytest.raises(RunFailure) as exc_info:
        _run(tmp_path, monkeypatch, files=NEGATIVE_1, work_dir=work_dir)
    assert exc_info.value.code is Code.INSUFFICIENT_DISK
    # The convert stage's own (first) disk check really ran and passed;
    # only the stitch stage's (second) check was made to fail, so nothing
    # was published to the output folder -- proving the two checks are
    # against the two different volumes, not summed into one.
    assert call_count["n"] == 2


# =========================================================================
# Chunk P3-5: roll semantics
# =========================================================================


@requires_real_samples
def test_run_appends_to_an_existing_roll(tmp_path, monkeypatch):
    roll_dir = _out_dir(tmp_path)

    first = _run_into_roll(roll_dir, monkeypatch, NEGATIVE_1, run_id="run-1")
    assert first.status == "complete"
    second = _run_into_roll(roll_dir, monkeypatch, NEGATIVE_2, run_id="run-2")
    assert second.status == "complete"

    roll = load_roll_manifest(roll_dir)
    assert len(roll.runs) == 2
    assert {r.run_id for r in roll.runs} == {"run-1", "run-2"}
    assert len(roll.negatives) == 2
    assert {n.status for n in roll.negatives} == {"completed"}
    assert {s.filename for s in roll.sources} == {*NEGATIVE_1, *NEGATIVE_2}


@requires_real_samples
def test_run_default_work_dir_is_inside_the_roll(tmp_path, monkeypatch):
    roll_dir = _out_dir(tmp_path)

    outcome = _run_into_roll(
        roll_dir, monkeypatch, NEGATIVE_1, run_id="run-1", keep_intermediates=True
    )

    assert outcome.work_dir == roll_dir / ".work" / "run-1"
    assert outcome.work_dir.exists()


@requires_real_samples
def test_skip_sources_excludes_whole_groups(tmp_path, monkeypatch):
    roll_dir = _out_dir(tmp_path)
    files = NEGATIVE_1 + NEGATIVE_2

    # Only NEGATIVE_2 survives `--skip-sources` and actually reaches
    # `decode_raw`, so it is cut as its own independently-registerable
    # scene rather than as the tail of a six-frame one (see `decode_files`).
    outcome = _run_into_roll(
        roll_dir,
        monkeypatch,
        files,
        run_id="run-1",
        skip_sources=list(NEGATIVE_1),
        decode_files=NEGATIVE_2,
    )

    assert outcome.status == "complete"
    roll = load_roll_manifest(roll_dir)
    assert len(roll.negatives) == 1
    assert roll.negatives[0].members == list(NEGATIVE_2)


@requires_real_samples
def test_skip_sources_partial_group_is_a_usage_error(tmp_path, monkeypatch):
    roll_dir = _out_dir(tmp_path)
    files = NEGATIVE_1 + NEGATIVE_2

    # Skipping a file from the *middle* of the selection (not a whole group
    # at an edge) always leaves a gap in canonical order.
    with pytest.raises(RunFailure) as exc_info:
        _run_into_roll(
            roll_dir, monkeypatch, files, run_id="run-1", skip_sources=[NEGATIVE_1[1]]
        )
    assert exc_info.value.code is Code.NON_CONTIGUOUS_SELECTION


@requires_real_samples
def test_stitched_tiff_carries_real_capture_time(tmp_path, monkeypatch):
    roll_dir = _out_dir(tmp_path)

    outcome = _run_into_roll(roll_dir, monkeypatch, NEGATIVE_1, run_id="run-1")
    assert outcome.status == "complete"

    roll = load_roll_manifest(roll_dir)
    recorded = roll.negatives[0].capture_time.source_datetime_original
    assert recorded is not None
    recorded_dt = datetime.datetime.fromisoformat(recorded)

    expected = read_capture_timestamp(FIXTURES_DIR / NEGATIVE_1[0])
    assert expected is not None
    # Section 0: the real capture time, not Phase 1's noon-based synthetic
    # one -- compared to the second, since subsecond precision can be lost
    # round-tripping through EXIF ASCII tags and back.
    assert recorded_dt.replace(microsecond=0) == expected.when.replace(microsecond=0)
    assert recorded_dt.time() != datetime.time(12, 0, 0)


@requires_real_samples
def test_unskipped_overlap_supersedes_the_prior_negative(tmp_path, monkeypatch):
    roll_dir, old_id, events = _publish_and_supersede(tmp_path, monkeypatch)

    roll = load_roll_manifest(roll_dir)
    old = roll.negative(old_id)
    new = next(n for n in roll.negatives if n.negative_id != old_id)

    assert old.superseded_by == new.negative_id
    assert old.sequence is None
    assert new.status == "completed"
    assert new.superseded_by is None

    superseded_events = [e for e in events if isinstance(e, NegativeSuperseded)]
    assert len(superseded_events) == 1
    assert superseded_events[0].old_negative_id == old_id
    assert superseded_events[0].new_negative_id == new.negative_id


@requires_real_samples
def test_superseded_tiff_is_deleted_and_its_name_stays_claimed(tmp_path, monkeypatch):
    roll_dir, old_id, _events = _publish_and_supersede(tmp_path, monkeypatch)

    roll = load_roll_manifest(roll_dir)
    old = roll.negative(old_id)
    new = next(n for n in roll.negatives if n.negative_id != old_id)

    # The predecessor's file is gone, but its name is still on the record
    # (section 3.4) -- the replacement had to publish under a different one.
    assert not (roll_dir / old.expected_output).exists()
    assert new.output["name"] != old.expected_output
    assert (roll_dir / new.output["name"]).exists()


@requires_real_samples
def test_superseded_negative_leaves_neighbours_sequence_unchanged(tmp_path, monkeypatch):
    """Nothing computes real `sequence` values until Chunk P3-6's
    `roll_sequence.py` -- this proves the supersede step itself only nulls
    the sequence of the negative it actually covers, leaving an unrelated
    neighbour's untouched, whatever it already held."""
    roll_dir = _out_dir(tmp_path)
    _run_into_roll(roll_dir, monkeypatch, NEGATIVE_1, run_id="run-1")
    _run_into_roll(roll_dir, monkeypatch, NEGATIVE_2, run_id="run-2")

    roll = load_roll_manifest(roll_dir)
    neighbour = next(n for n in roll.negatives if n.members == list(NEGATIVE_2))
    neighbour.sequence = 7
    write_roll_manifest(roll_dir, roll)

    _run_into_roll(roll_dir, monkeypatch, NEGATIVE_1, run_id="run-3")

    roll = load_roll_manifest(roll_dir)
    unchanged = roll.negative(neighbour.negative_id)
    assert unchanged.sequence == 7
    assert unchanged.superseded_by is None


@requires_real_samples
def test_failed_supersede_delete_warns_but_does_not_fail_the_run(tmp_path, monkeypatch):
    roll_dir = _out_dir(tmp_path)
    _run_into_roll(roll_dir, monkeypatch, NEGATIVE_1, run_id="run-1")
    roll = load_roll_manifest(roll_dir)
    old_path = roll_dir / roll.negatives[0].output["name"]

    real_unlink = Path.unlink

    def _failing_unlink(self, *args, **kwargs):
        if self == old_path:
            raise OSError("simulated delete failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _failing_unlink)

    events: list = []
    outcome = _run_into_roll(roll_dir, monkeypatch, NEGATIVE_1, run_id="run-2", events=events)

    assert outcome.status == "complete"
    assert old_path.exists()  # the delete failed; the stray file remains

    warnings = [
        e
        for e in events
        if isinstance(e, WarningEvent) and e.code is Code.SUPERSEDED_FILE_NOT_REMOVED
    ]
    assert len(warnings) == 1
    superseded_events = [e for e in events if isinstance(e, NegativeSuperseded)]
    assert len(superseded_events) == 1
