"""Builds the metadata an exported file carries, as box payloads.

The extended-metadata editing feature's one rule about deliverables:
metadata lives in the database and reaches a file only at export. This
module is that moment (docs/EXPORT_PLAN.md §5.2).

Since the export became a JPEG XL write, there is no second pass: JPEG XL
takes metadata as boxes at encode time, so nothing is reopened or
rewritten. The exporters' base write carries the pixels, the ICC profile
and the boxes; this module *builds* the two box payloads:

- `build_exif` — a little-endian TIFF stream: IFD0 with
  `ImageDescription` (270) and `Model` (272), plus the nested EXIF IFD
  with `DateTimeOriginal` (36867), `SubSecTimeOriginal` (37521) and
  `LensModel` (42036). Built with `tifftools` over an in-memory 1x1
  placeholder image — readers take the tags from IFD0 and ignore the
  strip — because hand-rolling nested-IFD offsets is the fiddliest work
  in the export plan and there is no reason to do it.
- `build_xmp` — the XMP packet: `dc:description` (the caption),
  `photoshop:City`, `photoshop:State`, and the `scannyboy:provenance`
  record (the file's interpretability record — what the published TIFF's
  `ImageDescription` JSON used to carry, plus what the render actually
  did; §5.2).

Effective values follow the live-fallback rule: the negative's explicit
value, else the roll's. A field nobody set writes nothing at all — no
empty placeholders in the output file, and no Exif box when nothing is
set.
"""

from __future__ import annotations

import dataclasses
import datetime
import io
import json
from xml.sax.saxutils import escape

import numpy as np
import tifffile
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

# The provenance record's namespace (docs/EXPORT_PLAN.md §5.2): a JSON
# string in a `scannyboy:provenance` property — what makes an exported
# file interpretable without the database.
SCANNY_BOY_NS = "http://scannyboy.local/ns/1.0/"


@dataclasses.dataclass(frozen=True)
class ExportMetadata:
    """The metadata one exported file should carry, already resolved to
    effective values. `None` fields are omitted from the file entirely."""

    camera: str | None = None
    lens: str | None = None
    city: str | None = None
    state: str | None = None
    caption: str | None = None
    film: str | None = None
    iso: str | None = None
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
        film=effective.get("film"),
        iso=effective.get("iso"),
        date_time_original=date_time_original,
    )


def _xmp_packet(metadata: ExportMetadata, provenance: dict) -> str:
    """The XMP packet: the fields XMP (not EXIF) is the home of, plus the
    `scannyboy:provenance` record. The provenance is always present — it
    is what makes the file interpretable without the database — while the
    user-set fields appear only when set. `dc:description` is an `Alt`
    container with the `x-default` language item, as photo managers
    expect."""
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
    provenance_items = (
        f"<scannyboy:provenance>{escape(json.dumps(provenance, sort_keys=True))}"
        "</scannyboy:provenance>"
    )
    return (
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/" '
        f'xmlns:scannyboy="{SCANNY_BOY_NS}">'
        f"{description_items}{city}{state}{provenance_items}"
        "</rdf:Description>"
        "</rdf:RDF>"
        "</x:xmpmeta>"
        '<?xpacket end="w"?>'
    )


def build_xmp(metadata: ExportMetadata, provenance: dict) -> bytes:
    """The `xml ` box's payload: the XMP packet, UTF-8 bytes."""
    return _xmp_packet(metadata, provenance).encode("utf-8")


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


def build_exif(metadata: ExportMetadata, image_description: str) -> bytes:
    """The `Exif` box's payload: a little-endian TIFF stream whose IFD0
    carries `ImageDescription` and `Model`, with the nested EXIF IFD
    beside them. `tifftools` builds it over an in-memory 1x1 placeholder
    image — the tags live in IFD0, and readers of the box ignore the
    strip. Same tags, same effective-value fallback rules, and the same
    "a field nobody set writes nothing" behaviour as the TIFF-era second
    pass."""
    buffer = io.BytesIO()
    tifffile.imwrite(buffer, [[0]], dtype=np.uint8)
    buffer.seek(0)
    info = tifftools.read_tiff(buffer)
    ifd0 = info["ifds"][0]
    ifd0["tags"][270] = {
        "data": image_description,
        "datatype": tifftools.Datatype.ASCII,
    }
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
    out = io.BytesIO()
    tifftools.write_tiff(info, out)
    return out.getvalue()
