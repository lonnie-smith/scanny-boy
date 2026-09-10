"""`export --roll DIR --output DIR`: the moment edits become pixels.

The exporter replays each negative's ordered ops log over its published
TIFF — the canonical `(quarter_turns, flipped)` net transform, applied as a
horizontal mirror followed by `np.rot90` quarter turns — and renders the
result as a **positive in Adobe RGB (1998)-compatible colour, with the
negative's recorded tone and colour ops baked in** (`render.render_export`), written as
a **16-bit lossless JPEG XL** with the export ICC profile embedded.
A mono roll's export is single-channel, tagged
with the grey export profile and rendered without a colour matrix. The
roll's own TIFF is never opened for writing: exports land elsewhere, and a
re-export after further edits simply runs again.

A colour roll predating the `camera_color` block fails the export outright
(`CAMERA_MATRIX_MISSING`, raised once before anything is written): a silent
identity matrix would produce a file that claims Adobe RGB and is not. A
mono roll needs no matrix and is never failed for its absence.

An optional downsampling reduces the export to a chosen long edge before
the encode (`--downsample 6048|9072|12096`). The resize itself lives inside the
render (`render.render_export`'s `long_edge`), where it belongs: a
Lanczos3 resample of the *linear-light* values, between the gamut clip
and the display re-encode — not a resample of the finished gamma-encoded
pixels, which would darken midtones along high-contrast edges. It never
upscales: an image already at or below the target is skipped silently,
and what was actually applied is recorded in the XMP's
`scannyboy:provenance` like every other thing the render did.

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

from scanny_boy import color, jxl_writer, previews, render, resample, spots
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

DOWNSAMPLE_CHOICES = ("none", "6048", "9072", "12096")


def parse_downsample(value: str) -> int | None:
    """The `--downsample` choice as the long edge it asks for, `None` for
    `none`. The choices themselves are pinned by `DOWNSAMPLE_CHOICES`."""
    if value == "none":
        return None
    if value not in DOWNSAMPLE_CHOICES:
        raise ValueError(
            f"invalid --downsample {value!r}; expected one of "
            + ", ".join(DOWNSAMPLE_CHOICES)
        )
    return int(value)


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
    crop_params: dict | None = None,
) -> np.ndarray:
    """The single place an op log meets pixels: pure, ordered, and the same
    replay the preview generator performs at thumbnail scale. The canonical
    net transform mirrors the original horizontally first (when flipped),
    then applies the fine auto-rotation (`auto_rotate.rotate_with_fill`:
    the rotation keeps the canvas dimensions and fills what it uncovers
    with the stitching fill sentinel), then rotates. Quarter turns count
    clockwise, the fine angle counts clockwise too; np.rot90 turns
    counter-clockwise, so negate. The `crop` op's window sits before all
    of it — its coordinates are published-TIFF pixels like the spots op's
    (`previews.apply_crop`: warp about the rect's centre by the stored
    tilt, then slice the rect) — so everything the crop uncovers from the
    log's later transforms lands on the cropped frame wholesale, and the
    exported file's dimensions are the cropped display's.
    """
    image = previews.apply_crop(image, crop_params)
    if flipped_horizontally:
        image = np.ascontiguousarray(image[:, ::-1])
    if abs(fine_angle_deg) >= 1e-9:
        image = rotate_with_fill(image, fine_angle_deg)
    return np.rot90(image, k=(-rotation_quarter_turns) % 4)


def applied_downsample(
    image: np.ndarray, long_edge: int | None
) -> int | None:
    """The long edge a downsample will actually apply to `image` — the
    target when the image exceeds it, `None` otherwise (`resample.
    target_size` is the decision; this is the provenance-facing shape of
    the same fact, since the render answers it again independently)."""
    size = resample.target_size(image.shape[0], image.shape[1], long_edge)
    return None if size is None else long_edge


def export_image_description(negative: NegativeRecord) -> str:
    """The export's `ImageDescription`: the short human string. The
    interpretability record — the negative's `normalization` block —
    moved to the XMP's `scannyboy:provenance`, where it belongs: after
    the render the pixels are no longer the encoded thing that record
    describes."""
    return f"{negative.negative_id}{EXPORT_IMAGE_DESCRIPTION_SUFFIX}"


def camera_matrix_for(roll: RollManifest) -> np.ndarray | None:
    """The export's 3x3 camera -> Adobe RGB matrix, or `None` for a roll
    with no recorded block — which only a mono roll may proceed without
    (the caller gates on the published TIFF's channel count before
    calling this for colour). Built once per run: the block is a property
    of the camera body, frozen on the roll's first run."""
    if roll.camera_color is None:
        return None
    return render.export_matrix(roll.camera_color.rgb_xyz_matrix)


def provenance_record(
    negative: NegativeRecord,
    matrix: np.ndarray | None,
    tone_params: dict[str, float] | None,
    color_params: dict[str, float] | None,
    profile_kind: ProfileKind,
    clipped_fractions: tuple[float, ...],
    spots_params: dict | None = None,
    crop_params: dict | None = None,
    applied_downsample: int | None = None,
) -> dict[str, Any]:
    """The `scannyboy:provenance` payload: what makes an exported
    file interpretable without the database. The published TIFF's
    `normalization` block — the encoding the *published* file still
    carries — plus a `rendered` sibling recording what the export actually
    did to make the display pixels. The `spots` entry records the repair:
    "some pixels here are interpolated" is exactly the kind of thing the
    XMP exists to say. The `crop` entry records the
    window the exported frame was taken from — the published TIFF beside
    the export still holds the full frame, and the record says which part
    of it this file is."""
    repaired = None
    if spots_params is not None and spots_params.get("repair"):
        repaired = {
            "detector_version": spots_params.get("detector_version"),
            "sensitivity": spots_params.get("sensitivity"),
            "repaired": sum(
                1 for spot in spots_params.get("spots") or [] if not spot.get("rejected")
            ),
        }
    cropped = None
    if crop_params is not None:
        cropped = {
            "x": crop_params.get("x"),
            "y": crop_params.get("y"),
            "width": crop_params.get("w"),
            "height": crop_params.get("h"),
            "tilt_deg": crop_params.get("tilt_deg"),
            "preset": crop_params.get("preset"),
        }
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
            "color": None if color_params is None else dict(color_params),
            "clip_fractions": list(clipped_fractions),
            "spots": repaired,
            "crop": cropped,
            "downsample": (
                None
                if applied_downsample is None
                else {"long_edge": applied_downsample}
            ),
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
    downsample: int | None = None,
    emit: EmitFn,
) -> ExportOutcome:
    """Exports the roll's negatives (all of them, or the requested ids)
    as rendered positives in JPEG XL. `downsample` is the long edge to
    reduce each export to (`None` keeps full resolution; an image already
    smaller than the target is skipped silently). Raises
    `ExportFailure` when the roll itself can't be read — including a
    colour roll predating the `camera_color` block (raised once
    before anything is written); one negative's problem is a warning plus
    a `failed` entry, and never stops the rest."""
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

    # A **colour** roll with no recorded `camera_color` predates the
    # colour-managed export and must be re-converted (or deleted). A
    # **mono** roll does not need the matrix and must not be failed for its
    # absence, so the check is conditional on the published TIFF's channel
    # count — the same fact the render gates on. Peeking the first
    # completed negative's header keeps the failure "raised once, before
    # anything is written".
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
        result = _export_negative(
            roll_dir, output_dir, roll, negative, downsample, emit
        )
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
    downsample: int | None,
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
        state = repo.net_edit_state(roll_dir, negative.negative_id)
        quarter_turns, flipped, fine_angle, tone_params, color_params, spots_params = (
            state.quarter_turns,
            state.flipped,
            state.fine_angle_deg,
            state.tone,
            state.color,
            state.spots,
        )
        meter = color.read_metering(negative.normalization)
        # The crop and spot repair apply before any other geometry: both
        # ops' coordinates are TIFF space. A stale
        # crop — a re-stitch changed the canvas — applies as nothing, the
        # same degrade `apply_crop` performs for the previews.
        crop_params = (
            state.crop
            if previews.crop_is_live(
                state.crop, (image.shape[0], image.shape[1])
            )
            else None
        )
        image = spots.apply_repair(image, spots_params)
        rotated = apply_edits(
            image, quarter_turns, flipped, fine_angle, crop_params
        )
        # The matrix follows the channel count — `None` for a mono roll's
        # 2-D published TIFF, the recorded camera matrix for a colour one.
        matrix = None if rotated.ndim == 2 else camera_matrix_for(roll)
        # The downsample decision is made on the rotated image's shape —
        # the shape the render receives — but the resize itself happens
        # inside `render_export`, on the linear values between the gamut
        # clip and the display re-encode (resample's module docstring).
        applied = applied_downsample(rotated, downsample)
        rendered, clipped_fractions = render.render_export(
            rotated, matrix, tone_params, color_params, meter, long_edge=downsample
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
            color_params,
            clipped_fractions,
            spots_params,
            crop_params,
            applied,
        )
    except jxl_writer.JxlEncoderUnavailable as exc:
        # A packaging failure, not a user error: stop the export
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
    color_params: dict[str, float] | None,
    clipped_fractions: tuple[float, ...],
    spots_params: dict | None = None,
    crop_params: dict | None = None,
    applied_downsample: int | None = None,
) -> None:
    """The single write: rendered pixels, the export ICC profile embedded,
    and the metadata boxes built at encode time. The `.tmp`-and-replace
    lives in `write_jxl`, so there is no second pass and no nested
    tmp dance.

    The `has_any` guard: a field nobody set writes nothing — with no
    metadata at all there is no Exif box. The XMP always goes,
    because the provenance record is not user-set metadata but the file's
    interpretability record."""
    metadata = export_metadata_for(roll, negative)
    exif = (
        build_exif(metadata, export_image_description(negative))
        if metadata.has_any
        else None
    )
    provenance = provenance_record(
        negative,
        matrix,
        tone_params,
        color_params,
        profile_kind,
        clipped_fractions,
        spots_params,
        crop_params,
        applied_downsample,
    )
    jxl_writer.write_jxl(
        destination,
        rendered,
        icc_profile=load_icc_profile(profile_kind),
        exif=exif,
        xmp=build_xmp(metadata, provenance),
    )
