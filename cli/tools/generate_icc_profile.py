#!/usr/bin/env python3
"""Deterministic generator for the bundled Scanny Boy ICC profiles.

Derived from the committed ProPhoto-v4.icc bytes: primaries, white point and
chromatic-adaptation tag are carried over byte-identical, the description is
rewritten, and the TRC tags become a parametric function type 0 (pure
gamma). Two profiles (docs/DECISIONS.md, "Normalization decisions"):

- `ScannyBoy-Linear-ProPhoto-v1.icc`, g = 1.0 — the identity, declaring
  that the prepare stage's intermediate pixels are linear
  (`raw_decode.RAW_PARAMS` decodes linear).
- `ScannyBoy-Density-ProPhoto-v1.icc`, g = 2.2 — a **viewing convention**
  for the published, normalized log-density TIFF, not a colorimetric
  claim. A normalized log encoding over ~2 decades is closer to gamma 3.3
  than 2.2, and no ICC parametric type expresses it exactly anyway; but a
  *correct* profile would decode the file back to un-normalized linear —
  it would show the orange-masked raw scan, undoing the one thing the
  normalization stage does. The tag exists to make the file legible in an
  external viewer while debugging the edit stage. Every internal consumer
  decodes through `normalization.decode_normalized`, never through an ICC
  transform.
"""

from __future__ import annotations

import base64
import hashlib
import struct
import sys
from pathlib import Path

# Committed ProPhoto-v4.icc bytes, vendored so the generator keeps working
# after that file is deleted.
_PROPHOTO_V4_ICC_B64 = "AAAB4GxjbXMEIAAAbW50clJHQiBYWVogB+IAAwAUAAkADgAdYWNzcE1TRlQAAAAAc2F3c2N0cmwAAAAAAAAAAAAAAAAAAPbWAAEAAAAA0y1oYW5kH9ZD3wQwsLzdCGIbXzs4jAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKZGVzYwAAAPwAAAAkY3BydAAAASAAAAAid3RwdAAAAUQAAAAUY2hhZAAAAVgAAAAsclhZWgAAAYQAAAAUZ1hZWgAAAZgAAAAUYlhZWgAAAawAAAAUclRSQwAAAcAAAAAgZ1RSQwAAAcAAAAAgYlRSQwAAAcAAAAAgbWx1YwAAAAAAAAABAAAADGVuVVMAAAAIAAAAHABSAE8ATQBNbWx1YwAAAAAAAAABAAAADGVuVVMAAAAGAAAAHABDAEMAMAAAWFlaIAAAAAAAAPbWAAEAAAAA0y1zZjMyAAAAAAAA//3////+/////v////0AAQAD//////////8AAAABAAD/71hZWiAAAAAAAADMNwAASb4AAAAAWFlaIAAAAAAAACKaAAC2PQAAAAFYWVogAAAAAAAACAUAAAAFAADTLHBhcmEAAAAAAAMAAAABzM0AAQAAAAAAAAAAEAAAAAgA"

LINEAR_DESCRIPTION = "Scanny Boy Linear RGB (ProPhoto primaries, linear TRC)"

DENSITY_DESCRIPTION = (
    "ScannyBoy Normalized Density (viewing gamma 2.2). A viewing convention "
    "for normalized log-density working files, not a colorimetric claim: "
    "the pixels are per-channel affine-stretched log10 density, which no "
    "ICC TRC expresses exactly. Decode through "
    "scanny_boy.normalization.decode_normalized, never through this "
    "profile - it exists so external viewers show approximately the code "
    "values."
)

DENSITY_GREY_DESCRIPTION = (
    "ScannyBoy Normalized Density Grey (viewing gamma 2.2). The "
    "single-channel companion of the Density profile: a mono roll's "
    "published TIFF is one grayscale channel of the same normalized log "
    "density, which a ProPhoto RGB profile cannot tag. Same viewing "
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
    encoded = text.encode("utf-16-be") + b"\x00\x00"
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


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} OUTPUT_DIR", file=sys.stderr)
        return 2
    output = Path(argv[1])
    output.mkdir(parents=True, exist_ok=True)
    (output / "ScannyBoy-Linear-ProPhoto-v1.icc").write_bytes(generate_linear_profile())
    (output / "ScannyBoy-Density-ProPhoto-v1.icc").write_bytes(
        generate_density_profile()
    )
    (output / "ScannyBoy-Density-Grey-v1.icc").write_bytes(
        generate_density_grey_profile()
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
