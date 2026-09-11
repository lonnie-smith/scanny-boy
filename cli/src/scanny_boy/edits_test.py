"""Tests for `edit rotate`, `edit flip`, and `edit delete`: the ops log
gets the op, the preview refreshes, and the published TIFF is never touched
by rotate/flip; delete removes the record, the TIFF, and the preview.
Rotations and flips accept a selection; the whole selection is validated
before anything is recorded."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import tifffile

from scanny_boy import previews, spots
from scanny_boy.edits import (
    EditFailure,
    run_edit_color,
    run_edit_crop,
    run_edit_delete,
    run_edit_flip,
    run_edit_render_region,
    run_edit_rotate,
    run_edit_tone,
)
from scanny_boy.events import Code, WarningEvent
from scanny_boy.library import repo
from scanny_boy.roll_manifest import load_roll_manifest, write_roll_manifest
from scanny_boy.roll_manifest_test import _negative, _run
from scanny_boy.work_dir_support import make_roll_dir

_NEGATIVE_ID = "stitch-negative-01"


def _tone_params(grade_r: float = 115.0, snap_gamma: float = 0.0, **overrides: float):
    from scanny_boy import tone

    params = {
        "grade_r": grade_r,
        "snap_gamma": snap_gamma,
        "density": tone.DENSITY_REFERENCE,
        "shadow_density": 0.0,
        "highlight_density": 0.0,
        "toe": 0.0,
        "toe_width": tone.WIDTH_REFERENCE,
        "shoulder": 0.0,
        "shoulder_width": tone.WIDTH_REFERENCE,
    }
    params.update(overrides)
    return params


def _reset_tone_params():
    from scanny_boy import tone

    return {key: None for key in tone.TONE_PARAM_KEYS}


@pytest.fixture()
def stitched_roll(tmp_path: Path) -> Path:
    roll_dir = make_roll_dir(tmp_path)
    manifest = load_roll_manifest(roll_dir)
    from scanny_boy.manifest import SourceRecord
    from scanny_boy.roll_manifest import append_run, merge_sources

    append_run(manifest, _run(run_id="stitch-run", short_id="stitch"))
    merge_sources(
        manifest,
        [SourceRecord(filename="a.NEF", absolute_path="/x", size=1, mtime=1.0, sha256="a" * 64)],
        "stitch-run",
    )
    negative = _negative(
        negative_id=_NEGATIVE_ID,
        run_id="stitch-run",
        status="completed",
        sequence=1,
        output={
            "name": "_DSC0001.tif",
            "size": 0,
            "sha256": "0" * 64,
            "width": 4,
            "height": 3,
        },
    )
    manifest.negatives.append(negative)
    write_roll_manifest(roll_dir, manifest)
    tifffile.imwrite(roll_dir / "_DSC0001.tif", np.arange(12, dtype=np.uint16).reshape(3, 4))
    return roll_dir


def _append_stitched_negative(roll_dir: Path, negative_id: str, output_name: str) -> None:
    """A second completed negative, so selection-level tests have something
    to select."""
    from scanny_boy.roll_manifest import CaptureTime

    manifest = load_roll_manifest(roll_dir)
    manifest.negatives.append(
        _negative(
            negative_id=negative_id,
            run_id="stitch-run",
            status="completed",
            sequence=2,
            capture_time=CaptureTime(source_datetime_original="2026-08-01T12:00:00"),
            output={
                "name": output_name,
                "size": 0,
                "sha256": "0" * 64,
                "width": 4,
                "height": 3,
            },
        )
    )
    write_roll_manifest(roll_dir, manifest)
    tifffile.imwrite(roll_dir / output_name, np.arange(12, dtype=np.uint16).reshape(3, 4))


def _tiff_bytes(roll_dir: Path) -> bytes:
    return (roll_dir / "_DSC0001.tif").read_bytes()


def test_rotate_records_the_edit_and_reports_net_turns(stitched_roll):
    events: list = []

    (fields,) = run_edit_rotate(
        stitched_roll, _NEGATIVE_ID, "cw", emit=events.append
    )

    assert fields["rotation_quarter_turns"] == 1
    assert fields["flipped_horizontally"] is False
    assert fields["edit"]["op"] == "rotate"
    assert fields["edit"]["params"] == {"direction": "cw"}
    assert fields["preview_path"] is not None and Path(fields["preview_path"]).exists()
    # No warnings: the preview regenerated cleanly. (The `edit_recorded`
    # event itself is emitted by the CLI layer around this call.)


def test_rotate_never_touches_the_published_tiff(stitched_roll):
    before = _tiff_bytes(stitched_roll)

    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)
    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "ccw", emit=lambda event: None)

    assert _tiff_bytes(stitched_roll) == before


def test_two_rotations_compose_and_the_preview_tracks_them(stitched_roll):
    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)
    (fields,) = run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)

    assert fields["rotation_quarter_turns"] == 2
    assert fields["flipped_horizontally"] is False
    manifest = load_roll_manifest(stitched_roll)
    assert manifest.negative(_NEGATIVE_ID).preview_path == fields["preview_path"]


def test_flip_records_the_edit_and_reports_the_net_state(stitched_roll):
    (fields,) = run_edit_flip(stitched_roll, _NEGATIVE_ID, emit=lambda event: None)

    assert fields["edit"]["op"] == "flip"
    assert fields["edit"]["params"] == {}
    assert fields["rotation_quarter_turns"] == 0
    assert fields["flipped_horizontally"] is True
    assert fields["preview_path"] is not None and Path(fields["preview_path"]).exists()


def test_flip_never_touches_the_published_tiff(stitched_roll):
    before = _tiff_bytes(stitched_roll)

    run_edit_flip(stitched_roll, _NEGATIVE_ID, emit=lambda event: None)

    assert _tiff_bytes(stitched_roll) == before


def test_flip_and_rotate_do_not_commute(stitched_roll):
    """A flip applies to the pixels as they currently render, so flip-then-
   -rotate and rotate-then-flip are different images — the reason the ops
    log reduces to a (turns, flipped) pair rather than a single number."""
    from scanny_boy.library import repo

    _append_stitched_negative(stitched_roll, "stitch-negative-02", "_DSC0004.tif")

    run_edit_flip(stitched_roll, _NEGATIVE_ID, emit=lambda event: None)
    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)

    run_edit_rotate(stitched_roll, "stitch-negative-02", "cw", emit=lambda event: None)
    run_edit_flip(stitched_roll, "stitch-negative-02", emit=lambda event: None)

    # flip∘rot-cw ≠ rot-cw∘flip: the mirrored image ends up turned the other
    # way relative to the mirror.
    assert repo.net_edit_state(stitched_roll, _NEGATIVE_ID) == repo.EditState(
        1, True, 0.0, None, None
    )
    assert repo.net_edit_state(stitched_roll, "stitch-negative-02") == repo.EditState(
        3, True, 0.0, None, None
    )


def test_two_flips_cancel(stitched_roll):
    run_edit_flip(stitched_roll, _NEGATIVE_ID, emit=lambda event: None)
    (fields,) = run_edit_flip(stitched_roll, _NEGATIVE_ID, emit=lambda event: None)

    assert fields["rotation_quarter_turns"] == 0
    assert fields["flipped_horizontally"] is False


def test_selection_rotate_records_one_edit_per_negative(stitched_roll):
    _append_stitched_negative(stitched_roll, "stitch-negative-02", "_DSC0004.tif")

    results = run_edit_rotate(
        stitched_roll,
        [_NEGATIVE_ID, "stitch-negative-02"],
        "cw",
        emit=lambda event: None,
    )

    from scanny_boy.library import repo

    assert [r["negative_id"] for r in results] == [
        _NEGATIVE_ID,
        "stitch-negative-02",
    ]
    assert all(r["rotation_quarter_turns"] == 1 for r in results)
    assert all(
        repo.net_rotation_quarter_turns(stitched_roll, nid) == 1
        for nid in (_NEGATIVE_ID, "stitch-negative-02")
    )


def test_selection_flip_records_one_edit_per_negative(stitched_roll):
    _append_stitched_negative(stitched_roll, "stitch-negative-02", "_DSC0004.tif")

    results = run_edit_flip(
        stitched_roll,
        ["stitch-negative-02", _NEGATIVE_ID],
        emit=lambda event: None,
    )

    assert [r["negative_id"] for r in results] == [
        "stitch-negative-02",
        _NEGATIVE_ID,
    ]
    assert all(r["flipped_horizontally"] is True for r in results)


def test_a_bad_selection_records_nothing(stitched_roll):
    """The whole selection is validated before any op is appended, so one
    bad id means the good negatives are not half-edited either."""
    from scanny_boy.library import repo

    with pytest.raises(EditFailure):
        run_edit_rotate(
            stitched_roll,
            [_NEGATIVE_ID, "nope-negative-99"],
            "cw",
            emit=lambda event: None,
        )

    assert repo.edits_for(stitched_roll, _NEGATIVE_ID) == []


def test_rotate_the_wrong_direction_is_a_usage_failure(stitched_roll):
    with pytest.raises(EditFailure) as exc_info:
        run_edit_rotate(stitched_roll, _NEGATIVE_ID, "north", emit=lambda event: None)
    assert exc_info.value.code is Code.INVALID_EDIT


def test_rotate_an_unknown_negative_fails(stitched_roll):
    with pytest.raises(EditFailure) as exc_info:
        run_edit_rotate(
            stitched_roll, "nope-negative-99", "cw", emit=lambda event: None
        )
    assert exc_info.value.code is Code.NEGATIVE_NOT_FOUND


def test_rotate_rejects_another_rolls_negative_id(tmp_path):
    """`negative_id` is a global primary key and the argument is
    user-supplied: roll B's folder with roll A's negative id must be
    refused, not silently edit roll A's negative."""
    from scanny_boy.library import repo
    from scanny_boy.roll_manifest import new_roll_manifest

    roll_a = tmp_path / "roll-a"
    roll_b = tmp_path / "roll-b"
    for roll_dir, roll_id, negative_id in (
        (roll_a, "rid-a", "aaa-negative-01"),
        (roll_b, "rid-b", "bbb-negative-01"),
    ):
        roll_dir.mkdir()
        manifest = new_roll_manifest(roll_id=roll_id, roll_name=roll_dir.name, film_kind="colour")
        manifest.negatives.append(_negative(negative_id=negative_id, run_id="run-1"))
        write_roll_manifest(roll_dir, manifest)

    with pytest.raises(repo.RollNotRegisteredError):
        repo.append_edit(
            roll_b, "aaa-negative-01", repo.ROTATE_OP, {"direction": "cw"}
        )

    with pytest.raises(EditFailure):
        run_edit_rotate(roll_b, "aaa-negative-01", "cw", emit=lambda event: None)

    # Roll A's ops log is untouched.
    assert repo.edits_for(roll_a, "aaa-negative-01") == []


def test_rotate_an_unstitched_negative_fails(stitched_roll):
    manifest = load_roll_manifest(stitched_roll)
    manifest.negatives.append(
        _negative(negative_id="stitch-negative-02", run_id="stitch-run")
    )
    write_roll_manifest(stitched_roll, manifest)

    with pytest.raises(EditFailure) as exc_info:
        run_edit_rotate(stitched_roll, "stitch-negative-02", "cw", emit=lambda event: None)
    assert exc_info.value.code is Code.NEGATIVE_NOT_FOUND


def test_roll_info_reports_the_net_rotation(stitched_roll):
    from scanny_boy.library import repo

    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "ccw", emit=lambda event: None)

    manifest = load_roll_manifest(stitched_roll)
    assert (
        repo.net_rotation_quarter_turns(stitched_roll, _NEGATIVE_ID) == 3
    )
    assert manifest.negative(_NEGATIVE_ID).preview_path is not None


# --- edit tone --------------------------------------------------------------


def _ramp_tiff(stitched_roll: Path) -> None:
    """Rewrites the fixture TIFF with codes spanning the whole normalized
    range — `np.arange(12)` sits entirely in the encode's headroom, where
    every display value clips to white and no tone curve can show."""
    ramp = np.linspace(0, 65535, 12, dtype=np.uint16).reshape(3, 4)
    tifffile.imwrite(stitched_roll / "_DSC0001.tif", ramp)


def test_tone_records_the_state_and_changes_the_preview(stitched_roll):
    from scanny_boy import previews
    from scanny_boy.library import repo

    _ramp_tiff(stitched_roll)

    (fields,) = run_edit_tone(
        stitched_roll, _NEGATIVE_ID, _tone_params(90.0, 0.2), emit=lambda event: None
    )

    assert fields["edit"]["op"] == "tone"
    assert fields["edit"]["params"] == _tone_params(90.0, 0.2)
    assert fields["preview_path"] is not None and Path(fields["preview_path"]).exists()
    assert repo.net_edit_state(stitched_roll, _NEGATIVE_ID) == repo.EditState(
        quarter_turns=0,
        flipped=False,
        fine_angle_deg=0.0,
        tone=_tone_params(90.0, 0.2),
        color=None,
    )

    # The toned preview differs from the flat look the same TIFF renders
    # with no tone params.
    manifest = load_roll_manifest(stitched_roll)
    negative = manifest.negative(_NEGATIVE_ID)
    toned = Path(negative.preview_path).read_bytes()
    flat = previews.generate_preview(
        stitched_roll,
        manifest.roll_id,
        negative,
        tone_params=None,
    )
    assert toned != flat.read_bytes()


def test_tone_coalesces_repeated_commits(stitched_roll):
    from scanny_boy.library import repo

    run_edit_tone(
        stitched_roll, _NEGATIVE_ID, _tone_params(115.0, 0.0), emit=lambda event: None
    )
    (fields,) = run_edit_tone(
        stitched_roll, _NEGATIVE_ID, _tone_params(80.0, 0.3), emit=lambda event: None
    )

    edits = repo.edits_for(stitched_roll, _NEGATIVE_ID)
    assert len(edits) == 1
    assert edits[0]["id"] == fields["edit"]["id"]
    assert edits[0]["params"] == _tone_params(80.0, 0.3)


def test_tone_reset_returns_to_the_flat_look(stitched_roll):
    from scanny_boy import previews

    run_edit_tone(
        stitched_roll, _NEGATIVE_ID, _tone_params(90.0, 0.2), emit=lambda event: None
    )

    (fields,) = run_edit_tone(
        stitched_roll, _NEGATIVE_ID, _reset_tone_params(), emit=lambda event: None
    )

    assert fields["edit"]["params"] == _reset_tone_params()
    manifest = load_roll_manifest(stitched_roll)
    negative = manifest.negative(_NEGATIVE_ID)
    reset_preview = Path(negative.preview_path).read_bytes()
    flat = previews.generate_preview(
        stitched_roll,
        manifest.roll_id,
        negative,
        tone_params=None,
    )
    assert reset_preview == flat.read_bytes()
    assert repo.net_edit_state(stitched_roll, _NEGATIVE_ID).tone is None


def test_tone_never_touches_the_published_tiff(stitched_roll):
    before = _tiff_bytes(stitched_roll)

    run_edit_tone(
        stitched_roll, _NEGATIVE_ID, _tone_params(70.0, 0.4), emit=lambda event: None
    )
    run_edit_tone(
        stitched_roll, _NEGATIVE_ID, _reset_tone_params(), emit=lambda event: None
    )

    assert _tiff_bytes(stitched_roll) == before


def test_tone_composes_with_the_geometric_ops(stitched_roll):
    from scanny_boy.library import repo

    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)
    run_edit_tone(
        stitched_roll, _NEGATIVE_ID, _tone_params(70.0, 0.4), emit=lambda event: None
    )

    assert repo.net_edit_state(stitched_roll, _NEGATIVE_ID) == repo.EditState(
        quarter_turns=1,
        flipped=False,
        fine_angle_deg=0.0,
        tone=_tone_params(70.0, 0.4),
        color=None,
    )


def test_tone_rejects_bad_params_without_recording(stitched_roll):
    with pytest.raises(EditFailure) as exc_info:
        run_edit_tone(
            stitched_roll, _NEGATIVE_ID, _tone_params(900.0, 0.0), emit=lambda event: None
        )
    assert exc_info.value.code is Code.INVALID_EDIT

    partial = _tone_params(115.0, 0.0)
    partial["snap_gamma"] = None
    with pytest.raises(EditFailure):
        run_edit_tone(stitched_roll, _NEGATIVE_ID, partial, emit=lambda event: None)

    assert repo.edits_for(stitched_roll, _NEGATIVE_ID) == []


def test_render_region_applies_the_recorded_tone(stitched_roll, tmp_path):
    _ramp_tiff(stitched_roll)
    run_edit_tone(
        stitched_roll, _NEGATIVE_ID, _tone_params(70.0, 0.3), emit=lambda event: None
    )

    toned = tmp_path / "toned.png"
    flat = tmp_path / "flat.png"
    run_edit_render_region(
        stitched_roll,
        _NEGATIVE_ID,
        0,
        0,
        4,
        3,
        toned,
        emit=lambda event: None,
    )
    previews.render_region(
        stitched_roll / "_DSC0001.tif", 0, 0, 4, 3, destination=flat
    )

    assert toned.read_bytes() != flat.read_bytes()


# --- edit color --------------------------------------------------------------


def _color_params(**overrides: float):
    import dataclasses

    from scanny_boy import color

    params = dataclasses.asdict(color.NEUTRAL_COLOR)
    params.update(overrides)
    return params


def _reset_color_params():
    from scanny_boy import color

    return {key: None for key in color.COLOR_PARAM_KEYS}


def test_color_records_the_state_and_changes_the_preview(stitched_roll):
    from scanny_boy import previews
    from scanny_boy.library import repo

    _ramp_tiff(stitched_roll)
    params = _color_params(wb_cyan=0.1, dye_separation=1.2)

    (fields,) = run_edit_color(
        stitched_roll, _NEGATIVE_ID, params, emit=lambda event: None
    )

    assert fields["edit"]["op"] == "color"
    assert fields["edit"]["params"] == params
    assert fields["preview_path"] is not None and Path(fields["preview_path"]).exists()
    assert repo.net_edit_state(stitched_roll, _NEGATIVE_ID) == repo.EditState(
        quarter_turns=0,
        flipped=False,
        fine_angle_deg=0.0,
        tone=None,
        color=params,
    )

    manifest = load_roll_manifest(stitched_roll)
    negative = manifest.negative(_NEGATIVE_ID)
    coloured = Path(negative.preview_path).read_bytes()
    flat = previews.generate_preview(
        stitched_roll,
        manifest.roll_id,
        negative,
        tone_params=None,
        color_params=None,
    )
    assert coloured != flat.read_bytes()


def test_color_partial_update_leaves_other_values(stitched_roll):
    from scanny_boy.library import repo

    base = _color_params(wb_cyan=0.1, wb_magenta=0.2, cast_removal=0.3)
    run_edit_color(stitched_roll, _NEGATIVE_ID, base, emit=lambda event: None)

    (fields,) = run_edit_color(
        stitched_roll,
        _NEGATIVE_ID,
        {"wb_cyan": 0.5},
        emit=lambda event: None,
    )

    expected = dict(base)
    expected["wb_cyan"] = 0.5
    assert fields["edit"]["params"] == expected
    assert repo.net_edit_state(stitched_roll, _NEGATIVE_ID).color == expected


def test_color_temperature_moves_only_the_named_region(stitched_roll):
    from scanny_boy import color
    from scanny_boy.library import repo

    run_edit_color(
        stitched_roll,
        _NEGATIVE_ID,
        _color_params(wb_magenta=0.1, wb_yellow=0.05),
        emit=lambda event: None,
    )

    run_edit_color(
        stitched_roll,
        _NEGATIVE_ID,
        {},
        temperature=3200.0,
        region="shadows",
        emit=lambda event: None,
    )

    state = repo.net_edit_state(stitched_roll, _NEGATIVE_ID).color
    assert state is not None
    assert state["wb_magenta"] == pytest.approx(0.1)
    assert state["wb_yellow"] == pytest.approx(0.05)
    shadow_m, shadow_y = color.kelvin_to_wb(3200.0, 0.0, 0.0)
    assert state["shadow_magenta"] == pytest.approx(shadow_m)
    assert state["shadow_yellow"] == pytest.approx(shadow_y)


def test_color_reset_returns_to_the_neutral_preview(stitched_roll):
    from scanny_boy import previews

    run_edit_color(
        stitched_roll,
        _NEGATIVE_ID,
        _color_params(wb_cyan=0.2),
        emit=lambda event: None,
    )

    (fields,) = run_edit_color(
        stitched_roll,
        _NEGATIVE_ID,
        None,
        reset=True,
        emit=lambda event: None,
    )

    assert fields["edit"]["params"] == _reset_color_params()
    manifest = load_roll_manifest(stitched_roll)
    negative = manifest.negative(_NEGATIVE_ID)
    reset_preview = Path(negative.preview_path).read_bytes()
    flat = previews.generate_preview(
        stitched_roll,
        manifest.roll_id,
        negative,
        tone_params=None,
        color_params=None,
    )
    assert reset_preview == flat.read_bytes()
    assert repo.net_edit_state(stitched_roll, _NEGATIVE_ID).color is None


def test_color_never_touches_the_published_tiff(stitched_roll):
    before = _tiff_bytes(stitched_roll)

    run_edit_color(
        stitched_roll,
        _NEGATIVE_ID,
        _color_params(wb_magenta=0.3),
        emit=lambda event: None,
    )
    run_edit_color(
        stitched_roll,
        _NEGATIVE_ID,
        None,
        reset=True,
        emit=lambda event: None,
    )

    assert _tiff_bytes(stitched_roll) == before


def test_color_refused_on_monochrome_roll_except_reset(stitched_roll):
    run_edit_color(
        stitched_roll,
        _NEGATIVE_ID,
        _color_params(wb_cyan=0.1),
        emit=lambda event: None,
    )
    manifest = load_roll_manifest(stitched_roll)
    manifest.film = {
        "kind": "monochrome",
        "source": "manual",
        "statistic": None,
        "samples": [],
        "detector_version": 1,
    }
    write_roll_manifest(stitched_roll, manifest)

    with pytest.raises(EditFailure) as exc_info:
        run_edit_color(
            stitched_roll,
            _NEGATIVE_ID,
            _color_params(wb_cyan=0.2),
            emit=lambda event: None,
        )
    assert exc_info.value.code is Code.INVALID_EDIT

    run_edit_color(
        stitched_roll,
        _NEGATIVE_ID,
        None,
        reset=True,
        emit=lambda event: None,
    )
    assert repo.net_edit_state(stitched_roll, _NEGATIVE_ID).color is None


def test_color_cast_removal_warns_without_metering(stitched_roll):
    events: list = []

    results = run_edit_color(
        stitched_roll,
        _NEGATIVE_ID,
        _color_params(cast_removal=0.5),
        emit=events.append,
    )

    assert results[0]["edit"]["params"]["cast_removal"] == 0.5
    warnings = [e for e in events if isinstance(e, WarningEvent)]
    assert any(w.code is Code.TONE_METERING_UNAVAILABLE for w in warnings)


def test_color_composes_with_tone(stitched_roll):
    from scanny_boy.library import repo

    run_edit_tone(
        stitched_roll, _NEGATIVE_ID, _tone_params(70.0, 0.4), emit=lambda event: None
    )
    run_edit_color(
        stitched_roll,
        _NEGATIVE_ID,
        _color_params(wb_yellow=0.2),
        emit=lambda event: None,
    )

    state = repo.net_edit_state(stitched_roll, _NEGATIVE_ID)
    assert state.tone == _tone_params(70.0, 0.4)
    assert state.color == _color_params(wb_yellow=0.2)


# --- edit delete -----------------------------------------------------------


def test_delete_removes_the_record_the_tiff_and_the_preview(stitched_roll):
    events: list = []
    # A rotation first, so a preview PNG exists to be removed too.
    rotated = run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)
    preview_path = Path(rotated[0]["preview_path"])
    assert preview_path.exists()
    tiff_path = stitched_roll / "_DSC0001.tif"
    assert tiff_path.exists()

    (fields,) = run_edit_delete(stitched_roll, _NEGATIVE_ID, emit=events.append)

    assert fields == {"negative_id": _NEGATIVE_ID, "output": "_DSC0001.tif"}
    assert not tiff_path.exists()
    assert not preview_path.exists()
    manifest = load_roll_manifest(stitched_roll)
    with pytest.raises(KeyError):
        manifest.negative(_NEGATIVE_ID)
    # No warnings: every file came off the disk cleanly. (The
    # `negative_deleted` event itself is emitted by the CLI layer.)
    assert [e for e in events if isinstance(e, WarningEvent)] == []


def test_delete_cascades_the_ops_log(stitched_roll):
    from scanny_boy.library import repo

    run_edit_rotate(stitched_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)

    run_edit_delete(stitched_roll, _NEGATIVE_ID, emit=lambda event: None)

    with pytest.raises(repo.RollNotRegisteredError):
        repo.edits_for(stitched_roll, _NEGATIVE_ID)


def test_delete_renumbers_the_survivors(stitched_roll):
    from scanny_boy.roll_manifest import CaptureTime

    manifest = load_roll_manifest(stitched_roll)
    # Ranking is by the first member's real capture time, so the survivor
    # needs one to hold a position at all.
    manifest.negatives.append(
        _negative(
            negative_id="stitch-negative-02",
            run_id="stitch-run",
            status="completed",
            sequence=2,
            capture_time=CaptureTime(source_datetime_original="2026-08-01T12:00:00"),
            output={
                "name": "_DSC0004.tif",
                "size": 0,
                "sha256": "0" * 64,
                "width": 4,
                "height": 3,
            },
        )
    )
    write_roll_manifest(stitched_roll, manifest)

    run_edit_delete(stitched_roll, _NEGATIVE_ID, emit=lambda event: None)

    survivor = load_roll_manifest(stitched_roll).negative("stitch-negative-02")
    assert survivor.sequence == 1


def test_delete_a_pending_negative_succeeds_without_files(stitched_roll):
    manifest = load_roll_manifest(stitched_roll)
    manifest.negatives.append(
        _negative(negative_id="stitch-negative-02", run_id="stitch-run")
    )
    write_roll_manifest(stitched_roll, manifest)

    (fields,) = run_edit_delete(
        stitched_roll, "stitch-negative-02", emit=lambda event: None
    )

    assert fields == {"negative_id": "stitch-negative-02", "output": None}


def test_selection_delete_removes_every_selected_negative(stitched_roll):
    _append_stitched_negative(stitched_roll, "stitch-negative-02", "_DSC0004.tif")

    results = run_edit_delete(
        stitched_roll,
        [_NEGATIVE_ID, "stitch-negative-02"],
        emit=lambda event: None,
    )

    assert [r["negative_id"] for r in results] == [
        _NEGATIVE_ID,
        "stitch-negative-02",
    ]
    assert not (stitched_roll / "_DSC0001.tif").exists()
    assert not (stitched_roll / "_DSC0004.tif").exists()
    assert load_roll_manifest(stitched_roll).negatives == []


def test_selection_delete_validates_the_whole_selection_first(stitched_roll):
    _append_stitched_negative(stitched_roll, "stitch-negative-02", "_DSC0004.tif")

    with pytest.raises(EditFailure):
        run_edit_delete(
            stitched_roll,
            [_NEGATIVE_ID, "nope-negative-99"],
            emit=lambda event: None,
        )

    # Nothing was removed: the good negative and both TIFFs survive.
    assert load_roll_manifest(stitched_roll).negative(_NEGATIVE_ID) is not None
    assert (stitched_roll / "_DSC0001.tif").exists()


def test_delete_an_unknown_negative_fails(stitched_roll):
    with pytest.raises(EditFailure) as exc_info:
        run_edit_delete(stitched_roll, "nope-negative-99", emit=lambda event: None)
    assert exc_info.value.code is Code.NEGATIVE_NOT_FOUND


def test_delete_an_unregistered_roll_fails(tmp_path: Path):
    with pytest.raises(EditFailure) as exc_info:
        run_edit_delete(tmp_path / "no-such-roll", "any-negative", emit=lambda event: None)
    assert exc_info.value.code is Code.ROLL_NOT_FOUND


def test_delete_survives_a_stuck_tiff(stitched_roll, monkeypatch):
    def stuck_unlink(self, missing_ok=False):
        raise PermissionError(1, "Operation not permitted", str(self))

    monkeypatch.setattr(Path, "unlink", stuck_unlink)
    events: list = []

    (fields,) = run_edit_delete(stitched_roll, _NEGATIVE_ID, emit=events.append)

    # The record is gone regardless; the file left behind is reported, not fatal.
    assert fields == {"negative_id": _NEGATIVE_ID, "output": "_DSC0001.tif"}
    assert load_roll_manifest(stitched_roll).negatives == []
    warnings = [e for e in events if isinstance(e, WarningEvent)]
    assert [w.code for w in warnings] == [Code.ORPHAN_FILE_NOT_REMOVED]


def test_merge_color_params_survives_a_twelve_key_recorded_state(stitched_roll):
    """The merge builds its base from
    the neutral defaults and overlays the recorded dict, so a twelve-key
    recorded state (an op predating the thirteenth key) merges instead of
    raising, and a one-key update leaves the other twelve untouched."""
    import dataclasses

    from scanny_boy import color
    from scanny_boy.edits import _merge_color_params

    recorded = {
        key: value
        for key, value in dataclasses.asdict(color.NEUTRAL_COLOR).items()
        if key != "cast_removal_highlights"
    } | {"wb_cyan": 0.1, "cast_removal": 0.4}

    merged = _merge_color_params(recorded, {"wb_magenta": 0.3})

    assert set(merged) == set(color.COLOR_PARAM_KEYS)
    assert merged["cast_removal_highlights"] == 0.0
    assert merged["wb_cyan"] == 0.1
    assert merged["wb_magenta"] == 0.3
    assert merged["cast_removal"] == 0.4


# --- spotting -----------------------------------------------------------

# A published canvas small enough to keep detection instant, with the
# width-gate fraction patched up so the derived gates still bite: 300 * 0.02
# = 6 px max defect width, SE radius 6.
_SPOTS_H, _SPOTS_W = 300, 400
_SPOT_FRACTION = 0.02


@pytest.fixture()
def spotty_roll(tmp_path: Path, monkeypatch):
    """A registered roll holding one completed negative whose published
    TIFF carries two defects: a 5x5 dark blob and a 3x90 dark hair."""
    monkeypatch.setattr(spots, "MAX_SPOT_MINOR_FRACTION", _SPOT_FRACTION)
    roll_dir = make_roll_dir(tmp_path)
    manifest = load_roll_manifest(roll_dir)
    from scanny_boy.manifest import SourceRecord
    from scanny_boy.roll_manifest import append_run, merge_sources

    append_run(manifest, _run(run_id="stitch-run", short_id="stitch"))
    merge_sources(
        manifest,
        [SourceRecord(filename="a.NEF", absolute_path="/x", size=1, mtime=1.0, sha256="a" * 64)],
        "stitch-run",
    )
    manifest.negatives.append(
        _negative(
            negative_id=_NEGATIVE_ID,
            run_id="stitch-run",
            status="completed",
            sequence=1,
            output={
                "name": "_DSC0001.tif",
                "size": 0,
                "sha256": "0" * 64,
                "width": _SPOTS_W,
                "height": _SPOTS_H,
            },
        )
    )
    write_roll_manifest(roll_dir, manifest)
    image = np.full((_SPOTS_H, _SPOTS_W, 3), 30000, dtype=np.uint16)
    yy, xx = np.ogrid[:_SPOTS_H, :_SPOTS_W]
    blob = (yy - 150) ** 2 + (xx - 200) ** 2 <= 9  # radius 3 at (row 150, col 200)
    image[blob] = 29850
    image[60:63, 40:130] = 29850  # a 3x90 hair
    import tifffile

    tifffile.imwrite(roll_dir / "_DSC0001.tif", image)
    return roll_dir


def test_detect_spots_records_the_op_and_reports_display_rects(spotty_roll):
    from scanny_boy.edits import run_edit_detect_spots

    (fields,) = run_edit_detect_spots(
        spotty_roll, _NEGATIVE_ID, 0.5, emit=lambda event: None
    )

    state = repo.net_edit_state(spotty_roll, _NEGATIVE_ID)
    assert state.spots is not None
    assert state.spots["detector_version"] == spots.DETECTOR_VERSION
    assert state.spots["repair"] is False
    assert state.spots["canvas"] == [_SPOTS_W, _SPOTS_H]
    assert state.spots["spots"], "the fixture's defects must be found"
    for spot in state.spots["spots"]:
        assert "rle" in spot  # the op carries the exact masks

    # On the wire: display-space rects, ids unchanged, no rle anywhere.
    assert fields["found"] == len(fields["spots"]) == len(state.spots["spots"])
    assert fields["preview_path"] is not None
    for reported in fields["spots"]:
        assert set(reported) == {"id", "kind", "polarity", "rect", "score", "rejected"}
        x, y, w, h = reported["rect"]
        assert 0 <= x and 0 <= y and w > 0 and h > 0


def test_detect_spots_twice_carries_rejections_forward(spotty_roll):
    from scanny_boy.edits import run_edit_detect_spots, run_edit_spots

    (first,) = run_edit_detect_spots(
        spotty_roll, _NEGATIVE_ID, 0.5, emit=lambda event: None
    )
    by_centre = {}
    for reported in first["spots"]:
        x, y, w, h = reported["rect"]
        by_centre[(x + w // 2, y + h // 2)] = reported["id"]
    # Raster order: the hair (row 60) is id 1, the blob (row 150) id 2.
    assert first["spots"][0]["id"] == 1

    reviewed = run_edit_spots(
        spotty_roll, _NEGATIVE_ID, reject=(1,), emit=lambda event: None
    )
    assert reviewed["spots"][0]["rejected"] is True
    assert reviewed["spots"][1]["rejected"] is False

    (again,) = run_edit_detect_spots(
        spotty_roll, _NEGATIVE_ID, 0.5, emit=lambda event: None
    )
    # The hair's new proposal sits within REJECTION_MATCH_PX of the
    # rejected one and is born rejected; the blob's is not.
    assert again["spots"][0]["rejected"] is True
    assert again["spots"][1]["rejected"] is False


def test_detect_spots_preserves_repair_across_a_re_detect(spotty_roll):
    from scanny_boy.edits import run_edit_detect_spots, run_edit_spots

    run_edit_detect_spots(spotty_roll, _NEGATIVE_ID, 0.5, emit=lambda event: None)
    fields = run_edit_spots(
        spotty_roll, _NEGATIVE_ID, repair=True, emit=lambda event: None
    )
    assert fields["repair"] is True

    (again,) = run_edit_detect_spots(
        spotty_roll, _NEGATIVE_ID, 0.5, emit=lambda event: None
    )
    assert again["repair"] is True
    assert repo.net_edit_state(spotty_roll, _NEGATIVE_ID).spots["repair"] is True


def test_spots_reject_on_an_unknown_id_fails_and_records_nothing(spotty_roll):
    from scanny_boy.edits import EditFailure, run_edit_detect_spots, run_edit_spots

    run_edit_detect_spots(spotty_roll, _NEGATIVE_ID, 0.5, emit=lambda event: None)
    before = repo.edits_for(spotty_roll, _NEGATIVE_ID)

    with pytest.raises(EditFailure) as excinfo:
        run_edit_spots(spotty_roll, _NEGATIVE_ID, reject=(7,), emit=lambda e: None)
    assert excinfo.value.code is Code.INVALID_EDIT
    assert "7" in excinfo.value.message
    assert repo.edits_for(spotty_roll, _NEGATIVE_ID) == before


def test_spots_clear_empties_the_set_and_turns_repair_off(spotty_roll):
    from scanny_boy.edits import run_edit_detect_spots, run_edit_spots

    run_edit_detect_spots(spotty_roll, _NEGATIVE_ID, 0.5, emit=lambda event: None)
    run_edit_spots(spotty_roll, _NEGATIVE_ID, repair=True, emit=lambda event: None)

    fields = run_edit_spots(
        spotty_roll, _NEGATIVE_ID, clear=True, emit=lambda event: None
    )
    assert fields["repair"] is False
    assert fields["spots"] == []
    state = repo.net_edit_state(spotty_roll, _NEGATIVE_ID)
    assert state.spots["repair"] is False
    assert state.spots["spots"] == []


def test_repair_then_no_repair_restores_the_pre_repair_preview(spotty_roll):
    from scanny_boy.edits import run_edit_detect_spots, run_edit_spots

    (first,) = run_edit_detect_spots(
        spotty_roll, _NEGATIVE_ID, 0.5, emit=lambda event: None
    )
    plain = Path(first["preview_path"]).read_bytes()

    run_edit_spots(spotty_roll, _NEGATIVE_ID, repair=True, emit=lambda event: None)
    (repaired,) = run_edit_detect_spots(
        spotty_roll, _NEGATIVE_ID, 0.5, emit=lambda event: None
    )
    assert Path(repaired["preview_path"]).read_bytes() != plain

    back = run_edit_spots(
        spotty_roll, _NEGATIVE_ID, repair=False, emit=lambda event: None
    )
    assert back["repair"] is False
    assert Path(back["preview_path"]).read_bytes() == plain


def test_accept_un_rejects(spotty_roll):
    from scanny_boy.edits import run_edit_detect_spots, run_edit_spots

    run_edit_detect_spots(spotty_roll, _NEGATIVE_ID, 0.5, emit=lambda event: None)
    run_edit_spots(spotty_roll, _NEGATIVE_ID, reject=(1,), emit=lambda event: None)
    fields = run_edit_spots(
        spotty_roll, _NEGATIVE_ID, accept=(1,), emit=lambda event: None
    )
    assert all(not spot["rejected"] for spot in fields["spots"])
    state = repo.net_edit_state(spotty_roll, _NEGATIVE_ID)
    assert all(not spot.get("rejected") for spot in state.spots["spots"])


def test_spots_without_any_flag_fails(spotty_roll):
    from scanny_boy.edits import EditFailure, run_edit_detect_spots, run_edit_spots

    run_edit_detect_spots(spotty_roll, _NEGATIVE_ID, 0.5, emit=lambda event: None)
    with pytest.raises(EditFailure) as excinfo:
        run_edit_spots(spotty_roll, _NEGATIVE_ID, emit=lambda e: None)
    assert excinfo.value.code is Code.INVALID_EDIT


def test_spots_on_a_negative_without_a_set_fails(spotty_roll):
    from scanny_boy.edits import EditFailure, run_edit_spots

    with pytest.raises(EditFailure) as excinfo:
        run_edit_spots(spotty_roll, _NEGATIVE_ID, reject=(1,), emit=lambda e: None)
    assert excinfo.value.code is Code.INVALID_EDIT
    assert "detect-spots" in excinfo.value.message


def test_list_spots_records_nothing(spotty_roll):
    from scanny_boy.edits import run_edit_detect_spots, run_edit_list_spots

    run_edit_detect_spots(spotty_roll, _NEGATIVE_ID, 0.5, emit=lambda event: None)
    before = repo.edits_for(spotty_roll, _NEGATIVE_ID)

    fields = run_edit_list_spots(spotty_roll, _NEGATIVE_ID, emit=lambda event: None)

    assert repo.edits_for(spotty_roll, _NEGATIVE_ID) == before
    assert fields["spots"]
    assert fields["preview_path"] is None
    assert all("rle" not in spot for spot in fields["spots"])


def test_a_stale_spot_set_reports_empty_with_a_warning(spotty_roll):
    from scanny_boy.edits import run_edit_list_spots

    stale = spots.spots_params(
        canvas=(123, 456),
        spots=[
            {
                "id": 1,
                "kind": "blob",
                "polarity": "dense",
                "bbox": [10, 10, 4, 3],
                "rle": [0, 4, 0, 4, 0, 4],
                "area": 12,
                "score": 9.0,
                "rejected": False,
            }
        ],
        sensitivity=0.5,
        repair=True,
    )
    repo.append_spots_edit(spotty_roll, _NEGATIVE_ID, stale)

    warnings: list = []
    fields = run_edit_list_spots(spotty_roll, _NEGATIVE_ID, emit=warnings.append)

    assert fields["spots"] == []
    assert [w.code for w in warnings] == [Code.SPOTS_STALE]


def test_spot_limit_reached_fires_with_found_and_kept(spotty_roll, monkeypatch):
    from scanny_boy.edits import run_edit_detect_spots

    monkeypatch.setattr(spots, "MAX_SPOTS", 1)
    warnings: list = []
    (fields,) = run_edit_detect_spots(
        spotty_roll, _NEGATIVE_ID, 0.5, emit=warnings.append
    )

    assert fields["found"] == 2
    assert len(fields["spots"]) == 1
    assert [w.code for w in warnings] == [Code.SPOT_LIMIT_REACHED]
    assert "2" in warnings[0].message and "1" in warnings[0].message


def test_detect_spots_sensitivity_out_of_range_fails(spotty_roll):
    from scanny_boy.edits import EditFailure, run_edit_detect_spots

    with pytest.raises(EditFailure) as excinfo:
        run_edit_detect_spots(spotty_roll, _NEGATIVE_ID, 1.5, emit=lambda e: None)
    assert excinfo.value.code is Code.INVALID_EDIT
    assert repo.edits_for(spotty_roll, _NEGATIVE_ID) == []


def test_unstitched_negative_fails_for_all_three_spot_commands(tmp_path):
    from scanny_boy.edits import (
        EditFailure,
        run_edit_detect_spots,
        run_edit_list_spots,
        run_edit_spots,
    )

    roll_dir = make_roll_dir(tmp_path)
    for runner, kwargs in (
        (run_edit_detect_spots, {"sensitivity": 0.5}),
        (run_edit_spots, {"reject": (1,)}),
        (run_edit_list_spots, {}),
    ):
        with pytest.raises(EditFailure) as excinfo:
            runner(roll_dir, "nope", emit=lambda e: None, **kwargs)
        assert excinfo.value.code is Code.NEGATIVE_NOT_FOUND


# --- `edit crop` ---------------------------------------------------------


def _crop_gradient(roll_dir: Path, name: str = "_DSC0001.tif") -> None:
    """Writes the fixture negative's published TIFF: a 90x60 coordinate
    gradient, big enough to crop."""
    rows = np.arange(60, dtype=np.uint16)[:, None] * 300
    cols = np.arange(90, dtype=np.uint16)[None, :] * 150
    tifffile.imwrite(roll_dir / name, rows + cols)


@pytest.fixture()
def croppable_roll(tmp_path: Path) -> Path:
    """One completed negative whose published TIFF (90x60) has room for a
    crop above the 16px floor."""
    roll_dir = make_roll_dir(tmp_path)
    manifest = load_roll_manifest(roll_dir)
    from scanny_boy.manifest import SourceRecord
    from scanny_boy.roll_manifest import append_run, merge_sources

    append_run(manifest, _run(run_id="stitch-run", short_id="stitch"))
    merge_sources(
        manifest,
        [SourceRecord(filename="a.NEF", absolute_path="/x", size=1, mtime=1.0, sha256="a" * 64)],
        "stitch-run",
    )
    manifest.negatives.append(
        _negative(
            negative_id=_NEGATIVE_ID,
            run_id="stitch-run",
            status="completed",
            sequence=1,
            output={
                "name": "_DSC0001.tif",
                "size": 0,
                "sha256": "0" * 64,
                "width": 90,
                "height": 60,
            },
        )
    )
    write_roll_manifest(roll_dir, manifest)
    _crop_gradient(roll_dir)
    return roll_dir


def test_crop_records_the_window_and_refreshes_the_preview(croppable_roll):
    import cv2

    from scanny_boy.previews import NORMALIZED_DISPLAY_LUT, _display_image

    events: list = []
    tiff_before = (croppable_roll / "_DSC0001.tif").read_bytes()

    fields = run_edit_crop(
        croppable_roll,
        _NEGATIVE_ID,
        rect=(10, 8, 50, 24),
        tilt_deg=0.0,
        preset="35mm",
        emit=events.append,
    )

    # The published TIFF is untouched — the crop is metadata until export.
    assert (croppable_roll / "_DSC0001.tif").read_bytes() == tiff_before
    assert fields["edit"]["op"] == repo.CROP_OP
    assert fields["crop"]["width"] == 50
    assert fields["crop"]["height"] == 24
    assert fields["crop"]["tilt_deg"] == 0.0
    assert fields["crop"]["preset"] == "35mm"
    assert fields["crop"]["x"] == 10
    assert fields["crop"]["y"] == 8
    assert fields["crop"]["canvas_width"] == 90
    assert fields["crop"]["canvas_height"] == 60
    assert fields["preview_path"]

    # The preview now shows the cropped frame: the crop step applied first
    # in the replay, the display LUT on top.
    state = repo.net_edit_state(croppable_roll, _NEGATIVE_ID)
    display = _display_image(
        croppable_roll / "_DSC0001.tif",
        state.quarter_turns,
        state.flipped,
        state.fine_angle_deg,
        state.spots,
        state.crop,
    )
    stored = cv2.imread(str(fields["preview_path"]), cv2.IMREAD_UNCHANGED)
    expected = cv2.cvtColor(
        NORMALIZED_DISPLAY_LUT[display], cv2.COLOR_RGB2BGR
    )
    np.testing.assert_array_equal(stored, expected)


def test_crop_records_the_tilt_in_tiff_space(croppable_roll):
    """A drawn tilt lands in the stored window's `tilt_deg`, and the
    composed window's rect sits where the drawn rect's centre mapped."""
    fields = run_edit_crop(
        croppable_roll,
        _NEGATIVE_ID,
        rect=(10, 8, 50, 24),
        tilt_deg=5.0,
        emit=lambda event: None,
    )

    assert abs(fields["crop"]["tilt_deg"] - 5.0) < 0.51
    state = repo.net_edit_state(croppable_roll, _NEGATIVE_ID)
    assert state.crop["w"] == 50
    assert state.crop["h"] == 24
    assert 0 <= state.crop["x"] and state.crop["x"] + 50 <= 90
    assert 0 <= state.crop["y"] and state.crop["y"] + 24 <= 60


def test_crop_reset_clears_the_state(croppable_roll):
    run_edit_crop(
        croppable_roll,
        _NEGATIVE_ID,
        rect=(10, 8, 50, 24),
        emit=lambda event: None,
    )
    events: list = []

    fields = run_edit_crop(
        croppable_roll, _NEGATIVE_ID, reset=True, emit=events.append
    )

    assert fields["crop"] is None
    assert repo.net_edit_state(croppable_roll, _NEGATIVE_ID).crop is None
    manifest = load_roll_manifest(croppable_roll)
    negative = manifest.negative(_NEGATIVE_ID)
    # The preview regenerated back to the full frame.
    import cv2

    stored = cv2.imread(str(negative.preview_path), cv2.IMREAD_UNCHANGED)
    assert stored.shape[:2] == (60, 90)


def test_a_recrop_composes_over_the_live_crop(croppable_roll):
    """The second crop is drawn over the first crop's display, and the ops
    log still reduces to one fully-composed window: the final display's
    dimensions are the second rect's."""
    run_edit_crop(
        croppable_roll,
        _NEGATIVE_ID,
        rect=(5, 5, 80, 50),
        emit=lambda event: None,
    )

    fields = run_edit_crop(
        croppable_roll,
        _NEGATIVE_ID,
        rect=(10, 10, 40, 24),
        tilt_deg=3.0,
        emit=lambda event: None,
    )

    assert fields["crop"]["width"] == 40
    assert fields["crop"]["height"] == 24
    state = repo.net_edit_state(croppable_roll, _NEGATIVE_ID)
    assert abs(state.crop["tilt_deg"] - 3.0) < 0.51
    # One window in TIFF space, inside the canvas.
    assert 0 <= state.crop["x"] and state.crop["x"] + state.crop["w"] <= 90
    assert 0 <= state.crop["y"] and state.crop["y"] + state.crop["h"] <= 60


def test_a_full_frame_recrop_composes_over_the_live_crop(croppable_roll):
    """The app re-enters crop mode on the full uncropped canvas; the second
    rect is in that space and still reduces to one composed window."""
    run_edit_crop(
        croppable_roll,
        _NEGATIVE_ID,
        rect=(5, 5, 80, 50),
        emit=lambda event: None,
    )

    fields = run_edit_crop(
        croppable_roll,
        _NEGATIVE_ID,
        rect=(10, 8, 50, 24),
        tilt_deg=3.0,
        full_frame=True,
        emit=lambda event: None,
    )

    assert fields["crop"]["width"] == 50
    assert fields["crop"]["height"] == 24
    assert abs(fields["crop"]["x"] - 10) <= 1
    assert abs(fields["crop"]["y"] - 8) <= 1
    assert fields["crop"]["canvas_width"] == 90
    assert fields["crop"]["canvas_height"] == 60
    state = repo.net_edit_state(croppable_roll, _NEGATIVE_ID)
    assert abs(state.crop["tilt_deg"] - 3.0) < 0.51
    assert 0 <= state.crop["x"] and state.crop["x"] + state.crop["w"] <= 90
    assert 0 <= state.crop["y"] and state.crop["y"] + state.crop["h"] <= 60


def test_crop_validation_rejects_bad_rects(croppable_roll):
    with pytest.raises(EditFailure) as exc:
        run_edit_crop(
            croppable_roll,
            _NEGATIVE_ID,
            rect=(10, 8, 8, 24),
            emit=lambda event: None,
        )
    assert exc.value.code == Code.INVALID_EDIT

    with pytest.raises(EditFailure) as exc:
        run_edit_crop(
            croppable_roll,
            _NEGATIVE_ID,
            rect=(80, 8, 50, 24),  # hangs off the 90-wide display
            emit=lambda event: None,
        )
    assert exc.value.code == Code.INVALID_EDIT

    with pytest.raises(EditFailure) as exc:
        run_edit_crop(
            croppable_roll,
            _NEGATIVE_ID,
            rect=(10, 8, 50, 24),
            tilt_deg=50.0,
            emit=lambda event: None,
        )
    assert exc.value.code == Code.INVALID_EDIT

    with pytest.raises(EditFailure) as exc:
        run_edit_crop(croppable_roll, _NEGATIVE_ID, emit=lambda event: None)
    assert exc.value.code == Code.INVALID_EDIT


def test_crop_composes_with_the_recorded_rotation(croppable_roll):
    """A crop drawn after a quarter turn maps back through the rotation:
    the stored window's sides are the drawn rect's swapped, and the final
    display's dimensions are the drawn rect's own."""
    run_edit_rotate(croppable_roll, _NEGATIVE_ID, "cw", emit=lambda event: None)

    fields = run_edit_crop(
        croppable_roll,
        _NEGATIVE_ID,
        rect=(10, 8, 40, 24),
        emit=lambda event: None,
    )

    assert fields["crop"]["width"] == 40
    assert fields["crop"]["height"] == 24
    state = repo.net_edit_state(croppable_roll, _NEGATIVE_ID)
    assert state.quarter_turns == 1
    assert (state.crop["w"], state.crop["h"]) == (24, 40)


def test_spot_markers_hide_while_a_crop_is_live(croppable_roll):
    """A live crop reports no markers — the axis-aligned rects have no
    faithful drawing over a cropped-and-tilted display, and the repair
    they stand for is replayed before the crop anyway."""
    from scanny_boy.edits import _spots_for_report

    crop_params = {
        "canvas": [90, 60],
        "x": 10,
        "y": 8,
        "w": 50,
        "h": 24,
        "tilt_deg": 0.0,
    }
    repo.append_edit(croppable_roll, _NEGATIVE_ID, repo.CROP_OP, crop_params)

    reported = _spots_for_report(
        croppable_roll,
        load_roll_manifest(croppable_roll).negative(_NEGATIVE_ID),
        {
            "canvas": [90, 60],
            "repair": False,
            "spots": [
                {"id": 1, "bbox": [20, 20, 4, 4], "rejected": False, "rle": "1x4"}
            ],
        },
    )
    assert reported == []
