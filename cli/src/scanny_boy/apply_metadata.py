"""`apply-metadata --roll DIR`: writes each dirty negative's intended
capture time into its published TIFF. See
`docs/PHASE3_IMPLEMENTATION_PLAN.md` section 3.8.

**Intent lives in the manifest; the TIFF is the artefact.** A negative is
*dirty* when `capture_time.intended_datetime_original` differs from
`capture_time.applied_datetime_original`; this module is the only thing
that ever writes the latter. No pixel data is read, decoded, or rewritten —
`_rewrite_date_time_original` reuses `tifftools`, the same numeric-tag
library `tiff_exif.py` uses to build a TIFF's nested EXIF directory in the
first place, so only that directory's `DateTimeOriginal`/`SubSecTimeOriginal`
entries are ever touched.
"""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path
from typing import Any

import tifftools
from tifftools.constants import Tag

from scanny_boy import hashing
from scanny_boy.events import Code, MetadataApplied, MetadataSkipped
from scanny_boy.manifest import BadManifestError
from scanny_boy.roll_manifest import (
    ROLL_MANIFEST_FILENAME,
    NegativeRecord,
    RollManifestUnsupportedError,
    current_roll_manifest_path,
    load_roll_manifest,
    write_roll_manifest,
)
from scanny_boy.tiff_exif import (
    DATE_TIME_ORIGINAL,
    SUBSEC_TIME_ORIGINAL,
    format_date_time,
    format_subsec,
)

EmitFn = Any


class ApplyMetadataFailure(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class ApplyMetadataOutcome:
    applied: list[str]
    skipped: list[str]


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat() + "Z"


def _is_dirty(negative: NegativeRecord) -> bool:
    """Section 3.8: dirty, `completed`, never superseded, and actually
    published — exactly the negatives `apply-metadata` processes."""
    return (
        negative.status == "completed"
        and negative.superseded_by is None
        and negative.output is not None
        and negative.capture_time.intended_datetime_original
        != negative.capture_time.applied_datetime_original
    )


def _verify_rewrite(tmp_path: Path, intended: datetime.datetime) -> None:
    """"Verify it reads back with the expected tags" (section 3.8):
    both `DateTimeOriginal` and `SubSecTimeOriginal`, the only two tags
    `_rewrite_date_time_original` touched."""
    info = tifftools.read_tiff(str(tmp_path))
    exif_tags = info["ifds"][0]["tags"][Tag.ExifIFD.value]["ifds"][0][0]["tags"]

    expected_date = format_date_time(intended)
    actual_date = exif_tags.get(DATE_TIME_ORIGINAL, {}).get("data")
    if actual_date != expected_date:
        raise ValueError(
            f"DateTimeOriginal did not round-trip: wrote {expected_date!r}, "
            f"read {actual_date!r}"
        )

    expected_subsec = format_subsec(intended)
    actual_subsec = exif_tags.get(SUBSEC_TIME_ORIGINAL, {}).get("data")
    if actual_subsec != expected_subsec:
        raise ValueError(
            f"SubSecTimeOriginal did not round-trip: wrote {expected_subsec!r}, "
            f"read {actual_subsec!r}"
        )


def _rewrite_date_time_original(tiff_path: Path, intended: datetime.datetime) -> None:
    """Section 3.8 point 2: rewrite `DateTimeOriginal`/`SubSecTimeOriginal`
    in `tiff_path`'s nested EXIF directory. Writes a sibling temp file,
    verifies it, then renames over `tiff_path` — `tiff_path` is untouched
    until the rename. Raises `ApplyMetadataFailure(METADATA_WRITE_FAILED)`
    on any problem, leaving no temp file behind."""
    try:
        info = tifftools.read_tiff(str(tiff_path))
        exif_tags = info["ifds"][0]["tags"][Tag.ExifIFD.value]["ifds"][0][0]["tags"]

        exif_tags[DATE_TIME_ORIGINAL] = {
            "data": format_date_time(intended),
            "datatype": tifftools.Datatype.ASCII,
        }
        subsec = format_subsec(intended)
        if subsec is not None:
            exif_tags[SUBSEC_TIME_ORIGINAL] = {
                "data": subsec,
                "datatype": tifftools.Datatype.ASCII,
            }
        elif SUBSEC_TIME_ORIGINAL in exif_tags:
            del exif_tags[SUBSEC_TIME_ORIGINAL]

        tmp_path = tiff_path.with_suffix(tiff_path.suffix + ".tmp")
        tifftools.write_tiff(info, str(tmp_path))
        try:
            _verify_rewrite(tmp_path, intended)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        tmp_path.replace(tiff_path)
    except Exception as exc:
        raise ApplyMetadataFailure(
            Code.METADATA_WRITE_FAILED,
            f"could not rewrite {tiff_path}: {exc}",
        ) from exc


def run_apply_metadata(roll_dir: Path, *, emit: EmitFn) -> ApplyMetadataOutcome:
    """Section 3.8, in full. Raises `ApplyMetadataFailure` when the roll
    itself can't be read; a single negative's problem is reported through
    `MetadataSkipped` and never stops the rest (section 3.8: "never fail
    the whole roll for one")."""
    if not current_roll_manifest_path(roll_dir).exists():
        raise ApplyMetadataFailure(
            Code.ROLL_NOT_FOUND,
            f"{roll_dir} has no {ROLL_MANIFEST_FILENAME}; create the roll first",
        )
    try:
        roll = load_roll_manifest(roll_dir)
    except (BadManifestError, RollManifestUnsupportedError) as exc:
        raise ApplyMetadataFailure(exc.code, exc.message) from exc

    dirty = [n for n in roll.negatives if _is_dirty(n)]
    if not dirty:
        return ApplyMetadataOutcome(applied=[], skipped=[])

    applied: list[str] = []
    skipped: list[str] = []

    for negative in dirty:
        assert negative.output is not None
        tiff_path = roll_dir / negative.output["name"]

        actual_size = tiff_path.stat().st_size if tiff_path.exists() else None
        actual_sha256 = hashing.sha256_file(tiff_path) if tiff_path.exists() else None
        if actual_size != negative.output["size"] or actual_sha256 != negative.output["sha256"]:
            message = f"{tiff_path} no longer matches the roll's recorded size and hash"
            emit(
                MetadataSkipped(
                    negative_id=negative.negative_id,
                    code=Code.OUTPUT_MODIFIED_EXTERNALLY,
                    message=message,
                )
            )
            skipped.append(negative.negative_id)
            continue

        intended = datetime.datetime.fromisoformat(
            negative.capture_time.intended_datetime_original
        )
        try:
            _rewrite_date_time_original(tiff_path, intended)
        except ApplyMetadataFailure as exc:
            emit(
                MetadataSkipped(
                    negative_id=negative.negative_id, code=exc.code, message=exc.message
                )
            )
            skipped.append(negative.negative_id)
            continue

        negative.output["size"] = tiff_path.stat().st_size
        negative.output["sha256"] = hashing.sha256_file(tiff_path)
        negative.capture_time.applied_datetime_original = (
            negative.capture_time.intended_datetime_original
        )
        applied.append(negative.negative_id)
        emit(MetadataApplied(negative_id=negative.negative_id))

    roll.metadata.last_applied_at = _now_iso()
    write_roll_manifest(roll_dir, roll)

    return ApplyMetadataOutcome(applied=applied, skipped=skipped)
