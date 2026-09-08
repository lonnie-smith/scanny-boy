"""The persistence repository: `RollManifest` dataclasses to and from the
library database.

`save_roll` upserts the whole manifest keyed by `roll_id` — the roll row's
`folder_path` is updated on every save, which is what makes `roll rename`'s
folder move a data update rather than a special case. Children are diffed by
key (`run_id`, `sha256`, `negative_id`): rows the incoming manifest no
longer describes are deleted, rows it does describe are merged in place.
Sources are the exception: they are rewritten wholesale on every save because
their surrogate key gives `merge` no identity to diff against. A
negative's `edits` rows hang off its stable `negative_id`, so re-stitching a
negative — which keeps its id — keeps its edit history, while removing a
negative (adoption's removal path) cascades its edits away with it.

Load and save are deliberately the only two shapes the rest of the program
sees: everything is a plain `RollManifest` in memory, exactly as it was when
the manifest was a JSON file.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from scanny_boy.events import Code
from scanny_boy.flatfield import FlatFieldError, FlatFieldProfile
from scanny_boy.grid_profile import GridProfile, GridProfileError
from scanny_boy.library.db import open_engine
from scanny_boy.library.models import (
    METADATA_FIELDS,
    ROLL_ONLY_METADATA_FIELDS,
    EditRow,
    FlatFieldProfileRow,
    GridProfileRow,
    MetadataValueRow,
    NegativeRow,
    RollRow,
    RunRow,
    SourceRow,
)

if TYPE_CHECKING:
    from scanny_boy.roll_manifest import RollManifest

# The edit operations the app records. Rotation params are
# `{"direction": "cw" | "ccw"}`; flip takes no params (horizontal only);
# `rotate_fine` params are `{"angle_deg": number, "source": "auto"}` — the
# stitch stage's auto-seeded rebate squaring (see `auto_rotate.py`). All
# compose by ordered replay — a flip does not commute with rotation, so the
# log reduces to a `(quarter_turns, flipped, fine_angle_deg)` triple, not a
# single number.
ROTATE_OP = "rotate"
FLIP_OP = "flip"
ROTATE_FINE_OP = "rotate_fine"
_DIRECTIONS = {"cw": 1, "ccw": -1}

# `crop` params are a tilted crop window in **published-TIFF pixels** — the
# same convention the `spots` op uses ("TIFF-space geometry does not move
# when the display transform does"): `{"canvas": [w, h], "x", "y", "w",
# "h", "tilt_deg", "preset"}`. `tilt_deg` is the window's
# counter-clockwise tilt as displayed (±45 at the CLI's widest; the app's
# slider is ±10), and the window itself is the axis-aligned rect `x, y,
# w, h` — the crop removes the tilt: the output shows the rect's content
# upright, which is the image rotated by `-tilt` about the rect's centre
# then cropped to the rect. A sibling of `tone`/`color`/`spots`: a state,
# not a transform — the latest op wins, a trailing op coalescing in place
# is unnecessary because every op stores the fully-composed window (a
# re-crop is mapped through the net state back to TIFF space at record
# time), and `--reset` appends a `{"reset": true}` op that parses to no
# crop. Like `spots`, the `canvas` guards a re-stitch: a crop recorded
# against different TIFF dimensions is stale and ignored (see
# `previews.crop_is_live`).
CROP_OP = "crop"
# The widest tilt a `crop` op may record, in degrees. The app's slider is
# ±10; the CLI's floor is wider so a re-crop composed over an existing
# tilt (which adds algebraically in TIFF space) stays valid.
CROP_TILT_MAX_DEG = 45.0
# The smallest crop window side the CLI accepts, in pixels — a window
# smaller than this is almost certainly a mis-click.
CROP_MIN_SIZE_PX = 16

# `tone` params are the complete preview tone state — nine keys, all set or
# all `None` for the reset (see `tone.py`). Unlike the geometric ops it is a
# state, not a transform and never a mode: the latest one wins.
# `append_tone_edit` coalesces a trailing `tone` op in place, the one
# sanctioned exception to the log's append-only discipline.
TONE_OP = "tone"

# `color` params are the complete preview colour state — thirteen keys
# (docs/CAST_REMOVAL_PLAN.md R-2), of which the original twelve are the
# compatibility floor (`color.COLOR_PARAM_KEYS_V1`) — all set or all `None`
# for the reset (see `color.py`). A sibling of `tone`:
# preview-only, independently resettable, coalesced in place. Baked at export
# through the same render as the preview (docs/EXPORT_PLAN.md §4.6).
COLOR_OP = "color"

# `spots` params are the spot detector's proposals plus the whole-negative
# `repair` switch (see `spots.py`): `{"detector_version", "sensitivity",
# "repair", "canvas", "spots"}`, each spot carrying its exact RLE mask over
# its own bounding box in **published-TIFF pixels**. A sibling of `tone` and
# `color` — a state, not a transform, the latest one wins, coalesced in
# place — with one singular difference: it is the only op whose replay
# *synthesizes* pixel values, which is why `repair` is part of the state and
# why a canvas mismatch repairs nothing (SPOTTING_PLAN §1.5).
SPOTS_OP = "spots"

# The gain a frame record carries when the row predates gain normalization
# and never had one written: unity, since nothing was applied.
_UNITY_GAIN = (1.0, 1.0, 1.0)


@dataclasses.dataclass(frozen=True)
class EditState:
    quarter_turns: int
    flipped: bool
    fine_angle_deg: float
    tone: dict[str, float] | None
    color: dict[str, float] | None
    # The net `spots` op's params, or None — a state like `tone`/`color`,
    # but one that reaches the export (SPOTTING_PLAN §3.3).
    spots: dict | None = None
    # The net `crop` op's params, or None — same state family, TIFF-space
    # like `spots`; the preview folds it in and the export bakes it.
    crop: dict | None = None


class RollNotRegisteredError(Exception):
    """Maps to `ROLL_NOT_FOUND`: the folder is not a roll the library knows
    about. Replaces the old "folder has no scanny-boy-roll.json" check —
    with the manifest in the database, existence is registration."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = Code.ROLL_NOT_FOUND
        self.message = message


@contextmanager
def _session() -> Iterator[Session]:
    session = Session(open_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _folder_key(roll_dir: Path) -> str:
    # Resolved so the same folder reached through different path spellings
    # (`/var` vs `/private/var`, trailing separators) is one row.
    return str(roll_dir.resolve())


# --- RollManifest -> rows -------------------------------------------------


def save_roll(roll_dir: Path, manifest: RollManifest) -> None:
    from scanny_boy.roll_manifest import (
        NegativeRecord,
        RollSourceRecord,
        RunRecord,
    )

    folder = _folder_key(roll_dir)
    with _session() as session:
        roll = session.get(RollRow, manifest.roll_id)
        if roll is None:
            roll = RollRow(roll_id=manifest.roll_id)
            session.add(roll)
        roll.folder_path = folder
        roll.roll_name = manifest.roll_name
        roll.scanny_boy_version = manifest.scanny_boy_version
        roll.created_at = manifest.created_at
        roll.updated_at = manifest.updated_at
        roll.processing_params = manifest.processing_params
        roll.icc_profile = manifest.icc_profile
        roll.published_icc_profile = manifest.published_icc_profile
        roll.stitch_params = manifest.stitch_params
        roll.camera_color = (
            None if manifest.camera_color is None else manifest.camera_color.to_dict()
        )
        roll.film_kind = manifest.film
        roll.film_base = manifest.film_base
        roll.roll_capture_date = manifest.metadata.roll_capture_date
        roll.last_applied_at = manifest.metadata.last_applied_at
        for field in METADATA_FIELDS:
            setattr(roll, field, getattr(manifest.metadata, field))
        for field in ROLL_ONLY_METADATA_FIELDS:
            setattr(roll, field, getattr(manifest.metadata, field))

        # Diff by key so re-saving an unchanged child is a no-op and removed
        # children (an adopted negative's removal) actually go away.
        run_ids = {r.run_id for r in manifest.runs}
        session.execute(
            delete(RunRow).where(
                RunRow.roll_id == manifest.roll_id, RunRow.run_id.not_in(run_ids)
            )
        )
        for ordinal, run in enumerate(manifest.runs):
            assert isinstance(run, RunRecord)
            session.merge(
                RunRow(
                    run_id=run.run_id,
                    roll_id=manifest.roll_id,
                    ordinal=ordinal,
                    short_id=run.short_id,
                    kind=run.kind,
                    status=run.status,
                    convert_run_id=run.convert_run_id,
                    input_folder=run.input_folder,
                    source_order=run.source_order,
                    work_dir=run.work_dir,
                    started_at=run.started_at,
                    finished_at=run.finished_at,
                    normalization_aggregate=run.normalization_aggregate,
                )
            )

        # Sources carry an autoincrement primary key, so a `merge` without a
        # pre-loaded identity inserts a duplicate row on every save. The list
        # is small and nothing holds a foreign key to it, so rewriting it
        # wholesale is the safe diff: delete all, insert the incoming set.
        session.execute(delete(SourceRow).where(SourceRow.roll_id == manifest.roll_id))
        for ordinal, source in enumerate(manifest.sources):
            assert isinstance(source, RollSourceRecord)
            session.add(
                SourceRow(
                    roll_id=manifest.roll_id,
                    ordinal=ordinal,
                    filename=source.filename,
                    absolute_path=source.absolute_path,
                    size=source.size,
                    mtime=source.mtime,
                    sha256=source.sha256,
                    run_id=source.run_id,
                    scan_clip_fractions=(
                        None
                        if source.scan_clip_fractions is None
                        else list(source.scan_clip_fractions)
                    ),
                )
            )

        negative_ids = {n.negative_id for n in manifest.negatives}
        # Deleting a negative cascades its edits; surviving negatives keep
        # theirs, keyed by the stable `negative_id`.
        session.execute(
            delete(NegativeRow).where(
                NegativeRow.roll_id == manifest.roll_id,
                NegativeRow.negative_id.not_in(negative_ids),
            )
        )
        for ordinal, negative in enumerate(manifest.negatives):
            assert isinstance(negative, NegativeRecord)
            session.merge(
                NegativeRow(
                    negative_id=negative.negative_id,
                    roll_id=manifest.roll_id,
                    ordinal=ordinal,
                    run_id=negative.run_id,
                    sequence=negative.sequence,
                    members=negative.members,
                    expected_output=negative.expected_output,
                    status=negative.status,
                    output=negative.output,
                    frames=[f.to_dict() for f in negative.frames],
                    pairs=[p.to_dict() for p in negative.pairs],
                    global_rms_px=negative.global_rms_px,
                    canvas=(
                        None
                        if negative.canvas is None
                        else {"width": negative.canvas[0], "height": negative.canvas[1]}
                    ),
                    valid_rect=(
                        None
                        if negative.valid_rect is None
                        else list(negative.valid_rect)
                    ),
                    fill_color=list(negative.fill_color),
                    normalized_fill=negative.normalized_fill,
                    normalization=negative.normalization,
                    rectification=negative.rectification,
                    rebate_deviation_px=negative.rebate_deviation_px,
                    used_clahe_fallback=negative.used_clahe_fallback,
                    error_code=negative.error_code,
                    error_message=negative.error_message,
                    capture_time=negative.capture_time.to_dict(),
                    preview_path=negative.preview_path,
                    grid=negative.grid,
                    grid_cells=negative.grid_cells,
                    grid_pitch_ratio=negative.grid_pitch_ratio,
                    grid_alignment_ratio=negative.grid_alignment_ratio,
                    **{
                        field: getattr(negative.metadata, field)
                        for field in METADATA_FIELDS
                    },
                )
            )


# --- rows -> RollManifest --------------------------------------------------


def roll_registered(roll_dir: Path) -> bool:
    with _session() as session:
        return (
            session.scalar(
                select(RollRow.folder_path).where(
                    RollRow.folder_path == _folder_key(roll_dir)
                )
            )
            is not None
        )


def registered_rolls_under(library: Path) -> list[tuple[str, str, str, int]]:
    """Every registered roll whose folder sits directly under `library`:
    `(folder_path, roll_id, roll_name, negative_count)`, sorted by folder
    path."""
    prefix = str(library.resolve())
    with _session() as session:
        rolls = session.scalars(select(RollRow)).all()
        listing: list[tuple[str, str, str, int]] = []
        for roll in rolls:
            if str(Path(roll.folder_path).parent) != prefix:
                continue
            count = session.scalar(
                select(func.count())
                .select_from(NegativeRow)
                .where(NegativeRow.roll_id == roll.roll_id)
            )
            listing.append((roll.folder_path, roll.roll_id, roll.roll_name, count or 0))
        listing.sort(key=lambda entry: entry[0])
        return listing


def delete_roll(roll_dir: Path) -> str:
    """Remove the roll's registration and return its `roll_id`. The runs,
    sources, negatives, and edits rows cascade away with it (each child's
    `ondelete="CASCADE"`; `PRAGMA foreign_keys=ON` is set on every
    connection). The folder is deliberately not touched — deleting the
    registration is what makes `roll list` drop the roll, whatever then
    happens to its files."""
    folder = _folder_key(roll_dir)
    with _session() as session:
        roll = session.scalar(select(RollRow).where(RollRow.folder_path == folder))
        if roll is None:
            raise RollNotRegisteredError(
                f"{roll_dir} is not a registered roll; create the roll first"
            )
        session.delete(roll)
        return roll.roll_id


def load_roll(roll_dir: Path) -> RollManifest:
    from scanny_boy.roll_manifest import (
        CameraColor,
        CaptureTime,
        FrameRecord,
        NegativeMetadata,
        NegativeRecord,
        PairRecord,
        RollManifest,
        RollMetadata,
        RollSourceRecord,
        RunRecord,
    )

    folder = _folder_key(roll_dir)
    with _session() as session:
        roll = session.scalar(select(RollRow).where(RollRow.folder_path == folder))
        if roll is None:
            raise RollNotRegisteredError(
                f"{roll_dir} is not a registered roll; create the roll first"
            )

        runs = session.scalars(
            select(RunRow)
            .where(RunRow.roll_id == roll.roll_id)
            .order_by(RunRow.ordinal)
        ).all()
        sources = session.scalars(
            select(SourceRow)
            .where(SourceRow.roll_id == roll.roll_id)
            .order_by(SourceRow.ordinal)
        ).all()
        negatives = session.scalars(
            select(NegativeRow)
            .where(NegativeRow.roll_id == roll.roll_id)
            .order_by(NegativeRow.ordinal)
        ).all()

        return RollManifest(
            scanny_boy_version=roll.scanny_boy_version,
            roll_id=roll.roll_id,
            roll_name=roll.roll_name,
            created_at=roll.created_at,
            updated_at=roll.updated_at,
            processing_params=roll.processing_params,
            icc_profile=roll.icc_profile,
            published_icc_profile=dict(roll.published_icc_profile or {}),
            stitch_params=roll.stitch_params,
            film=roll.film_kind,
            film_base=roll.film_base,
            runs=[
                RunRecord(
                    run_id=r.run_id,
                    short_id=r.short_id,
                    kind=r.kind,
                    status=r.status,
                    convert_run_id=r.convert_run_id,
                    input_folder=r.input_folder,
                    source_order=list(r.source_order),
                    work_dir=r.work_dir,
                    started_at=r.started_at,
                    finished_at=r.finished_at,
                    normalization_aggregate=r.normalization_aggregate,
                )
                for r in runs
            ],
            sources=[
                RollSourceRecord(
                    filename=s.filename,
                    absolute_path=s.absolute_path,
                    size=s.size,
                    mtime=s.mtime,
                    sha256=s.sha256,
                    run_id=s.run_id,
                    scan_clip_fractions=(
                        None
                        if s.scan_clip_fractions is None
                        else tuple(s.scan_clip_fractions)
                    ),
                )
                for s in sources
            ],
            negatives=[
                NegativeRecord(
                    negative_id=n.negative_id,
                    run_id=n.run_id,
                    sequence=n.sequence,
                    members=list(n.members),
                    expected_output=n.expected_output,
                    fill_color=tuple(n.fill_color),
                    status=n.status,
                    output=n.output,
                    frames=[
                        FrameRecord(
                            name=f["name"],
                            rotation_deg=f["rotation_deg"],
                            translation=(f["translation"][0], f["translation"][1]),
                            # Rows written before gain normalization carry no
                            # `gain` (nothing was applied to them) — a missing
                            # gain is unity, not a corrupt row.
                            gain=tuple(f.get("gain", _UNITY_GAIN)),
                            # Likewise, rows written before the per-frame scale
                            # solve (docs/STITCH_QUALITY_PLAN.md section 2)
                            # carry no `scale` — the layout was placed as a
                            # rigid transform, which is scale 1.
                            scale=f.get("scale", 1.0),
                        )
                        for f in n.frames
                    ],
                    pairs=[
                        PairRecord(
                            a=p["a"],
                            b=p["b"],
                            inliers=p["inliers"],
                            good_matches=p["good_matches"],
                            inlier_ratio=p["inlier_ratio"],
                            rms_residual_px=p["rms_residual_px"],
                            scale_drift=p["scale_drift"],
                            overlap_fraction=p["overlap_fraction"],
                            overlap_mad=p["overlap_mad"],
                            # Likewise absent before gain normalization: no
                            # pre-gain measurement was ever taken.
                            overlap_mad_pregain=p.get("overlap_mad_pregain"),
                            accepted=p["accepted"],
                        )
                        for p in n.pairs
                    ],
                    global_rms_px=n.global_rms_px,
                    canvas=(
                        None
                        if n.canvas is None
                        else (n.canvas["width"], n.canvas["height"])
                    ),
                    valid_rect=None if n.valid_rect is None else tuple(n.valid_rect),
                    normalized_fill=n.normalized_fill,
                    normalization=n.normalization,
                    rectification=n.rectification,
                    rebate_deviation_px=n.rebate_deviation_px,
                    used_clahe_fallback=bool(n.used_clahe_fallback),
                    error_code=n.error_code,
                    error_message=n.error_message,
                    capture_time=CaptureTime(**n.capture_time),
                    metadata=NegativeMetadata(
                        **{field: getattr(n, field) for field in METADATA_FIELDS}
                    ),
                    preview_path=n.preview_path,
                    grid=n.grid,
                    grid_cells=n.grid_cells,
                    grid_pitch_ratio=n.grid_pitch_ratio,
                    grid_alignment_ratio=n.grid_alignment_ratio,
                )
                for n in negatives
            ],
            metadata=RollMetadata(
                roll_capture_date=roll.roll_capture_date,
                last_applied_at=roll.last_applied_at,
                **{field: getattr(roll, field) for field in METADATA_FIELDS},
                **{field: getattr(roll, field) for field in ROLL_ONLY_METADATA_FIELDS},
            ),
            camera_color=(
                None
                if roll.camera_color is None
                else CameraColor.from_dict(roll.camera_color)
            ),
        )


# --- the edits ops log ------------------------------------------------------


def _negative_row(session: Session, roll_dir: Path, negative_id: str) -> NegativeRow:
    negative = session.get(NegativeRow, negative_id)
    # `negative_id` is a globally-unique primary key and the argument is
    # user-supplied, so an id that belongs to a *different* roll must be
    # refused here, not silently edited under the wrong roll's name.
    roll = session.scalar(
        select(RollRow).where(RollRow.folder_path == _folder_key(roll_dir))
    )
    if negative is None or roll is None or negative.roll_id != roll.roll_id:
        raise RollNotRegisteredError(
            f"{negative_id} is not a negative of {_folder_key(roll_dir)}"
        )
    return negative


def append_edit(
    roll_dir: Path, negative_id: str, op: str, params: dict[str, Any]
) -> dict:
    """Appends one op to the negative's ordered log and returns it as a
    dict: `{id, negative_id, position, op, params, created_at}`."""
    from scanny_boy.roll_manifest import _now_iso

    with _session() as session:
        negative = _negative_row(session, roll_dir, negative_id)
        position = (
            session.scalar(
                select(func.max(EditRow.position)).where(
                    EditRow.negative_id == negative.negative_id
                )
            )
            or 0
        ) + 1
        row = EditRow(
            negative_id=negative.negative_id,
            position=position,
            op=op,
            params=params,
            created_at=_now_iso(),
        )
        session.add(row)
        session.flush()
        return {
            "id": row.id,
            "negative_id": row.negative_id,
            "position": row.position,
            "op": row.op,
            "params": row.params,
            "created_at": row.created_at,
        }


def _tone_param_bounds() -> tuple[tuple[str, float, float], ...]:
    from scanny_boy import tone

    return (
        ("grade_r", tone.GRADE_MIN, tone.GRADE_MAX),
        ("snap_gamma", tone.SNAP_MIN, tone.SNAP_MAX),
        ("density", tone.DENSITY_MIN, tone.DENSITY_MAX),
        ("shadow_density", tone.SHADOW_DENSITY_MIN, tone.SHADOW_DENSITY_MAX),
        ("highlight_density", tone.HIGHLIGHT_DENSITY_MIN, tone.HIGHLIGHT_DENSITY_MAX),
        ("toe", tone.TOE_MIN, tone.TOE_MAX),
        ("toe_width", tone.TOE_WIDTH_MIN, tone.TOE_WIDTH_MAX),
        ("shoulder", tone.SHOULDER_MIN, tone.SHOULDER_MAX),
        ("shoulder_width", tone.SHOULDER_WIDTH_MIN, tone.SHOULDER_WIDTH_MAX),
    )


def validated_tone_params(
    params: dict[str, float | None] | None,
) -> dict[str, float | None]:
    """The `tone` op's params: all nine set, or all nine `None` (the reset)."""
    from scanny_boy import tone

    if params is None:
        return {key: None for key in tone.TONE_PARAM_KEYS}
    missing = [key for key in tone.TONE_PARAM_KEYS if key not in params]
    if missing:
        raise ValueError(f"tone params missing keys: {', '.join(missing)}")
    values = {key: params[key] for key in tone.TONE_PARAM_KEYS}
    if all(value is None for value in values.values()):
        return {key: None for key in tone.TONE_PARAM_KEYS}
    if any(value is None for value in values.values()):
        raise ValueError("tone params must all be set together, or all None for reset")
    validated: dict[str, float | None] = {}
    for name, low, high in _tone_param_bounds():
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"tone {name} must be a number, got {value!r}")  # noqa: TRY004
        if not low <= value <= high:
            raise ValueError(f"tone {name} must be within [{low}, {high}], got {value}")
        validated[name] = float(value)
    return validated


def validated_color_params(
    params: dict[str, float | None] | None,
) -> dict[str, float | None]:
    """The `color` op's params: all twelve compatibility-floor keys set, or
    all `None` (docs/CAST_REMOVAL_PLAN.md R-2 §6.2). A newer key absent
    from `params` is filled from the neutral defaults before validation —
    that is what "this op predates the control" means."""
    from scanny_boy import color

    if params is None:
        return {key: None for key in color.COLOR_PARAM_KEYS}
    missing = [key for key in color.COLOR_PARAM_KEYS_V1 if key not in params]
    if missing:
        raise ValueError(f"color params missing keys: {', '.join(missing)}")
    defaults = _color_neutral_defaults()
    values = {
        key: params.get(key, defaults[key]) for key in color.COLOR_PARAM_KEYS
    }
    if all(value is None for value in values.values()):
        return {key: None for key in color.COLOR_PARAM_KEYS}
    if any(value is None for value in values.values()):
        raise ValueError("color params must all be set together, or all None for reset")
    validated: dict[str, float | None] = {}
    for name, low, high in color._color_param_bounds():
        value = values[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"color {name} must be a number, got {value!r}")  # noqa: TRY004
        if not low <= value <= high:
            raise ValueError(
                f"color {name} must be within [{low}, {high}], got {value}"
            )
        validated[name] = float(value)
    return validated


def _coalesce_state_edit(
    roll_dir: Path,
    negative_id: str,
    op: str,
    params: dict[str, float | None],
) -> dict:
    """Append or update the trailing state op (`tone` or `color`)."""
    from scanny_boy.roll_manifest import _now_iso

    with _session() as session:
        negative = _negative_row(session, roll_dir, negative_id)
        last = session.scalar(
            select(EditRow)
            .where(EditRow.negative_id == negative.negative_id)
            .order_by(EditRow.position.desc())
            .limit(1)
        )
        if last is not None and last.op == op:
            last.params = params
            last.created_at = _now_iso()
            session.flush()
            return {
                "id": last.id,
                "negative_id": last.negative_id,
                "position": last.position,
                "op": last.op,
                "params": last.params,
                "created_at": last.created_at,
            }
        position = (
            session.scalar(
                select(func.max(EditRow.position)).where(
                    EditRow.negative_id == negative.negative_id
                )
            )
            or 0
        ) + 1
        row = EditRow(
            negative_id=negative.negative_id,
            position=position,
            op=op,
            params=params,
            created_at=_now_iso(),
        )
        session.add(row)
        session.flush()
        return {
            "id": row.id,
            "negative_id": row.negative_id,
            "position": row.position,
            "op": row.op,
            "params": row.params,
            "created_at": row.created_at,
        }


def append_tone_edit(
    roll_dir: Path,
    negative_id: str,
    params: dict[str, float | None] | None,
) -> dict:
    """Records the negative's preview tone adjustment (see `tone.py`), or
    the reset when every param is `None`.

    The op is a state, not a transform — the latest one wins — so when the
    log's last entry is already a `tone` op it is updated in place rather
    than appended behind (the one coalescing exception to the log's
    append-only discipline; slider commits would otherwise pile up dead
    rows). Returns the row as a dict, same shape as `append_edit`'s.
    Raises `ValueError` on out-of-range or mismatched params."""

    validated = validated_tone_params(params)
    return _coalesce_state_edit(roll_dir, negative_id, TONE_OP, validated)


def append_color_edit(
    roll_dir: Path,
    negative_id: str,
    params: dict[str, float | None] | None,
) -> dict:
    """Records the negative's preview colour adjustment (see `color.py`), or
    the reset when every param is `None`. Coalesces a trailing `color` op in
    place. Raises `ValueError` on out-of-range or mismatched params."""
    validated = validated_color_params(params)
    return _coalesce_state_edit(roll_dir, negative_id, COLOR_OP, validated)


def append_spots_edit(roll_dir: Path, negative_id: str, params: dict) -> dict:
    """Records the negative's spot proposals and repair switch (see
    `spots.py`), coalescing a trailing `spots` op in place like `tone` and
    `color` — it is a state, not a transform. Raises `ValueError` on
    malformed params."""
    validated = validated_spots_params(params)
    return _coalesce_state_edit(roll_dir, negative_id, SPOTS_OP, validated)


def _tone_neutral_defaults() -> dict[str, float]:
    from scanny_boy import tone

    return {
        "grade_r": tone.GRADE_REFERENCE,
        "snap_gamma": 0.0,
        "density": tone.DENSITY_REFERENCE,
        "shadow_density": 0.0,
        "highlight_density": 0.0,
        "toe": 0.0,
        "toe_width": tone.WIDTH_REFERENCE,
        "shoulder": 0.0,
        "shoulder_width": tone.WIDTH_REFERENCE,
    }


def _color_neutral_defaults() -> dict[str, float]:
    return {
        "wb_cyan": 0.0,
        "wb_magenta": 0.0,
        "wb_yellow": 0.0,
        "shadow_cyan": 0.0,
        "shadow_magenta": 0.0,
        "shadow_yellow": 0.0,
        "highlight_cyan": 0.0,
        "highlight_magenta": 0.0,
        "highlight_yellow": 0.0,
        "cast_removal": 0.0,
        "cast_removal_highlights": 0.0,
        "dye_separation": 1.0,
        "separation_damping": 0.0,
    }


def _color_in_range(params: dict[str, float]) -> bool:
    from scanny_boy import color

    for name, low, high in color._color_param_bounds():
        value = params[name]
        if not low <= value <= high:
            return False
    return True


def _tone_in_range(params: dict[str, float]) -> bool:
    for name, low, high in _tone_param_bounds():
        value = params[name]
        if not low <= value <= high:
            return False
    return True


def _parse_tone_op(params: dict) -> dict[str, float] | None:
    grade = params.get("grade_r")
    snap = params.get("snap_gamma")
    if grade is None or snap is None:
        return None
    if isinstance(grade, bool) or not isinstance(grade, (int, float)):
        return None
    if isinstance(snap, bool) or not isinstance(snap, (int, float)):
        return None
    merged = _tone_neutral_defaults()
    merged["grade_r"] = float(grade)
    merged["snap_gamma"] = float(snap)
    for key in merged:
        if key in ("grade_r", "snap_gamma"):
            continue
        value = params.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        merged[key] = float(value)
    if not _tone_in_range(merged):
        return None
    return merged


def _parse_color_op(params: dict) -> dict[str, float] | None:
    from scanny_boy import color

    # Gate on the ORIGINAL twelve (docs/CAST_REMOVAL_PLAN.md R-2 §6.1): an
    # op written before that plan has no thirteenth key and is still a
    # complete colour state. Newer keys fall back to their neutral
    # defaults, which is what "this op predates the control" means.
    if not all(key in params for key in color.COLOR_PARAM_KEYS_V1):
        return None
    if all(params.get(key) is None for key in color.COLOR_PARAM_KEYS_V1):
        return None
    merged = _color_neutral_defaults()
    for key in merged:
        value = params.get(key)
        if value is None:
            if key in color.COLOR_PARAM_KEYS_V1:
                return None  # a null among the twelve is still a reset
            continue  # a missing newer key keeps its default
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        merged[key] = float(value)
    if not _color_in_range(merged):
        return None
    return merged


_SPOT_CORE_KEYS = ("id", "kind", "polarity", "bbox", "rle")
_SPOT_KINDS = ("blob", "streak")
_SPOT_POLARITIES = ("dense", "thin")


def _check_spots_params(params: dict) -> dict:
    """The shared validation behind `validated_spots_params` (which raises)
    and `_parse_spots_op` (which degrades to `None`). Unknown keys — on the
    params and on each spot — are ignored, not rejected: a parser that
    demands an exact key set silently discards real user state the day the
    set grows (CAST_REMOVAL_PLAN §6.1's cautionary tale)."""
    from scanny_boy import spots

    version = params.get("detector_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError("spots detector_version must be an int")  # noqa: TRY004
    if version > spots.DETECTOR_VERSION:
        raise ValueError(
            f"spots detector_version {version} is newer than this build's "
            f"{spots.DETECTOR_VERSION}"
        )
    repair = params.get("repair")
    if not isinstance(repair, bool):
        raise ValueError("spots repair must be a bool")  # noqa: TRY004
    sensitivity = params.get("sensitivity")
    if isinstance(sensitivity, bool) or not isinstance(sensitivity, (int, float)):
        raise ValueError("spots sensitivity must be a number")  # noqa: TRY004
    if not 0.0 <= float(sensitivity) <= 1.0:
        raise ValueError(f"spots sensitivity must be within [0, 1], got {sensitivity}")
    canvas = params.get("canvas")
    if (
        not isinstance(canvas, list)
        or len(canvas) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in canvas
        )
    ):
        raise ValueError("spots canvas must be [width, height], two positive ints")
    spot_list = params.get("spots")
    if not isinstance(spot_list, list):
        raise ValueError("spots must be a list")  # noqa: TRY004
    checked_spots: list[dict] = []
    for spot in spot_list:
        if not isinstance(spot, dict):
            raise ValueError("each spot must be an object")  # noqa: TRY004
        missing = [key for key in _SPOT_CORE_KEYS if key not in spot]
        if missing:
            raise ValueError(f"spot missing keys: {', '.join(missing)}")
        if spot["kind"] not in _SPOT_KINDS:
            raise ValueError(f"spot kind must be one of {list(_SPOT_KINDS)}")
        if spot["polarity"] not in _SPOT_POLARITIES:
            raise ValueError(f"spot polarity must be one of {list(_SPOT_POLARITIES)}")
        bbox = spot["bbox"]
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or any(
                isinstance(value, bool) or not isinstance(value, int) for value in bbox
            )
        ):
            raise ValueError("spot bbox must be [x, y, width, height], four ints")
        if bbox[2] <= 0 or bbox[3] <= 0:
            raise ValueError("spot bbox must have positive width and height")
        rle = spot["rle"]
        if (
            not isinstance(rle, list)
            or not rle
            or any(
                isinstance(run, bool) or not isinstance(run, int) or run < 0
                for run in rle
            )
        ):
            raise ValueError("spot rle must be a list of non-negative ints")
        if sum(rle) != bbox[2] * bbox[3]:
            raise ValueError(
                "spot rle must sum to the bbox area "
                f"({sum(rle)} != {bbox[2]} * {bbox[3]})"
            )
        checked_spots.append(dict(spot))
    return {
        "detector_version": version,
        "sensitivity": float(sensitivity),
        "repair": repair,
        "canvas": list(canvas),
        "spots": checked_spots,
    }


def validated_spots_params(params: dict) -> dict:
    """The `spots` op's params, validated. Raises `ValueError` with a
    specific message for each malformed shape; `edits.py` turns those into
    `INVALID_EDIT`. The parser cannot check `canvas` against a real image —
    it has none — so that comparison stays in `spots.py` (SPOTTING_PLAN
    §1.5)."""
    if not isinstance(params, dict):
        raise ValueError("spots params must be an object")  # noqa: TRY004
    return _check_spots_params(params)


def _parse_spots_op(params: dict) -> dict | None:
    """The `spots` op as replayed into `EditState`. Never raises: anything
    malformed degrades to `None`, which means *no markers and no repair* —
    degrading toward "change no pixels" is the only safe direction
    (SPOTTING_PLAN §5)."""
    if not isinstance(params, dict):
        return None
    try:
        return _check_spots_params(params)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def validated_crop_params(
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """The `crop` op's params, validated: a `{"reset": true}` op needs
    nothing else; a real window needs the rect, the tilt, and the canvas
    it was recorded against. Raises `ValueError` on anything else — the
    caller (`run_edit_crop`) validates before anything is written."""
    if not isinstance(params, dict):
        raise ValueError("crop params must be an object")  # noqa: TRY004 — matches `_check_spots_params`
    if params.get("reset"):
        return {"reset": True}
    for key in ("canvas", "x", "y", "w", "h", "tilt_deg"):
        if key not in params:
            raise ValueError(f"crop params missing {key!r}")
    canvas = params["canvas"]
    if (
        not isinstance(canvas, (list, tuple))
        or len(canvas) != 2
        or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in canvas)
    ):
        raise ValueError(f"crop canvas must be [width, height] ints, got {canvas!r}")
    for key in ("x", "y", "w", "h"):
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"crop {key} must be a non-negative int, got {value!r}")
    tilt = params["tilt_deg"]
    if isinstance(tilt, bool) or not isinstance(tilt, (int, float)):
        raise ValueError(f"crop tilt_deg must be a number, got {tilt!r}")  # noqa: TRY004
    if abs(float(tilt)) > CROP_TILT_MAX_DEG + 1e-9:
        raise ValueError(
            f"crop tilt_deg must be within ±{CROP_TILT_MAX_DEG}, got {tilt}"
        )
    if params["w"] < CROP_MIN_SIZE_PX or params["h"] < CROP_MIN_SIZE_PX:
        raise ValueError(
            f"crop window must be at least {CROP_MIN_SIZE_PX}x{CROP_MIN_SIZE_PX}, "
            f"got {params['w']}x{params['h']}"
        )
    validated: dict[str, Any] = {
        "canvas": [int(canvas[0]), int(canvas[1])],
        "x": int(params["x"]),
        "y": int(params["y"]),
        "w": int(params["w"]),
        "h": int(params["h"]),
        "tilt_deg": round(float(tilt), 2),
    }
    preset = params.get("preset")
    if preset is not None:
        if not isinstance(preset, str):
            raise ValueError(f"crop preset must be a string, got {preset!r}")
        validated["preset"] = preset
    return validated


def _parse_crop_op(params: dict) -> dict | None:
    """The `crop` op as replayed into `EditState`. Never raises: anything
    malformed degrades to `None` — a missing or unreadable crop leaves the
    full frame, the same degrade-toward-no-op direction as `tone`'s."""
    if not isinstance(params, dict):
        return None
    if not params or params.get("reset"):
        return None
    try:
        return validated_crop_params(params)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def edits_for(roll_dir: Path, negative_id: str) -> list[dict]:
    with _session() as session:
        negative = _negative_row(session, roll_dir, negative_id)
        rows = session.scalars(
            select(EditRow)
            .where(EditRow.negative_id == negative.negative_id)
            .order_by(EditRow.position)
        ).all()
        return [
            {
                "id": row.id,
                "negative_id": row.negative_id,
                "position": row.position,
                "op": row.op,
                "params": row.params,
                "created_at": row.created_at,
            }
            for row in rows
        ]


def net_edit_state(roll_dir: Path, negative_id: str) -> EditState:
    """Replays the negative's edit ops in order and reduces them to the
    canonical net state. Geometric ops compose; `tone`, `color`, `spots`,
    and `crop` are states where only the latest op of each kind matters.
    Unknown ops are skipped; malformed state ops degrade to no adjustment."""
    turns = 0
    flipped = False
    fine_deg = 0.0
    tone: dict[str, float] | None = None
    color: dict[str, float] | None = None
    spots: dict | None = None
    crop: dict | None = None
    for edit in edits_for(roll_dir, negative_id):
        op = edit["op"]
        if op == ROTATE_OP:
            direction = edit["params"].get("direction")
            if direction not in _DIRECTIONS:
                continue
            turns += _DIRECTIONS[direction]
        elif op == FLIP_OP:
            flipped = not flipped
            turns = -turns
            fine_deg = -fine_deg
        elif op == ROTATE_FINE_OP:
            angle = edit["params"].get("angle_deg")
            if isinstance(angle, bool) or not isinstance(angle, (int, float)):
                continue
            fine_deg += float(angle)
        elif op == TONE_OP:
            parsed = _parse_tone_op(edit["params"])
            tone = parsed
        elif op == COLOR_OP:
            parsed = _parse_color_op(edit["params"])
            color = parsed
        elif op == SPOTS_OP:
            # The net spots state is TIFF-space geometry; it does not move
            # when the display transform does.
            spots = _parse_spots_op(edit["params"])
        elif op == CROP_OP:
            # Same family: TIFF-space geometry, a state where only the
            # latest op matters (each op already stores the fully-composed
            # window).
            crop = _parse_crop_op(edit["params"])
    return EditState(
        quarter_turns=turns % 4,
        flipped=flipped,
        fine_angle_deg=fine_deg,
        tone=tone,
        color=color,
        spots=spots,
        crop=crop,
    )


def net_rotation_quarter_turns(roll_dir: Path, negative_id: str) -> int:
    """The rotation half of `net_edit_state` — kept for callers that only
    care about orientation."""
    return net_edit_state(roll_dir, negative_id).quarter_turns


# --- flat-field profiles -----------------------------------------------------


def _flatfield_profile_row(session: Session, profile_id: str) -> FlatFieldProfileRow:
    row = session.get(FlatFieldProfileRow, profile_id)
    if row is None:
        raise FlatFieldError(
            Code.FLATFIELD_PROFILE_NOT_FOUND,
            f"no flat-field profile with id {profile_id}",
        )
    return row


def _to_flatfield_profile(row: FlatFieldProfileRow) -> FlatFieldProfile:
    return FlatFieldProfile(
        profile_id=row.profile_id,
        name=row.name,
        gain_map_path=row.gain_map_path,
        gain_map_sha256=row.gain_map_sha256,
        source_path=row.source_path,
        reference_width=row.reference_width,
        reference_height=row.reference_height,
        params=dict(row.params),
        scanny_boy_version=row.scanny_boy_version,
        created_at=row.created_at,
        board_key=row.board_key,
        geometry=row.geometry,
        chromatic_aberration=row.chromatic_aberration,
        calibration_report=row.calibration_report,
    )


def save_flatfield_profile(profile: FlatFieldProfile) -> None:
    """Upserts one profile row. Profile records are immutable once created —
    `name` is not in the roll token precisely so renaming stays possible,
    but nothing here needs to rewrite one today."""
    with _session() as session:
        session.merge(
            FlatFieldProfileRow(
                profile_id=profile.profile_id,
                name=profile.name,
                gain_map_path=profile.gain_map_path,
                gain_map_sha256=profile.gain_map_sha256,
                source_path=profile.source_path,
                reference_width=profile.reference_width,
                reference_height=profile.reference_height,
                params=profile.params,
                scanny_boy_version=profile.scanny_boy_version,
                created_at=profile.created_at,
                board_key=profile.board_key,
                geometry=profile.geometry,
                chromatic_aberration=profile.chromatic_aberration,
                calibration_report=profile.calibration_report,
            )
        )


def list_flatfield_profiles() -> list[FlatFieldProfile]:
    with _session() as session:
        rows = session.scalars(
            select(FlatFieldProfileRow).order_by(
                FlatFieldProfileRow.created_at, FlatFieldProfileRow.name
            )
        ).all()
        return [_to_flatfield_profile(row) for row in rows]


def load_flatfield_profile(profile_id: str) -> FlatFieldProfile:
    with _session() as session:
        return _to_flatfield_profile(_flatfield_profile_row(session, profile_id))


def delete_flatfield_profile(profile_id: str) -> None:
    with _session() as session:
        row = _flatfield_profile_row(session, profile_id)
        session.delete(row)


def rolls_using_flatfield(profile_id: str) -> list[str]:
    """Every roll whose `processing_params.flat_field.profile_id` names
    `profile_id`. `processing_params` is an open JSON object the CLI wrote,
    so the match is made on the decoded value, not a string pattern."""
    with _session() as session:
        rows = session.scalars(select(RollRow)).all()
        return sorted(
            roll.roll_id
            for roll in rows
            if (roll.processing_params or {}).get("flat_field", {}).get("profile_id")
            == profile_id
        )


def rolls_using_profile_geometry(profile_id: str) -> list[str]:
    """Every roll whose `stitch_params.geometry.profile_id` names
    `profile_id` — the stitch-side half of the two invariant buckets
    (docs/GEOMETRIC_PLAN.md section 3.6). A profile whose geometry a roll
    depends on is exactly as undeletable as one whose gain map it depends
    on; `flatfield delete` unions this with `rolls_using_flatfield`."""
    with _session() as session:
        rows = session.scalars(select(RollRow)).all()
        return sorted(
            roll.roll_id
            for roll in rows
            if (roll.stitch_params or {}).get("geometry", {}).get("profile_id")
            == profile_id
        )


# --- grid configuration presets ----------------------------------------------


def _grid_profile_row(session: Session, profile_id: str) -> GridProfileRow:
    row = session.get(GridProfileRow, profile_id)
    if row is None:
        raise GridProfileError(
            Code.GRID_PROFILE_NOT_FOUND,
            f"no grid configuration with id {profile_id}",
        )
    return row


def _to_grid_profile(row: GridProfileRow) -> GridProfile:
    return GridProfile(
        profile_id=row.profile_id,
        name=row.name,
        across=row.across,
        down=row.down,
        created_at=row.created_at,
    )


def save_grid_profile(profile: GridProfile) -> None:
    with _session() as session:
        session.merge(
            GridProfileRow(
                profile_id=profile.profile_id,
                name=profile.name,
                across=profile.across,
                down=profile.down,
                created_at=profile.created_at,
            )
        )


def list_grid_profiles() -> list[GridProfile]:
    with _session() as session:
        rows = session.scalars(
            select(GridProfileRow).order_by(
                GridProfileRow.created_at, GridProfileRow.name
            )
        ).all()
        return [_to_grid_profile(row) for row in rows]


def load_grid_profile(profile_id: str) -> GridProfile:
    with _session() as session:
        return _to_grid_profile(_grid_profile_row(session, profile_id))


def load_grid_profile_by_name(name: str) -> GridProfile:
    with _session() as session:
        row = session.scalar(
            select(GridProfileRow).where(GridProfileRow.name == name)
        )
        if row is None:
            raise GridProfileError(
                Code.GRID_PROFILE_NOT_FOUND,
                f"no grid configuration named {name!r}",
            )
        return _to_grid_profile(row)


def delete_grid_profile(profile_id: str) -> None:
    with _session() as session:
        row = _grid_profile_row(session, profile_id)
        session.delete(row)


# --- the extended-metadata value catalog -------------------------------------

# Bounded so a long-typed history cannot make the typeahead unbounded work;
# the most-recently-used head of the list is what matters anyway.
METADATA_VALUES_LIMIT = 200


def upsert_metadata_values(field: str, values: list[str]) -> None:
    """Remembers each value as a catalog entry for `field`, bumping
    `last_used_at` for entries that already exist. Idempotent per (field,
    value) pair — the catalog records *that* a value was used, not how
    often."""
    from scanny_boy.roll_manifest import _now_iso

    with _session() as session:
        for value in values:
            row = session.scalar(
                select(MetadataValueRow).where(
                    MetadataValueRow.field == field,
                    MetadataValueRow.value == value,
                )
            )
            if row is None:
                session.add(
                    MetadataValueRow(field=field, value=value, last_used_at=_now_iso())
                )
            else:
                row.last_used_at = _now_iso()


def list_metadata_values(field: str) -> list[str]:
    """The catalog's values for `field`, most-recently-used first."""
    with _session() as session:
        rows = session.scalars(
            select(MetadataValueRow)
            .where(MetadataValueRow.field == field)
            .order_by(MetadataValueRow.last_used_at.desc(), MetadataValueRow.id.desc())
            .limit(METADATA_VALUES_LIMIT)
        ).all()
        return [row.value for row in rows]
