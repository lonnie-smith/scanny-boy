"""`export --roll DIR --output DIR`: the moment edits become pixels.

The exporter replays each negative's ordered ops log over its published
TIFF — the canonical `(quarter_turns, flipped)` net transform, applied as a
horizontal mirror followed by `np.rot90` quarter turns — and renders the
result as a **positive in Adobe RGB (1998)-compatible colour, with the
negative's recorded tone op baked in** (`render.render_export`), written as
a **16-bit lossless JPEG XL** with the export ICC profile embedded
(docs/EXPORT_PLAN.md §5). A mono roll's export is single-channel, tagged
with the grey export profile and rendered without a colour matrix. The
roll's own TIFF is never opened for writing: exports land elsewhere, and a
re-export after further edits simply runs again.

A colour roll predating the `camera_color` block fails the export outright
(`CAMERA_MATRIX_MISSING`, raised once before anything is written): a silent
identity matrix would produce a file that claims Adobe RGB and is not,
which is precisely the bug the export plan exists to remove (§3.4). A mono
roll needs no matrix and is never failed for its absence.

The database's metadata — capture time, camera, lens, city, state,
caption — is built into the Exif and XMP boxes by `export_metadata` at
encode time; the XMP also carries the `scannyboy:provenance` record (the
file's interpretability record: the published TIFF's normalization block
plus what the render actually did). A malformed metadata value now fails
the negative outright — there is no second write left to downgrade, and
the file was never written.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import numpy as np
import tifffile

from scanny_boy import jxl_writer, render
from scanny_boy.auto_rotate import rotate_with_fill
from scanny_boy.events import Code, ExportDone, WarningEvent
from scanny_boy.export_metadata import (
    build_exif,
    build_xmp,
    export_metadata_for,
)
from scanny_boy.icc_profile import (
    ProfileKind,
    export_profile_kind,
    load_icc_profile,
    profile_record,
)
from scanny_boy.library import repo
from scanny_boy.library.repo import RollNotRegisteredError
from scanny_boy.manifest import BadManifestError
from scanny_boy.roll_manifest import NegativeRecord, RollManifest, load_roll_manifest

EmitFn = Any

EXPORT_IMAGE_DESCRIPTION_SUFFIX = ": Scanny Boy export"


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
    """The export's `ImageDescription`: the short human string. The
    interpretability record — the negative's `normalization` block —
    moved to the XMP's `scannyboy:provenance` (§5.2), where it belongs:
    after the render the pixels are no longer the encoded thing that
    record describes."""
    return f"{negative.negative_id}{EXPORT_IMAGE_DESCRIPTION_SUFFIX}"


def camera_matrix_for(roll: RollManifest) -> np.ndarray | None:
    """The export's 3x3 camera -> Adobe RGB matrix, or `None` for a roll
    with no recorded block — which only a mono roll may proceed without
    (§3.4: the caller gates on the published TIFF's channel count before
    calling this for colour). Built once per run: the block is a property
    of the camera body, frozen on the roll's first run."""
    if roll.camera_color is None:
        return None
    return render.export_matrix(roll.camera_color.rgb_xyz_matrix)


def provenance_record(
    negative: NegativeRecord,
    matrix: np.ndarray | None,
    tone_params: dict[str, float] | None,
    profile_kind: ProfileKind,
    clipped_fractions: tuple[float, ...],
) -> dict[str, Any]:
    """The `scannyboy:provenance` payload (§5.2): what makes an exported
    file interpretable without the database. The published TIFF's
    `normalization` block — the encoding the *published* file still
    carries — plus a `rendered` sibling recording what the export actually
    did to make the display pixels."""
    return {
        "kind": "scanny-boy export",
        "negative_id": negative.negative_id,
        # The published TIFF's encoding — the export no longer *is* this,
        # but the published file beside the export still is.
        "normalization": negative.normalization,
        "normalized_fill": negative.normalized_fill,
        "rendered": {
            "profile": profile_record(profile_kind),
            "gamma": render.GAMMA_ADOBE,
            "matrix": (
                None if matrix is None else np.asarray(matrix).tolist()
            ),
            "tone": None if tone_params is None else dict(tone_params),
            "clip_fractions": list(clipped_fractions),
        },
    }


def _first_published_channel_count(
    roll_dir: Path, negatives: list[NegativeRecord]
) -> int | None:
    """The first completed negative's published channel count, read from
    the TIFF's header only (cheap). `None` when there is nothing to peek
    at — the per-negative loop reports missing files itself."""
    for negative in negatives:
        if negative.output is None or negative.status != "completed":
            continue
        tiff_path = Path(roll_dir) / negative.output["name"]
        if not tiff_path.exists():
            continue
        with tifffile.TiffFile(tiff_path) as tif:
            page = tif.pages[0]
            return int(page.samplesperpixel)
    return None


def run_export(
    roll_dir: Path,
    output_dir: Path,
    negative_ids: list[str],
    *,
    emit: EmitFn,
) -> ExportOutcome:
    """Exports the roll's negatives (all of them, or the requested ids)
    as rendered positives in JPEG XL. Raises `ExportFailure` when the
    roll itself can't be read — including a colour roll predating the
    `camera_color` block (§3.4, raised once before anything is written);
    one negative's problem is a warning plus a `failed` entry, and never
    stops the rest."""
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

    # §3.4's gate, before anything is written: a **colour** roll with no
    # recorded `camera_color` predates the colour-managed export and must
    # be re-converted (or deleted). A **mono** roll does not need the
    # matrix and must not be failed for its absence (§4.5), so the check
    # is conditional on the published TIFF's channel count — the same fact
    # §4.5's render gates on. Peeking the first completed negative's
    # header keeps the failure "raised once, before anything is written".
    channels = _first_published_channel_count(roll_dir, negatives)
    if channels is not None and channels > 1 and roll.camera_color is None:
        raise ExportFailure(
            Code.CAMERA_MATRIX_MISSING,
            f"{roll_dir}'s roll manifest predates the colour-managed export: "
            "it records no camera_color block. Re-convert the roll (or "
            "delete it) so the export can colour-manage it.",
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
        # The net edit state's fourth element is the tone op this render
        # bakes in (§4.6) — no longer destructured away.
        quarter_turns, flipped, fine_angle, tone_params = repo.net_edit_state(
            roll_dir, negative.negative_id
        )
        rotated = apply_edits(image, quarter_turns, flipped, fine_angle)
        # §4.5: the matrix follows the channel count — `None` for a mono
        # roll's 2-D published TIFF, the recorded camera matrix for a
        # colour one. (When MONOCHROME_PLAN §2's film block lands, the
        # two agree by construction.)
        matrix = None if rotated.ndim == 2 else camera_matrix_for(roll)
        rendered, clipped_fractions = render.render_export(
            rotated, matrix, tone_params
        )
        profile_kind = export_profile_kind(
            1 if rendered.ndim == 2 else rendered.shape[2]
        )
        destination = output_dir / Path(negative.output["name"]).with_suffix(
            jxl_writer.JXL_SUFFIX
        )
        _write_export(
            destination,
            rendered,
            profile_kind,
            roll,
            negative,
            matrix,
            tone_params,
            clipped_fractions,
        )
    except jxl_writer.JxlEncoderUnavailable as exc:
        # A packaging failure, not a user error (§1.2): stop the export
        # and say what broke, rather than failing every negative with a
        # per-file warning.
        raise ExportFailure(Code.JXL_ENCODER_UNAVAILABLE, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — one bad negative never stops the export
        emit(
            WarningEvent(
                code=Code.EXPORT_FAILED,
                message=f"could not export {negative.negative_id}: {exc}",
            )
        )
        return None

    height, width = rendered.shape[0], rendered.shape[1]
    return destination.name, width, height


def _write_export(
    destination: Path,
    rendered: np.ndarray,
    profile_kind: ProfileKind,
    roll: RollManifest,
    negative: NegativeRecord,
    matrix: np.ndarray | None,
    tone_params: dict[str, float] | None,
    clipped_fractions: tuple[float, ...],
) -> None:
    """The single write: rendered pixels, the export ICC profile embedded,
    and the metadata boxes built at encode time. The `.tmp`-and-replace
    lives in `write_jxl` (§1.4), so there is no second pass and no nested
    tmp dance.

    The `has_any` guard: a field nobody set writes nothing — with no
    metadata at all there is no Exif box (§5.1). The XMP always goes,
    because the provenance record is not user-set metadata but the file's
    interpretability record (§5.2)."""
    metadata = export_metadata_for(roll, negative)
    exif = (
        build_exif(metadata, export_image_description(negative))
        if metadata.has_any
        else None
    )
    provenance = provenance_record(
        negative, matrix, tone_params, profile_kind, clipped_fractions
    )
    jxl_writer.write_jxl(
        destination,
        rendered,
        icc_profile=load_icc_profile(profile_kind),
        exif=exif,
        xmp=build_xmp(metadata, provenance),
    )
