#!/usr/bin/env python3
"""Deterministic generator for the bundled Scanny Boy ICC profiles.

The colorants are carried over byte-identical from the vendored
ProPhoto-v4.icc source (white point and chromatic-adaptation tag with
them) and are a **deliberately wide container**, not a measurement:
`raw_decode.RAW_PARAMS` decodes the camera's own filter responses
(`output_color=raw`, `user_wb=[1, 1, 1, 1]`), so the pixels have never
been converted into any colorimetric space and no profile primaries could
be true of them. A wide container keeps a viewer applying the profile
from clipping camera-native values; ICC has no "unknown primaries"
encoding, so the wide container is the honest choice (docs/
PROFILE_HONESTY_PLAN.md). Three profiles:

- `ScannyBoy-Linear-v1.icc`, g = 1.0 — the identity, declaring that the
  prepare stage's intermediate pixels are linear (`raw_decode.RAW_PARAMS`
  decodes linear).
- `ScannyBoy-Density-v1.icc`, g = 2.2 — a **viewing convention** for the
  published, normalized log-density TIFF, not a colorimetric claim. A
  normalized log encoding over ~2 decades is closer to gamma 3.3 than
  2.2, and no ICC parametric type expresses it exactly anyway; but a
  *correct* profile would decode the file back to un-normalized linear —
  it would show the orange-masked raw scan, undoing the one thing the
  normalization stage does. The tag exists to make the file legible in an
  external viewer while debugging the edit stage. Every internal consumer
  decodes through `normalization.decode_normalized`, never through an ICC
  transform.
- `ScannyBoy-Density-Grey-v1.icc`, the grey-class companion of the
  density profile (MONOCHROME_PLAN section 4).

A second construction path builds the **export** profiles with lcms2
(docs/EXPORT_PLAN.md section 2): the carry-over trick cannot produce
Adobe RGB, whose primaries *and* white point differ from the ProPhoto
source's, and hand-assembling the bytes instead is what produced the
mislabelled `chad`, missing `cprt` and undisplayable `desc` that shipped
in v1. lcms2 is handed the published chromaticities and writes the
profile; only the description, the copyright and the grey profile's
D50 white point and `chad` are set afterwards. Two more profiles:

- `ScannyBoy-Export-AdobeRGB-v1.icc` — the export's RGB space, whose
  colorimetry is the published Adobe RGB (1998) specification and *is* a
  claim: the export is a rendered positive, and its pixels really are in
  this space (EXPORT_PLAN section 4's matrix conversion).
- `ScannyBoy-Export-Grey-v1.icc` — the grey-class companion, same TRC,
  for a mono roll's export.
"""

from __future__ import annotations

import base64
import hashlib
import struct
import sys
from pathlib import Path

from imagecodecs import cms_profile

# Committed ProPhoto-v4.icc bytes, vendored so the generator keeps working
# after that file is deleted.
_PROPHOTO_V4_ICC_B64 = "AAAB4GxjbXMEIAAAbW50clJHQiBYWVogB+IAAwAUAAkADgAdYWNzcE1TRlQAAAAAc2F3c2N0cmwAAAAAAAAAAAAAAAAAAPbWAAEAAAAA0y1oYW5kH9ZD3wQwsLzdCGIbXzs4jAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKZGVzYwAAAPwAAAAkY3BydAAAASAAAAAid3RwdAAAAUQAAAAUY2hhZAAAAVgAAAAsclhZWgAAAYQAAAAUZ1hZWgAAAZgAAAAUYlhZWgAAAawAAAAUclRSQwAAAcAAAAAgZ1RSQwAAAcAAAAAgYlRSQwAAAcAAAAAgbWx1YwAAAAAAAAABAAAADGVuVVMAAAAIAAAAHABSAE8ATQBNbWx1YwAAAAAAAAABAAAADGVuVVMAAAAGAAAAHABDAEMAMAAAWFlaIAAAAAAAAPbWAAEAAAAA0y1zZjMyAAAAAAAA//3////+/////v////0AAQAD//////////8AAAABAAD/71hZWiAAAAAAAADMNwAASb4AAAAAWFlaIAAAAAAAACKaAAC2PQAAAAFYWVogAAAAAAAACAUAAAAFAADTLHBhcmEAAAAAAAMAAAABzM0AAQAAAAAAAAAAEAAAAAgA"

LINEAR_DESCRIPTION = (
    "Scanny Boy Linear RGB. The colorants are a deliberately wide "
    "container, chosen so a viewer applying this profile does not clip "
    "camera-native values, and not a measurement of this camera's "
    "primaries: raw_decode.RAW_PARAMS decodes raw camera channels, never "
    "converted into any colorimetric space. The linear TRC is the truth "
    "about the pixels."
)

DENSITY_DESCRIPTION = (
    "ScannyBoy Normalized Density (viewing gamma 2.2). The colorants are "
    "a deliberately wide container, chosen so a viewer applying this "
    "profile does not clip camera-native values, and not a measurement "
    "of this camera's primaries: raw_decode.RAW_PARAMS decodes raw "
    "camera channels, never converted into any colorimetric space. A "
    "viewing convention for normalized log-density working files, not a "
    "colorimetric claim: the pixels are per-channel affine-stretched "
    "log10 density, which no ICC TRC expresses exactly. Decode through "
    "scanny_boy.normalization.decode_normalized, never through this "
    "profile - it exists so external viewers show approximately the code "
    "values."
)

DENSITY_GREY_DESCRIPTION = (
    "ScannyBoy Normalized Density Grey (viewing gamma 2.2). The "
    "single-channel companion of the Density profile: a mono roll's "
    "published TIFF is one grayscale channel of the same normalized log "
    "density, which an RGB profile cannot tag - this profile carries no "
    "colorants at all, while its RGB siblings carry a deliberately wide "
    "container, not a measurement of this camera's primaries "
    "(raw_decode.RAW_PARAMS decodes raw camera channels). Same viewing "
    "convention, same caveat - decode through "
    "scanny_boy.normalization.decode_normalized, never through this "
    "profile (MONOCHROME_PLAN section 4)."
)

# ICC parametricCurveType function type 0: pure gamma. g = 1.0 in s15Fixed16
# — the identity curve, because the decode is linear. The density profile's
# g = 2.2 is `round(2.2 * 65536) = 144179`, a viewing convention, not a
# colorimetric claim (see the module docstring).
TRC_FUNCTION_TYPE = 0
TRC_PARAMS = (65536,)
DENSITY_TRC_PARAMS = (144179,)

TRC_SIGNATURES = (b"rTRC", b"gTRC", b"bTRC")


def _pad4(n: int) -> int:
    return (n + 3) & ~3


def _s15fixed16(value: int) -> bytes:
    return struct.pack(">i", value)


def _build_desc_tag(text: str) -> bytes:
    # No NUL terminator: ICC.1:2010 §10.13 `multiLocalizedUnicodeType`
    # counts only the characters, and a counted trailing U+0000 is a
    # spec violation (it was here until the lcms2 switch).
    encoded = text.encode("utf-16-be")
    record_size = 12
    header_size = 16
    string_offset = header_size + record_size
    tag = bytearray()
    tag.extend(b"mluc")
    tag.extend(b"\x00\x00\x00\x00")
    tag.extend(struct.pack(">I", 1))  # num records
    tag.extend(struct.pack(">I", record_size))
    tag.extend(b"en")
    tag.extend(b"US")
    tag.extend(struct.pack(">I", len(encoded)))
    tag.extend(struct.pack(">I", string_offset))
    tag.extend(encoded)
    return bytes(tag)


def _build_trc_tag(trc_params: tuple[int, ...]) -> bytes:
    tag = bytearray()
    tag.extend(b"para")
    tag.extend(b"\x00\x00\x00\x00")
    tag.extend(struct.pack(">H", TRC_FUNCTION_TYPE))
    tag.extend(b"\x00\x00")
    for value in trc_params:
        tag.extend(_s15fixed16(value))
    return bytes(tag)


def _parse_tag_table(data: bytes) -> list[tuple[bytes, int, int]]:
    tag_count = struct.unpack(">I", data[128:132])[0]
    entries: list[tuple[bytes, int, int]] = []
    offset = 132
    for _ in range(tag_count):
        sig, tag_offset, size = struct.unpack(">4sII", data[offset : offset + 12])
        entries.append((sig, tag_offset, size))
        offset += 12
    return entries


def _profile_id(data: bytes) -> bytes:
    profile = bytearray(data)
    profile[44:48] = b"\x00\x00\x00\x00"
    profile[64:68] = b"\x00\x00\x00\x00"
    profile[84:100] = b"\x00" * 16
    return hashlib.md5(profile, usedforsecurity=False).digest()


def prophoto_source_bytes() -> bytes:
    """The upstream ProPhoto-v4.icc bytes this profile derives from."""
    return base64.b64decode(_PROPHOTO_V4_ICC_B64)


def generate_profile(
    description: str = LINEAR_DESCRIPTION,
    trc_params: tuple[int, ...] = TRC_PARAMS,
    *,
    trc_signatures: tuple[bytes, ...] = (),
    drop_tag_signatures: tuple[bytes, ...] = (),
    header_colorspace: bytes | None = None,
) -> bytes:
    """Rebuild the source profile with `description` and `trc_params`.

    The keyword arguments exist for the grey profile (MONOCHROME_PLAN
    section 4): `trc_signatures` replaces the source's rTRC/gTRC/bTRC
    registrations (a gray-class profile carries a single `kTRC`),
    `drop_tag_signatures` drops the RGB matrix tags, and
    `header_colorspace` rewrites the header's data colour space ('RGB '
    -> 'GRAY'). The defaults reproduce the two RGB profiles exactly."""
    source = base64.b64decode(_PROPHOTO_V4_ICC_B64)
    entries = _parse_tag_table(source)

    tag_payloads: list[tuple[bytes, bytes]] = []
    kept_trc_signatures: list[bytes] = []

    for sig, tag_offset, size in entries:
        if sig in TRC_SIGNATURES:
            if sig not in drop_tag_signatures:
                kept_trc_signatures.append(sig)
            continue
        if sig in drop_tag_signatures:
            continue
        if sig == b"desc":
            tag_payloads.append((sig, _build_desc_tag(description)))
        else:
            tag_payloads.append((sig, source[tag_offset : tag_offset + size]))

    trc_bytes = _build_trc_tag(trc_params)
    trc_signatures = list(trc_signatures) or kept_trc_signatures or list(TRC_SIGNATURES)

    tag_count = len(tag_payloads) + len(trc_signatures)
    table_size = 128 + 4 + tag_count * 12
    data_offset = _pad4(table_size)
    current = data_offset

    rebuilt_entries: list[tuple[bytes, int, int]] = []
    blob = bytearray()
    for sig, payload in tag_payloads:
        rebuilt_entries.append((sig, current, len(payload)))
        blob.extend(payload)
        padding = _pad4(len(payload)) - len(payload)
        blob.extend(b"\x00" * padding)
        current += len(payload) + padding

    trc_offset = current
    trc_size = len(trc_bytes)
    blob.extend(trc_bytes)
    padding = _pad4(trc_size) - trc_size
    blob.extend(b"\x00" * padding)
    current += trc_size + padding
    for sig in trc_signatures:
        rebuilt_entries.append((sig, trc_offset, trc_size))

    header = bytearray(source[:128])
    if header_colorspace is not None:
        header[16:20] = header_colorspace
    struct.pack_into(">I", header, 0, current)

    table = bytearray()
    table.extend(header)
    table.extend(struct.pack(">I", tag_count))
    for sig, tag_offset, size in rebuilt_entries:
        table.extend(sig)
        table.extend(struct.pack(">I", tag_offset))
        table.extend(struct.pack(">I", size))
    if len(table) < data_offset:
        table.extend(b"\x00" * (data_offset - len(table)))
    table.extend(blob)

    profile_id = _profile_id(bytes(table))
    struct.pack_into(">16s", table, 84, profile_id)
    return bytes(table)


def generate_linear_profile() -> bytes:
    return generate_profile(LINEAR_DESCRIPTION, TRC_PARAMS)


def generate_density_profile() -> bytes:
    return generate_profile(DENSITY_DESCRIPTION, DENSITY_TRC_PARAMS)


def generate_density_grey_profile() -> bytes:
    """The gray-class companion of the density profile (MONOCHROME_PLAN
    section 4): same gamma-2.2 viewing TRC, one kTRC instead of the RGB
    matrix + three TRCs, header data colour space GRAY."""
    return generate_profile(
        DENSITY_GREY_DESCRIPTION,
        DENSITY_TRC_PARAMS,
        trc_signatures=(b"kTRC",),
        drop_tag_signatures=(b"rXYZ", b"gXYZ", b"bXYZ"),
        header_colorspace=b"GRAY",
    )


# --- EXPORT_PLAN section 2: the export profiles ----------------------------
#
# These two are built by **lcms2** (`imagecodecs.cms_profile`, already a
# runtime dependency and already loaded into the process by
# `jxl_writer`), not by assembling ICC bytes here. The hand-assembled
# construction this replaced shipped three defects that a mature ICC
# writer does not make: `chad` carried the `XYZ ` type signature instead
# of `sf32`, the required `cprt` tag was absent entirely, and the `desc`
# string was long enough that macOS reported no profile name at all
# (ColorSync returns an empty description at >= 100 characters).
#
# lcms2 is given the *published Adobe RGB (1998) chromaticities* — which
# are the specification — and derives the D50-adapted colorants and the
# `chad` from them. That is the source of truth now, in place of the
# s15Fixed16 integers previously copied from Apple's file; the two agree
# exactly except for `bXYZ`'s Z, where lcms2 rounds 0.744568 down to
# 48795 rather than up to 48796 (a difference of 1/65536 = 0.000015,
# colorimetrically nil). See EXPORT_PLAN section 2.2.

ADOBE_RGB_EXPORT_DESCRIPTION = "ScannyBoy Export RGB (Adobe RGB 1998 compatible)"
ADOBE_RGB_EXPORT_GREY_DESCRIPTION = "ScannyBoy Export Grey (Adobe RGB 1998 compatible)"

# The disclaimer EXPORT_PLAN section 2.3 requires lives in `cprt`, not
# `desc`: it is a copyright notice, `cprt` is the tag for one, and a
# `desc` this long is precisely what macOS refuses to display.
ADOBE_RGB_EXPORT_COPYRIGHT = (
    "Colour space compatible with Adobe RGB (1998): same primaries, white "
    "point and transfer function. Generated by "
    "cli/tools/generate_icc_profile.py; not an Adobe product and not "
    "derived from Adobe's profile."
)

# Adobe RGB (1998), as published: D65 white and the CIE xy primaries.
ADOBE_RGB_WHITEPOINT_XY = (0.3127, 0.3290)
ADOBE_RGB_PRIMARIES_XY = (0.6400, 0.3300, 0.2100, 0.7100, 0.1500, 0.0600)

# D50 PCS white point, in s15Fixed16. lcms2 writes this for the RGB
# profile on its own; the grey profile needs it set explicitly (below).
ADOBE_RGB_WTPT = (63190, 65536, 54061)
# Bradford D65 -> D50 chromatic adaptation, in s15Fixed16. lcms2 derives
# exactly these nine values for the RGB profile; the grey profile carries
# them so a mono and a colour export make the same white-point claim.
ADOBE_RGB_CHAD = (
    68674, 1502, -3291,
    1939, 64912, -1119,
    -606, 988, 49262,
)
# The TRC gamma, in s15Fixed16: 144128 / 65536 = 2.19921875 = 563/256,
# the published Adobe RGB value and exactly representable. One constant,
# shared with `icc_profile.TRC_G_EXPORT` and `render.GAMMA_ADOBE`
# (EXPORT_PLAN section 2.2). lcms2 writes it into a `para` type-0 curve,
# the same shape the other three profiles already use.
EXPORT_TRC_S15FIXED16 = 144128
EXPORT_GAMMA = EXPORT_TRC_S15FIXED16 / 65536.0

# lcms2 stamps the wall-clock time into the header's creation date, which
# would make the generator non-deterministic and break the pinned
# SHA-256s. Pin it instead: 2026-01-01T00:00:00Z, the export profiles'
# epoch.
PINNED_CREATION_DATE = (2026, 1, 1, 0, 0, 0)

XYZ_TYPE = b"XYZ "
SF32_TYPE = b"sf32"


def _build_xyz_tag(ints: tuple[int, ...]) -> bytes:
    return XYZ_TYPE + b"\x00\x00\x00\x00" + b"".join(
        _s15fixed16(value) for value in ints
    )


def _build_sf32_tag(ints: tuple[int, ...]) -> bytes:
    """`s15Fixed16ArrayType` — the type `chad` must carry. The previous
    hand-built profiles stamped `XYZ ` here, so a parser dispatching on
    the type signature read one XYZNumber and ignored the other six
    values."""
    return SF32_TYPE + b"\x00\x00\x00\x00" + b"".join(
        _s15fixed16(value) for value in ints
    )


def _rewrite_tags(profile: bytes, replacements: dict[bytes, bytes]) -> bytes:
    """Return `profile` with each tag in `replacements` set to the given
    payload, adding it if absent, then the header's size, creation date
    and profile ID brought back into agreement.

    Payload bytes that compare equal share one offset, which is how a
    conformant writer registers rTRC/gTRC/bTRC against a single curve —
    and what `icc_profile_test.test_trc_tags_share_one_offset` checks.
    This is the only byte-level profile surgery left in this module; the
    structure, the colorimetry and every type signature come from lcms2.
    """
    payloads: dict[bytes, bytes] = {
        sig: profile[offset : offset + size]
        for sig, offset, size in _parse_tag_table(profile)
    }
    payloads.update(replacements)

    tag_count = len(payloads)
    data_offset = _pad4(128 + 4 + tag_count * 12)
    current = data_offset

    entries: list[tuple[bytes, int, int]] = []
    blob = bytearray()
    placed: dict[bytes, tuple[int, int]] = {}
    for sig, payload in payloads.items():
        if payload in placed:
            entries.append((sig, *placed[payload]))
            continue
        entries.append((sig, current, len(payload)))
        placed[payload] = (current, len(payload))
        blob.extend(payload)
        padding = _pad4(len(payload)) - len(payload)
        blob.extend(b"\x00" * padding)
        current += len(payload) + padding

    out = bytearray(profile[:128])
    struct.pack_into(">I", out, 0, current)
    struct.pack_into(">6H", out, 24, *PINNED_CREATION_DATE)
    out.extend(struct.pack(">I", tag_count))
    for sig, tag_offset, size in entries:
        out.extend(sig)
        out.extend(struct.pack(">I", tag_offset))
        out.extend(struct.pack(">I", size))
    if len(out) < data_offset:
        out.extend(b"\x00" * (data_offset - len(out)))
    out.extend(blob)

    struct.pack_into(">16s", out, 84, _profile_id(bytes(out)))
    return bytes(out)


def generate_export_rgb_profile() -> bytes:
    """The export's RGB space (EXPORT_PLAN section 2): Adobe RGB (1998)'s
    published colorimetry, laid out by lcms2, with this project's own
    description and copyright. The colorimetry *is* a claim here — the
    export is a rendered positive whose pixels really are in this
    space."""
    profile = cms_profile(
        "rgb",
        whitepoint=ADOBE_RGB_WHITEPOINT_XY,
        primaries=ADOBE_RGB_PRIMARIES_XY,
        gamma=EXPORT_GAMMA,
    )
    return _rewrite_tags(
        profile,
        {
            b"desc": _build_desc_tag(ADOBE_RGB_EXPORT_DESCRIPTION),
            b"cprt": _build_desc_tag(ADOBE_RGB_EXPORT_COPYRIGHT),
        },
    )


def generate_export_grey_profile() -> bytes:
    """The grey-class export companion (EXPORT_PLAN section 2.2): the
    same TRC as the RGB profile — a mono roll and a colour roll of the
    same scene must have identical tonality — with a D50 `wtpt` and the
    same `chad`, and no colorants (GRAY class).

    lcms2's gray profile writes a D65 `wtpt` and no `chad`, which is the
    older non-conformant convention section 2.2 rejects; the conformant
    D50 + `chad` pair is set here."""
    profile = cms_profile(
        "gray",
        whitepoint=ADOBE_RGB_WHITEPOINT_XY,
        gamma=EXPORT_GAMMA,
    )
    return _rewrite_tags(
        profile,
        {
            b"desc": _build_desc_tag(ADOBE_RGB_EXPORT_GREY_DESCRIPTION),
            b"cprt": _build_desc_tag(ADOBE_RGB_EXPORT_COPYRIGHT),
            b"wtpt": _build_xyz_tag(ADOBE_RGB_WTPT),
            b"chad": _build_sf32_tag(ADOBE_RGB_CHAD),
        },
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} OUTPUT_DIR", file=sys.stderr)
        return 2
    output = Path(argv[1])
    output.mkdir(parents=True, exist_ok=True)
    (output / "ScannyBoy-Linear-v1.icc").write_bytes(generate_linear_profile())
    (output / "ScannyBoy-Density-v1.icc").write_bytes(
        generate_density_profile()
    )
    (output / "ScannyBoy-Density-Grey-v1.icc").write_bytes(
        generate_density_grey_profile()
    )
    (output / "ScannyBoy-Export-AdobeRGB-v1.icc").write_bytes(
        generate_export_rgb_profile()
    )
    (output / "ScannyBoy-Export-Grey-v1.icc").write_bytes(
        generate_export_grey_profile()
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
