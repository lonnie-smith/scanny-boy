"""Writes the roll's and negative's metadata into an exported TIFF.

The extended-metadata editing feature's one rule about TIFFs: metadata
lives in the database and reaches a TIFF only at export. This module is
that moment. The exporter's base write (plain `tifffile.imwrite`) carries
the pixels, the ICC profile, and the `ImageDescription` JSON; this module's
second pass adds everything a photo manager reads:

- `DateTimeOriginal`/`SubSecTimeOriginal` (nested EXIF IFD) from the
  negative's *intended* capture time — the rank formula's answer, so roll
  order survives into any tool that sorts by capture time.
- `Model` (IFD0) from the roll/negative `camera` field. The field is one
  free-text string, so it lands on `Model` alone; `Make` stays unset
  rather than guessed at.
- `LensModel` (EXIF IFD 42036) from the `lens` field.
- An XMP packet (tag 700) carrying `photoshop:City`, `photoshop:State`,
  and `dc:description` (the caption) — the fields that live in the
  commercial tool catalogs, not in EXIF.

Effective values follow the live-fallback rule: the negative's explicit
value, else the roll's. A field nobody set writes nothing at all — no
empty placeholders in the output file. The pass writes to a `.tmp` sibling
and verifies before replacing, the same discipline
`apply_metadata.rewrite_date_time_original` and `finalize_tiff` follow.
"""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import tifftools
from tifftools.constants import Tag

from scanny_boy.roll_manifest import (
    NegativeRecord,
    RollManifest,
    effective_metadata,
)

# IFD0 tag codes not already named in tiff_exif (which curates the nested
# EXIF IFD for the stitch pipeline's own two-pass write).
MODEL = 272
XMP_DESCRIPTION = 700

DATE_TIME_ORIGINAL = 36867
SUBSEC_TIME_ORIGINAL = 37521
LENS_MODEL = 42036


@dataclasses.dataclass(frozen=True)
class ExportMetadata:
    """The metadata one exported TIFF should carry, already resolved to
    effective values. `None` fields are omitted from the file entirely."""

    camera: str | None = None
    lens: str | None = None
    city: str | None = None
    state: str | None = None
    caption: str | None = None
    # The intended capture time, parsed from the manifest's ISO string.
    date_time_original: datetime.datetime | None = None

    @property
    def has_any(self) -> bool:
        return any(
            getattr(self, field.name) is not None
            for field in dataclasses.fields(self)
        )


def export_metadata_for(manifest: RollManifest, negative: NegativeRecord) -> ExportMetadata:
    """Resolves the roll/negative live fallback into one export record."""
    effective = effective_metadata(manifest.metadata, negative.metadata)
    intended_text = negative.capture_time.intended_datetime_original
    try:
        date_time_original = (
            datetime.datetime.fromisoformat(intended_text) if intended_text else None
        )
    except ValueError:
        date_time_original = None
    return ExportMetadata(
        camera=effective.get("camera"),
        lens=effective.get("lens"),
        city=effective.get("city"),
        state=effective.get("state"),
        caption=effective.get("caption"),
        date_time_original=date_time_original,
    )


def _xmp_packet(metadata: ExportMetadata) -> str:
    """A minimal XMP packet for the fields XMP (not EXIF) is the home of.
    Only the set fields are written; `dc:description` is an `Alt` container
    with the `x-default` language item, as photo managers expect."""
    description_items = ""
    if metadata.caption is not None:
        description_items = (
            "<dc:description><rdf:Alt>"
            f'<rdf:li xml:lang="x-default">{escape(metadata.caption)}</rdf:li>'
            "</rdf:Alt></dc:description>"
        )
    city = (
        f"<photoshop:City>{escape(metadata.city)}</photoshop:City>"
        if metadata.city is not None
        else ""
    )
    state = (
        f"<photoshop:State>{escape(metadata.state)}</photoshop:State>"
        if metadata.state is not None
        else ""
    )
    return (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/">'
        f"{description_items}{city}{state}"
        "</rdf:Description>"
        "</rdf:RDF>"
        "</x:xmpmeta>"
        '<?xpacket end="w"?>'
    )


def _exif_ifd_tags(metadata: ExportMetadata) -> dict[int, dict]:
    tags: dict[int, dict] = {}
    if metadata.date_time_original is not None:
        tags[DATE_TIME_ORIGINAL] = {
            "data": metadata.date_time_original.strftime("%Y:%m:%d %H:%M:%S"),
            "datatype": tifftools.Datatype.ASCII,
        }
        if metadata.date_time_original.microsecond:
            digits = f"{metadata.date_time_original.microsecond:06d}".rstrip("0")
            tags[SUBSEC_TIME_ORIGINAL] = {
                "data": digits,
                "datatype": tifftools.Datatype.ASCII,
            }
    if metadata.lens is not None:
        tags[LENS_MODEL] = {
            "data": metadata.lens,
            "datatype": tifftools.Datatype.ASCII,
        }
    return tags


def write_export_metadata(path: Path, metadata: ExportMetadata) -> None:
    """Adds the metadata tags to an already-written export TIFF. The file is
    rewritten through a `.tmp` sibling and only replaces the destination
    after the `DateTimeOriginal` round-trips (when one was written), so a
    failed write can never leave the export half-tagged or truncated."""
    info = tifftools.read_tiff(str(path))
    ifd0 = info["ifds"][0]
    if metadata.camera is not None:
        ifd0["tags"][MODEL] = {
            "data": metadata.camera,
            "datatype": tifftools.Datatype.ASCII,
        }
    exif_tags = _exif_ifd_tags(metadata)
    if exif_tags:
        exif_ifd = {"tags": exif_tags, "ifds": []}
        ifd0["tags"][Tag.ExifIFD.value] = {
            "ifds": [[exif_ifd]],
            "datatype": tifftools.Datatype.LONG,
        }
    if metadata.city is not None or metadata.state is not None or metadata.caption is not None:
        ifd0["tags"][XMP_DESCRIPTION] = {
            # XMP in TIFF is a BYTE-array tag; tifftools packs `data` as a
            # list of ints for that datatype.
            "data": list(_xmp_packet(metadata).encode("utf-8")),
            "datatype": tifftools.Datatype.BYTE,
        }

    tmp_path = path.with_suffix(path.suffix + ".meta.tmp")
    try:
        tifftools.write_tiff(info, str(tmp_path))
        _verify(tmp_path, metadata)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _verify(path: Path, metadata: ExportMetadata) -> None:
    """The rewrite's sanity check: the `DateTimeOriginal` just written must
    read back identically (the one tag whose correctness is easy to get
    silently wrong), and the XMP packet — when one belongs in this file —
    must be present."""
    info = tifftools.read_tiff(str(path))
    ifd0 = info["ifds"][0]
    xmp_entry = ifd0["tags"].get(XMP_DESCRIPTION)
    if xmp_entry is not None:
        xmp_data = xmp_entry["data"]
        if isinstance(xmp_data, list):
            xmp_data = bytes(xmp_data).decode("utf-8")
        if "xmpmeta" not in xmp_data:
            raise ValueError("XMP packet did not survive the rewrite")
    elif metadata.city is not None or metadata.state is not None or metadata.caption is not None:
        raise ValueError("XMP packet did not survive the rewrite")
    if metadata.date_time_original is not None:
        exif_ifd = ifd0["tags"][Tag.ExifIFD.value]["ifds"][0][0]
        actual = exif_ifd["tags"][DATE_TIME_ORIGINAL]["data"]
        expected = metadata.date_time_original.strftime("%Y:%m:%d %H:%M:%S")
        if actual != expected:
            raise ValueError(
                f"DateTimeOriginal did not round-trip: wrote {expected!r}, "
                f"read {actual!r}"
            )
