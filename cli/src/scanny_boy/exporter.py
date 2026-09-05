"""`export --roll DIR --output DIR`: the moment edits become pixels.

The exporter replays each negative's ordered ops log over its published
TIFF — the canonical `(quarter_turns, flipped)` net transform, applied as a
horizontal mirror followed by `np.rot90` quarter turns — and
writes the result, named after the negative, into the output folder. The
roll's own TIFF is never opened for writing: exports land elsewhere, and a
re-export after further edits simply runs again.

The export *is* a normalized digital negative: the pixels replay straight
through, carrying the same density profile the published TIFF carries
(docs/DECISIONS.md, "Normalization decisions"), and the negative's
`normalization` block is written into the `ImageDescription` so the file is
interpretable without the database. The database's metadata — capture time,
camera, lens, city, state, caption — is also written here, by
`export_metadata.write_export_metadata`'s second pass: export is the only
place metadata reaches a TIFF. A metadata write that fails downgrades to a
`METADATA_WRITE_FAILED` warning and the export still counts — the pixels
are good, and re-exporting after fixing the metadata is cheap.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import numpy as np
import tifffile

from scanny_boy.auto_rotate import rotate_with_fill
from scanny_boy.events import Code, ExportDone, WarningEvent
from scanny_boy.export_metadata import (
    export_metadata_for,
    write_export_metadata,
)
from scanny_boy.icc_profile import ProfileKind, load_icc_profile
from scanny_boy.library import repo
from scanny_boy.library.repo import RollNotRegisteredError
from scanny_boy.manifest import BadManifestError
from scanny_boy.roll_manifest import NegativeRecord, RollManifest, load_roll_manifest

EmitFn = Any


class ExportFailure(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class ExportOutcome:
    exported: list[str]
    failed: list[str]


def apply_edits(
    image: np.ndarray,
    rotation_quarter_turns: int,
    flipped_horizontally: bool = False,
    fine_angle_deg: float = 0.0,
) -> np.ndarray:
    """The single place an op log meets pixels: pure, ordered, and the same
    replay the preview generator performs at thumbnail scale. The canonical
    net transform mirrors the original horizontally first (when flipped),
    then applies the fine auto-rotation (`auto_rotate.rotate_with_fill`:
    the rotation keeps the canvas dimensions and fills what it uncovers
    with the stitching fill sentinel), then rotates. Quarter turns count
    clockwise, the fine angle counts clockwise too; np.rot90 turns
    counter-clockwise, so negate."""
    if flipped_horizontally:
        image = np.ascontiguousarray(image[:, ::-1])
    if abs(fine_angle_deg) >= 1e-9:
        image = rotate_with_fill(image, fine_angle_deg)
    return np.rot90(image, k=(-rotation_quarter_turns) % 4)


def export_image_description(negative: NegativeRecord) -> str:
    """The export's `ImageDescription`: the negative's `normalization`
    block as JSON, so the file is interpretable without the database."""
    return json.dumps(
        {
            "kind": "scanny-boy export",
            "negative_id": negative.negative_id,
            "normalization": negative.normalization,
            "normalized_fill": negative.normalized_fill,
        },
        sort_keys=True,
    )


def _write_export(
    destination: Path, image: np.ndarray, negative: NegativeRecord
) -> None:
    tmp_path = destination.with_suffix(destination.suffix + ".tmp")
    try:
        tifffile.imwrite(
            tmp_path,
            image,
            description=export_image_description(negative),
            # MONOCHROME_PLAN section 4: a mono roll's published TIFF is
            # single-channel and carries the grey density profile.
            iccprofile=load_icc_profile(
                ProfileKind.DENSITY_GREY if image.ndim == 2 else ProfileKind.DENSITY
            ),
        )
        tmp_path.replace(destination)
    except BaseException:
        # A failed write (disk full, permissions) must not leave a partial
        # .tmp file behind in the user's output folder — the same rule
        # apply_metadata.rewrite_date_time_original already follows.
        tmp_path.unlink(missing_ok=True)
        raise


def run_export(
    roll_dir: Path,
    output_dir: Path,
    negative_ids: list[str],
    *,
    emit: EmitFn,
) -> ExportOutcome:
    """Exports the roll's negatives (all of them, or the requested ids) with
    their edits applied. Raises `ExportFailure` when the roll itself can't
    be read; one negative's problem is a warning plus a `failed` entry, and
    never stops the rest."""
    if not repo.roll_registered(roll_dir):
        raise ExportFailure(
            Code.ROLL_NOT_FOUND,
            f"{roll_dir} is not a registered roll; create the roll first",
        )

    try:
        roll = load_roll_manifest(roll_dir)
    except (BadManifestError, RollNotRegisteredError) as exc:
        raise ExportFailure(exc.code, exc.message) from exc

    if negative_ids:
        known = {n.negative_id for n in roll.negatives}
        unknown = [nid for nid in negative_ids if nid not in known]
        if unknown:
            raise ExportFailure(
                Code.NEGATIVE_NOT_FOUND,
                f"{roll_dir} has no negative(s) {', '.join(unknown)}",
            )
        negatives = [n for n in roll.negatives if n.negative_id in set(negative_ids)]
    else:
        negatives = list(roll.negatives)

    if not negatives:
        raise ExportFailure(
            Code.NEGATIVE_NOT_FOUND,
            f"{roll_dir} has no negatives to export",
        )

    if not output_dir.exists():
        try:
            output_dir.mkdir(parents=True)
        except OSError as exc:
            raise ExportFailure(
                Code.OUTPUT_NOT_WRITABLE, f"could not create {output_dir}: {exc}"
            ) from exc
    if not output_dir.is_dir():
        raise ExportFailure(
            Code.OUTPUT_NOT_WRITABLE, f"{output_dir} is not a directory"
        )

    exported: list[str] = []
    failed: list[str] = []

    for negative in negatives:
        assert isinstance(negative, NegativeRecord)
        result = _export_negative(roll_dir, output_dir, roll, negative, emit)
        if result is None:
            failed.append(negative.negative_id)
        else:
            name, width, height = result
            exported.append(name)
            emit(
                ExportDone(
                    negative_id=negative.negative_id,
                    output=name,
                    width=width,
                    height=height,
                )
            )

    return ExportOutcome(exported=exported, failed=failed)


def _export_negative(
    roll_dir: Path,
    output_dir: Path,
    roll: RollManifest,
    negative: NegativeRecord,
    emit: EmitFn,
) -> tuple[str, int, int] | None:
    if negative.output is None or negative.status != "completed":
        emit(
            WarningEvent(
                code=Code.NEGATIVE_NOT_FOUND,
                message=f"{negative.negative_id} has not been stitched; skipped",
            )
        )
        return None

    tiff_path = Path(roll_dir) / negative.output["name"]
    if not tiff_path.exists():
        emit(
            WarningEvent(
                code=Code.NEGATIVE_NOT_FOUND,
                message=f"{tiff_path} is missing; skipped",
            )
        )
        return None

    try:
        image = tifffile.imread(tiff_path)
        quarter_turns, flipped, fine_angle, _tone = repo.net_edit_state(
            roll_dir, negative.negative_id
        )
        rotated = apply_edits(image, quarter_turns, flipped, fine_angle)
        destination = output_dir / negative.output["name"]
        _write_export(destination, rotated, negative)
        _write_metadata(destination, roll, negative, emit)
    except Exception as exc:  # noqa: BLE001 — one bad negative never stops the export
        emit(
            WarningEvent(
                code=Code.EXPORT_FAILED,
                message=f"could not export {negative.negative_id}: {exc}",
            )
        )
        return None

    height, width = rotated.shape[0], rotated.shape[1]
    return destination.name, width, height


def _write_metadata(
    destination: Path,
    roll: RollManifest,
    negative: NegativeRecord,
    emit: EmitFn,
) -> None:
    """The second pass that puts the database's metadata into the exported
    TIFF. A failure here downgrades to a `METADATA_WRITE_FAILED` warning —
    it must not lose the export that already succeeded above."""
    metadata = export_metadata_for(roll, negative)
    if not metadata.has_any:
        return
    try:
        write_export_metadata(destination, metadata)
    except Exception as exc:  # noqa: BLE001 — metadata never loses pixels
        emit(
            WarningEvent(
                code=Code.METADATA_WRITE_FAILED,
                message=(
                    f"exported {negative.negative_id} but could not write its "
                    f"metadata: {exc}"
                ),
            )
        )
