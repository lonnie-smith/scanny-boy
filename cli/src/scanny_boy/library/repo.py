"""The persistence repository: `RollManifest` dataclasses to and from the
library database.

`save_roll` upserts the whole manifest keyed by `roll_id` — the roll row's
`folder_path` is updated on every save, which is what makes `roll rename`'s
folder move a data update rather than a special case. Children are diffed by
key (`run_id`, `sha256`, `negative_id`): rows the incoming manifest no
longer describes are deleted, rows it does describe are merged in place. A
negative's `edits` rows hang off its stable `negative_id`, so re-stitching a
negative — which keeps its id — keeps its edit history, while removing a
negative (adoption's removal path) cascades its edits away with it.

Load and save are deliberately the only two shapes the rest of the program
sees: everything is a plain `RollManifest` in memory, exactly as it was when
the manifest was a JSON file.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from scanny_boy.events import Code
from scanny_boy.flatfield import FlatFieldError, FlatFieldProfile
from scanny_boy.library.db import open_engine
from scanny_boy.library.models import (
    EditRow,
    FlatFieldProfileRow,
    NegativeRow,
    RollRow,
    RunRow,
    SourceRow,
)

if TYPE_CHECKING:
    from scanny_boy.roll_manifest import RollManifest

# The only edit operation the proof of concept implements. Rotation params
# are `{"direction": "cw" | "ccw"}`; quarter turns compose by replay.
ROTATE_OP = "rotate"
_DIRECTIONS = {"cw": 1, "ccw": -1}

# The gain a frame record carries when the row predates gain normalization
# and never had one written: unity, since nothing was applied.
_UNITY_GAIN = (1.0, 1.0, 1.0)


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
        roll.stitch_params = manifest.stitch_params
        roll.roll_capture_date = manifest.metadata.roll_capture_date
        roll.last_applied_at = manifest.metadata.last_applied_at

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
                )
            )

        source_hashes = {s.sha256 for s in manifest.sources}
        session.execute(
            delete(SourceRow).where(
                SourceRow.roll_id == manifest.roll_id,
                SourceRow.sha256.not_in(source_hashes),
            )
        )
        for ordinal, source in enumerate(manifest.sources):
            assert isinstance(source, RollSourceRecord)
            session.merge(
                SourceRow(
                    roll_id=manifest.roll_id,
                    ordinal=ordinal,
                    filename=source.filename,
                    absolute_path=source.absolute_path,
                    size=source.size,
                    mtime=source.mtime,
                    sha256=source.sha256,
                    run_id=source.run_id,
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
                    rebate_deviation_px=negative.rebate_deviation_px,
                    used_clahe_fallback=negative.used_clahe_fallback,
                    error_code=negative.error_code,
                    error_message=negative.error_message,
                    capture_time=negative.capture_time.to_dict(),
                    preview_path=negative.preview_path,
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


def load_roll(roll_dir: Path) -> RollManifest:
    from scanny_boy.roll_manifest import (
        CaptureTime,
        FrameRecord,
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
            stitch_params=roll.stitch_params,
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
                    rebate_deviation_px=n.rebate_deviation_px,
                    used_clahe_fallback=bool(n.used_clahe_fallback),
                    error_code=n.error_code,
                    error_message=n.error_message,
                    capture_time=CaptureTime(**n.capture_time),
                    preview_path=n.preview_path,
                )
                for n in negatives
            ],
            metadata=RollMetadata(
                roll_capture_date=roll.roll_capture_date,
                last_applied_at=roll.last_applied_at,
            ),
        )


# --- the edits ops log ------------------------------------------------------


def _negative_row(session: Session, roll_dir: Path, negative_id: str) -> NegativeRow:
    negative = session.get(NegativeRow, negative_id)
    if negative is None:
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


def net_rotation_quarter_turns(roll_dir: Path, negative_id: str) -> int:
    """Replays the negative's rotate ops in order and reduces them to net
    clockwise quarter turns, normalised to 0-3. The single number every
    consumer needs: the preview generator's lossless `np.rot90` and the
    exporter's `np.rot90(k=...)` are both driven from it."""
    turns = 0
    for edit in edits_for(roll_dir, negative_id):
        if edit["op"] != ROTATE_OP:
            continue
        direction = edit["params"].get("direction")
        if direction not in _DIRECTIONS:
            continue
        turns += _DIRECTIONS[direction]
    return turns % 4


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
            select(FlatFieldProfileRow).order_by(FlatFieldProfileRow.created_at, FlatFieldProfileRow.name)
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
