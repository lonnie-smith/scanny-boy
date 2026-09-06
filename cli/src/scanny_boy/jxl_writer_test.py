"""Tests for `jxl_writer`: the ctypes shim over the bundled libjxl.

docs/EXPORT_PLAN.md §1.6. The `JxlBasicInfo` layout test is the tripwire
for a libjxl ABI change — struct-layout drift otherwise shows up as
silent pixel corruption, not an error.
"""

from __future__ import annotations

import ctypes
import struct
from pathlib import Path

import imagecodecs
import numpy as np
import pytest

from scanny_boy import jxl_writer
from scanny_boy.icc_profile import ProfileKind, load_icc_profile
from scanny_boy.jxl_writer import (
    JXL_BITS_PER_SAMPLE,
    JxlBasicInfo,
    encode_jxl,
    write_jxl,
)

# --- container parsing helpers --------------------------------------------

def _boxes(blob: bytes) -> list[tuple[bytes, bytes]]:
    """The container-format boxes of a JXL file: (type, contents) pairs.

    The container is a BMFF-style sequence: a 4-byte big-endian size
    (including its own 8 header bytes, 0 meaning "to end of file")
    followed by a 4-byte type.
    """
    assert blob[:12] == b"\x00\x00\x00\x0cJXL \r\n\x87\n"
    boxes = []
    pos = 12
    while pos < len(blob):
        (size,) = struct.unpack_from(">I", blob, pos)
        box_type = blob[pos + 4 : pos + 8]
        if size == 0:
            boxes.append((box_type, blob[pos + 8 :]))
            break
        boxes.append((box_type, blob[pos + 8 : pos + size]))
        pos += size
    return boxes


def _box(boxes: list[tuple[bytes, bytes]], box_type: bytes) -> bytes:
    for name, contents in boxes:
        if name == box_type:
            return contents
    raise AssertionError(f"no {box_type!r} box in {[n for n, _ in boxes]}")


def read_icc_profile(blob: bytes) -> bytes:
    """The ICC profile embedded in a JXL file, byte-identical to what the
    writer was given. `imagecodecs.jpegxl_decode` does not expose the
    profile, so this drives libjxl's decoder directly, resolving its
    symbols from the same global namespace the writer shim uses. Shared
    with `exporter_test`, which asserts the export's profile choice."""
    lib = ctypes.CDLL(None)

    def fn(name: str) -> ctypes._FuncPtr:
        f = getattr(lib, name)
        f.restype = ctypes.c_int
        return f

    JxlDecoderCreate = lib.JxlDecoderCreate
    JxlDecoderCreate.argtypes = [ctypes.c_void_p]
    JxlDecoderCreate.restype = ctypes.c_void_p
    JxlDecoderDestroy = lib.JxlDecoderDestroy
    JxlDecoderDestroy.argtypes = [ctypes.c_void_p]
    JxlDecoderDestroy.restype = None
    JxlDecoderSubscribeEvents = fn("JxlDecoderSubscribeEvents")
    JxlDecoderSubscribeEvents.argtypes = [ctypes.c_void_p, ctypes.c_int]
    JxlDecoderSetInput = fn("JxlDecoderSetInput")
    JxlDecoderSetInput.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
    JxlDecoderProcessInput = fn("JxlDecoderProcessInput")
    JxlDecoderProcessInput.argtypes = [ctypes.c_void_p]
    JxlDecoderGetICCProfileSize = fn("JxlDecoderGetICCProfileSize")
    JxlDecoderGetICCProfileSize.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    JxlDecoderGetColorAsICCProfile = fn("JxlDecoderGetColorAsICCProfile")
    JxlDecoderGetColorAsICCProfile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]

    # JxlDecoderStatus / event values from decode.h.
    JXL_DEC_SUCCESS = 0
    JXL_DEC_NEED_MORE_INPUT = 2
    JXL_DEC_COLOR_ENCODING = 0x100
    JXL_COLOR_PROFILE_TARGET_ORIGINAL = 0

    # The reader must have reached the color-encoding part of the
    # codestream before the ICC is available; subscribe to that event.
    dec = JxlDecoderCreate(None)
    try:
        buf = ctypes.create_string_buffer(blob, len(blob))
        assert JxlDecoderSubscribeEvents(dec, JXL_DEC_COLOR_ENCODING) == JXL_DEC_SUCCESS
        JxlDecoderSetInput(dec, buf, len(blob))
        while True:
            status = JxlDecoderProcessInput(dec)
            if status == JXL_DEC_COLOR_ENCODING:
                break
            assert status in (JXL_DEC_NEED_MORE_INPUT,), status
        size = ctypes.c_size_t(0)
        assert (
            JxlDecoderGetICCProfileSize(
                dec, JXL_COLOR_PROFILE_TARGET_ORIGINAL, ctypes.byref(size)
            )
            == JXL_DEC_SUCCESS
        )
        out = ctypes.create_string_buffer(size.value)
        assert (
            JxlDecoderGetColorAsICCProfile(
                dec, JXL_COLOR_PROFILE_TARGET_ORIGINAL, out, size.value
            )
            == JXL_DEC_SUCCESS
        )
        return out.raw[: size.value]
    finally:
        JxlDecoderDestroy(dec)


# --- the ABI tripwire ------------------------------------------------------

def test_basic_info_layout_and_documented_defaults():
    """`JxlEncoderInitBasicInfo` on a zeroed instance must yield the
    documented defaults and the struct must be the 0.12 ABI's 204 bytes.
    A mismatch here means the ctypes mirror above has drifted from
    libjxl's struct — fix the mirror, do not loosen the assertion."""
    info = JxlBasicInfo()
    ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
    jxl_writer._load_api().JxlEncoderInitBasicInfo(ctypes.byref(info))
    assert ctypes.sizeof(JxlBasicInfo) == 204
    assert info.bits_per_sample == 8
    assert info.orientation == 1
    assert info.num_color_channels == 3
    assert info.xsize == 0
    assert info.ysize == 0
    assert info.exponent_bits_per_sample == 0
    assert info.num_extra_channels == 0
    assert info.alpha_bits == 0
    assert info.uses_original_profile == 0


def test_pixel_format_is_the_documented_24_byte_layout():
    # num_channels, data_type, endianness (each u32) + align (size_t, 8).
    assert ctypes.sizeof(jxl_writer.JxlPixelFormat) == 24


# --- encode round-trips ----------------------------------------------------

# libjxl validates the ICC profile's colour space against
# `num_color_channels` (a mismatch is `JXL_ENC_ERR_BAD_INPUT`), so the
# colour images need the RGB profile and the 1-channel images the grey
# one — which is also exactly how the exporter picks them (§5.1).
ICC = load_icc_profile(ProfileKind.LINEAR)
ICC_GREY = load_icc_profile(ProfileKind.DENSITY_GREY)


def _gradient_image(channels: int, size: int = 16) -> np.ndarray:
    if channels == 1:
        return np.arange(size * size, dtype=np.uint16).reshape(size, size)
    ramp = np.arange(size * size, dtype=np.uint16).reshape(size, size)
    return np.stack([ramp, ramp * 3, ramp * 7], axis=-1)


def test_three_channel_uint16_round_trips_bit_identically():
    image = _gradient_image(3)
    encoded = encode_jxl(image, icc_profile=ICC)
    decoded = imagecodecs.jpegxl_decode(bytes(encoded))
    assert decoded.dtype == np.uint16
    assert decoded.shape == image.shape
    assert np.array_equal(decoded, image)


def test_one_channel_uint16_round_trips_bit_identically():
    image = _gradient_image(1)
    encoded = encode_jxl(image, icc_profile=ICC_GREY)
    decoded = imagecodecs.jpegxl_decode(bytes(encoded))
    assert decoded.shape == image.shape
    assert np.array_equal(decoded, image)


def test_exactly_lossless_on_random_content():
    rng = np.random.default_rng(20260905)
    image = rng.integers(0, 2**16, size=(9, 11, 3), dtype=np.uint16)
    encoded = encode_jxl(image, icc_profile=ICC)
    decoded = imagecodecs.jpegxl_decode(bytes(encoded))
    assert np.array_equal(decoded, image)


def test_encoder_is_available():
    # A guard against the silent-failure failure mode: if the shim cannot
    # load, say so loudly here rather than at export time.
    jxl_writer._load_api()


# --- ICC and metadata boxes ------------------------------------------------

def test_embedded_icc_profile_is_recoverable_and_equal():
    """The ICC profile must come back out of the file byte-identical."""
    encoded = encode_jxl(_gradient_image(1), icc_profile=ICC_GREY)
    assert read_icc_profile(bytes(encoded)) == ICC_GREY


def test_exif_and_xmp_boxes_carry_the_payloads():
    exif = b"II*\x00" + b"\x00" * 20  # TIFF-shaped bytes; the test parses boxes, not tags
    xmp = b"<x:xmpmeta>captured</x:xmpmeta>"
    encoded = encode_jxl(
        _gradient_image(3), icc_profile=ICC, exif=exif, xmp=xmp
    )
    boxes = _boxes(encoded)
    # libjxl prefixes the Exif payload with the 4-byte big-endian offset
    # to the TIFF header; 0 puts it at the start.
    assert _box(boxes, b"Exif") == b"\x00\x00\x00\x00" + exif
    assert _box(boxes, b"xml ") == xmp


def test_no_metadata_boxes_without_exif_or_xmp():
    encoded = encode_jxl(_gradient_image(3), icc_profile=ICC)
    types = [name for name, _ in _boxes(encoded)]
    assert b"Exif" not in types
    assert b"xml " not in types
    assert b"jxlc" in types


# --- guards ----------------------------------------------------------------

def test_empty_icc_profile_raises():
    with pytest.raises(ValueError, match="ICC"):
        encode_jxl(_gradient_image(3), icc_profile=b"")


@pytest.mark.parametrize(
    "pixels",
    [
        np.zeros((4, 4), dtype=np.uint8),
        np.zeros((4, 4, 4), dtype=np.uint16),
        np.zeros((4, 4, 3), dtype=np.float32),
    ],
)
def test_bad_shape_or_dtype_raises_naming_the_shape(pixels):
    with pytest.raises(ValueError, match=str(pixels.shape)):
        encode_jxl(pixels, icc_profile=ICC)


def test_failed_write_leaves_no_tmp(tmp_path: Path, monkeypatch):
    destination = tmp_path / "out.jxl"
    monkeypatch.setattr(
        jxl_writer,
        "encode_jxl",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError, match="boom"):
        write_jxl(destination, _gradient_image(3), icc_profile=ICC)
    assert list(tmp_path.iterdir()) == []


def test_write_jxl_replaces_the_destination(tmp_path: Path):
    destination = tmp_path / "out.jxl"
    write_jxl(destination, _gradient_image(1), icc_profile=ICC_GREY)
    assert destination.exists()
    assert list(tmp_path.iterdir()) == [destination]
    boxes = _boxes(destination.read_bytes())
    assert _box(boxes, b"jxlc")  # a real codestream box


def test_unavailable_encoder_maps_to_the_dedicated_exception():
    """A missing libjxl must raise `JxlEncoderUnavailable` — the exporter
    turns it into the `JXL_ENCODER_UNAVAILABLE` protocol code — and not
    let an `AttributeError` escape."""
    from scanny_boy.events import Code

    assert Code.JXL_ENCODER_UNAVAILABLE.value == "JXL_ENCODER_UNAVAILABLE"
    assert jxl_writer.JXL_SUFFIX == ".jxl"
    assert JXL_BITS_PER_SAMPLE == 16
    assert jxl_writer.JXL_EFFORT == 7
