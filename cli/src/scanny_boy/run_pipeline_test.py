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
from scanny_boy.disk_check import DiskCheckError
from scanny_boy.events import Code, Progress, Stage, WarningEvent
from scanny_boy.pipeline import STEPS_PER_FRAME
from scanny_boy.roll_manifest import new_roll_manifest, write_roll_manifest
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
_FILM_DATE = datetime.date(2026, 8, 2)
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
        "overwrite": False,
        "jobs": 1,
    }
    defaults.update(kwargs)
    return run_full(
        FIXTURES_DIR,
        files,
        _out_dir(tmp_path),
        _FILM_DATE,
        _PER_NEGATIVE,
        work_dir=work_dir,
        cancel=cancel if cancel is not None else CancellationToken(),
        emit=(events.append if events is not None else (lambda event: None)),
        **defaults,
    )


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
        _FILM_DATE,
        _PER_NEGATIVE,
        run_id="run-run",
        work_dir=None,
        keep_intermediates=False,
        overwrite=False,
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
        _FILM_DATE,
        _PER_NEGATIVE,
        run_id="run-run",
        work_dir=None,
        keep_intermediates=False,
        overwrite=False,
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
        _FILM_DATE,
        _PER_NEGATIVE,
        run_id="run-run",
        work_dir=None,
        keep_intermediates=False,
        overwrite=False,
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
