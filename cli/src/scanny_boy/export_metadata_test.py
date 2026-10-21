"""Tests for the export's metadata resolution and box payloads: roll-level
values reach every negative, a negative's override wins only when it is
defined, and `film`/`iso` land in the files. Fast tier — nothing here
decodes or stitches anything."""

from __future__ import annotations

import io

import pytest
import tifftools
from tifftools.constants import Tag

from scanny_boy.export_metadata import (
    ISO_SPEED_RATINGS,
    ExportMetadata,
    build_exif,
    build_xmp,
    export_metadata_for,
    parse_iso,
)
from scanny_boy.roll_manifest import (
    CaptureTime,
    NegativeMetadata,
    NegativeRecord,
    RollManifest,
    RollMetadata,
    effective_metadata,
    new_roll_manifest,
)

_ROLL_VALUES = {
    "camera": "Nikon F3",
    "lens": "50mm f/1.4",
    "city": "Porto",
    "state": "Oregon",
    "caption": "harbor morning",
    "film": "Kodak Tri-X 400",
    "iso": "400",
}


def _negative(number: int, **metadata: str | None) -> NegativeRecord:
    return NegativeRecord(
        negative_id=f"run-negative-{number:02d}",
        run_id="run",
        members=[f"_DSC{number:04d}.NEF"],
        expected_output=f"_DSC{number:04d}.tif",
        fill_color=(0, 0, 0),
        capture_time=CaptureTime(
            intended_datetime_original=f"2026-08-01T12:00:{number:02d}"
        ),
        metadata=NegativeMetadata(**metadata),
    )


def _roll(*negatives: NegativeRecord, **roll_values: str | None) -> RollManifest:
    manifest = new_roll_manifest(roll_id="roll-1", roll_name="Roll")
    manifest.metadata = RollMetadata(**{**_ROLL_VALUES, **roll_values})
    manifest.negatives = list(negatives)
    return manifest


def _all_fields(metadata: ExportMetadata) -> dict[str, str | None]:
    return {name: getattr(metadata, name) for name in _ROLL_VALUES}


def _ifd0(metadata: ExportMetadata) -> dict:
    info = tifftools.read_tiff(io.BytesIO(build_exif(metadata, "description")))
    return info["ifds"][0]["tags"]


def _exif_ifd_tags(metadata: ExportMetadata) -> dict:
    exif = _ifd0(metadata).get(Tag.ExifIFD.value)
    return {} if exif is None else exif["ifds"][0][0]["tags"]


# --- (a) roll values reach every negative -----------------------------------


def test_every_negative_carries_all_the_roll_values_when_none_override():
    roll = _roll(_negative(1), _negative(2), _negative(3))
    for negative in roll.negatives:
        assert _all_fields(export_metadata_for(roll, negative)) == _ROLL_VALUES


def test_a_roll_value_set_later_covers_every_negative_without_a_write():
    roll = _roll(_negative(1), _negative(2))
    roll.metadata.city = "Lisbon"
    for negative in roll.negatives:
        assert export_metadata_for(roll, negative).city == "Lisbon"


# --- (b) overrides win field by field ---------------------------------------


def test_a_negatives_overrides_win_and_the_rest_still_comes_from_the_roll():
    overriding = _negative(
        1, lens="35mm f/2", city="Lisbon", state="Lisboa", caption="tram 28"
    )
    sibling = _negative(2)
    roll = _roll(overriding, sibling)

    first = export_metadata_for(roll, overriding)
    assert (first.lens, first.city, first.state, first.caption) == (
        "35mm f/2",
        "Lisbon",
        "Lisboa",
        "tram 28",
    )
    assert (first.camera, first.film, first.iso) == (
        "Nikon F3",
        "Kodak Tri-X 400",
        "400",
    )

    # The sibling is untouched by its neighbour's overrides.
    assert _all_fields(export_metadata_for(roll, sibling)) == _ROLL_VALUES


def test_a_partial_override_leaves_the_other_fields_on_the_roll():
    negative = _negative(1, caption="only the caption")
    roll = _roll(negative)
    resolved = export_metadata_for(roll, negative)
    assert resolved.caption == "only the caption"
    assert resolved.lens == "50mm f/1.4"
    assert resolved.city == "Porto"
    assert resolved.state == "Oregon"


# --- (c) an override that is not defined falls back -------------------------


@pytest.mark.parametrize("blank", [None, "", "   ", "\t\n"])
def test_a_blank_override_falls_back_to_the_roll(blank):
    negative = _negative(1, lens=blank, city=blank, state=blank, caption=blank)
    roll = _roll(negative)
    assert _all_fields(export_metadata_for(roll, negative)) == _ROLL_VALUES


def test_an_override_is_stripped():
    negative = _negative(1, city="  Lisbon  ")
    assert export_metadata_for(_roll(negative), negative).city == "Lisbon"


def test_a_blank_roll_value_is_unset_not_an_empty_string():
    negative = _negative(1)
    roll = _roll(negative, city="  ", film="", iso=None)
    resolved = export_metadata_for(roll, negative)
    assert (resolved.city, resolved.film, resolved.iso) == (None, None, None)


# --- (d) camera is roll-only ------------------------------------------------


def test_camera_always_comes_from_the_roll():
    roll = _roll(_negative(1), _negative(2), camera="Leica M6")
    assert [export_metadata_for(roll, n).camera for n in roll.negatives] == [
        "Leica M6",
        "Leica M6",
    ]


def test_a_negative_has_no_camera_to_override_with():
    assert not hasattr(NegativeMetadata(), "camera")
    with pytest.raises(TypeError):
        NegativeMetadata(camera="Leica M6")  # type: ignore[call-arg]


def test_effective_metadata_has_every_key_with_roll_only_fields_from_the_roll():
    effective = effective_metadata(RollMetadata(**_ROLL_VALUES), NegativeMetadata())
    assert effective == _ROLL_VALUES
    unset = effective_metadata(RollMetadata(), NegativeMetadata())
    assert set(unset) == set(_ROLL_VALUES)
    assert all(value is None for value in unset.values())


# --- (e) film and iso reach the file ----------------------------------------


def test_film_and_iso_land_in_the_xmp_in_the_scannyboy_namespace():
    xmp = build_xmp(ExportMetadata(film="Kodak Tri-X 400", iso="400"), {}).decode()
    assert "<scannyboy:film>Kodak Tri-X 400</scannyboy:film>" in xmp
    assert "<scannyboy:iso>400</scannyboy:iso>" in xmp
    assert 'xmlns:scannyboy="http://scannyboy.local/ns/1.0/"' in xmp


def test_film_and_iso_are_xml_escaped():
    xmp = build_xmp(ExportMetadata(film="Tri-X <400> & co", iso="ISO 400"), {})
    assert b"<scannyboy:film>Tri-X &lt;400&gt; &amp; co</scannyboy:film>" in xmp


def test_unset_film_and_iso_write_no_xmp_properties():
    xmp = build_xmp(ExportMetadata(city="Porto"), {}).decode()
    assert "scannyboy:film>" not in xmp
    assert "scannyboy:iso>" not in xmp


def test_iso_lands_in_the_exif_ifd_as_a_short():
    tags = _exif_ifd_tags(ExportMetadata(iso="400"))
    assert tags[ISO_SPEED_RATINGS]["datatype"] == tifftools.Datatype.SHORT
    assert tags[ISO_SPEED_RATINGS]["data"] == [400]


def test_iso_with_a_prefix_is_still_written_to_the_exif():
    assert _exif_ifd_tags(ExportMetadata(iso=" ISO 3200 "))[ISO_SPEED_RATINGS][
        "data"
    ] == [3200]


@pytest.mark.parametrize("text", ["fast", "400/27", "pushed to 1600", "0", "70000", ""])
def test_an_unparseable_iso_writes_no_exif_tag_but_still_the_xmp(text):
    metadata = ExportMetadata(iso=text or None, city="Porto")
    assert ISO_SPEED_RATINGS not in _exif_ifd_tags(metadata)
    if text:
        assert (
            f"<scannyboy:iso>{text}</scannyboy:iso>" in build_xmp(metadata, {}).decode()
        )


def test_parse_iso_bounds():
    assert parse_iso("1") == 1
    assert parse_iso("65535") == 65535
    assert parse_iso("65536") is None
    assert parse_iso(None) is None


def test_the_full_resolved_record_round_trips_through_both_payloads():
    negative = _negative(1, lens="35mm f/2")
    metadata = export_metadata_for(_roll(negative), negative)
    ifd0 = _ifd0(metadata)
    assert ifd0[272]["data"] == "Nikon F3"
    exif = _exif_ifd_tags(metadata)
    assert exif[42036]["data"] == "35mm f/2"
    assert exif[ISO_SPEED_RATINGS]["data"] == [400]
    xmp = build_xmp(metadata, {}).decode()
    assert "photoshop:City>Porto<" in xmp
    assert "photoshop:State>Oregon<" in xmp
    assert "harbor morning" in xmp
    assert "scannyboy:film>Kodak Tri-X 400<" in xmp
