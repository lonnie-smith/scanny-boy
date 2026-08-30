import hashlib
import importlib.util
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from scanny_boy.icc_profile import (
    PROFILE_SHA256,
    TRC_A,
    TRC_B,
    TRC_C,
    TRC_D,
    TRC_E,
    TRC_F,
    TRC_FUNCTION_TYPE,
    TRC_G,
    IccProfileError,
    load_icc_profile,
    verify_icc_profile,
)
from scanny_boy.romm import DECODE_LUT, MAX_CODE

REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATOR = REPO_ROOT / "cli" / "tools" / "generate_icc_profile.py"
COMMITTED_PROFILE = (
    Path(__file__).resolve().parent / "resources" / "ScannyBoy-ROMM-LibRaw-v4.icc"
)

_spec = importlib.util.spec_from_file_location("generate_icc_profile", GENERATOR)
_generator = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_generator)
PROPHOTO_BYTES = _generator.prophoto_source_bytes()

TRC_SIGNATURES = (b"rTRC", b"gTRC", b"bTRC")
TRC_PARAMS = (TRC_G, TRC_A, TRC_B, TRC_C, TRC_D, TRC_E, TRC_F)
PROFILE_ID = bytes.fromhex("d225fb183d7eda2c613cead9665f2843")


def _tag_entries(data: bytes) -> list[tuple[bytes, int, int]]:
    tag_count = struct.unpack(">I", data[128:132])[0]
    entries: list[tuple[bytes, int, int]] = []
    offset = 132
    for _ in range(tag_count):
        sig, tag_offset, size = struct.unpack(">4sII", data[offset : offset + 12])
        entries.append((sig, tag_offset, size))
        offset += 12
    return entries


def _parametric_curve_params(data: bytes, tag_signature: bytes) -> list[int]:
    for sig, tag_offset, _size in _tag_entries(data):
        if sig != tag_signature:
            continue
        assert data[tag_offset : tag_offset + 4] == b"para"
        func_type = struct.unpack(">H", data[tag_offset + 8 : tag_offset + 10])[0]
        assert func_type == TRC_FUNCTION_TYPE
        params_raw = data[tag_offset + 12 : tag_offset + 12 + 4 * len(TRC_PARAMS)]
        return [struct.unpack(">i", params_raw[i : i + 4])[0] for i in range(0, len(params_raw), 4)]
    raise AssertionError(f"tag {tag_signature!r} not found in profile")


def _decode_parametric_type_four(params: tuple[int, ...], encoded: np.ndarray) -> np.ndarray:
    g, a, b, c, d, e, f = (p / 65536.0 for p in params)
    linear = np.empty_like(encoded, dtype=np.float64)
    low = encoded < d
    linear[low] = e * encoded[low] + f
    linear[~low] = np.power(a * encoded[~low] + b, g) + c
    return linear


def test_generator_reproduces_the_committed_profile(tmp_path):
    generated = tmp_path / "generated.icc"
    subprocess.run(
        [sys.executable, str(GENERATOR), str(generated)],
        check=True,
    )
    assert generated.read_bytes() == COMMITTED_PROFILE.read_bytes()


def test_committed_profile_hash_matches_the_constant():
    data = COMMITTED_PROFILE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == PROFILE_SHA256


def test_trc_tag_is_parametric_type_four_with_the_section_313_parameters():
    data = load_icc_profile()
    params = _parametric_curve_params(data, b"rTRC")
    assert params == list(TRC_PARAMS)


def test_three_trc_tags_share_one_offset():
    data = load_icc_profile()
    trc_entries = [(sig, off, size) for sig, off, size in _tag_entries(data) if sig in TRC_SIGNATURES]
    assert len(trc_entries) == 3
    offsets = {entry[1] for entry in trc_entries}
    sizes = {entry[2] for entry in trc_entries}
    assert len(offsets) == 1
    assert len(sizes) == 1
    assert next(iter(sizes)) == 40


def test_primaries_white_point_and_chad_are_byte_identical_to_prophoto():
    data = load_icc_profile()
    for tag_name in (b"wtpt", b"chad", b"rXYZ", b"gXYZ", b"bXYZ"):
        src = next(
            PROPHOTO_BYTES[off : off + size]
            for sig, off, size in _tag_entries(PROPHOTO_BYTES)
            if sig == tag_name
        )
        new = next(data[off : off + size] for sig, off, size in _tag_entries(data) if sig == tag_name)
        assert new == src, tag_name


def test_profile_id_is_the_specified_md5():
    data = load_icc_profile()
    assert data[84:100] == PROFILE_ID


def test_decoded_curve_matches_libraw_within_quarter_of_an_lsb():
    data = load_icc_profile()
    params = tuple(_parametric_curve_params(data, b"rTRC"))
    codes = np.arange(MAX_CODE + 1, dtype=np.float64) / MAX_CODE
    decoded = _decode_parametric_type_four(params, codes)
    expected = DECODE_LUT.astype(np.float64)
    max_error = np.max(np.abs(decoded - expected) * MAX_CODE)
    assert max_error < 0.25


def test_decoded_curve_is_monotonic_and_spans_zero_to_one():
    data = load_icc_profile()
    params = tuple(_parametric_curve_params(data, b"rTRC"))
    codes = np.arange(MAX_CODE + 1, dtype=np.float64) / MAX_CODE
    decoded = _decode_parametric_type_four(params, codes)
    assert decoded[0] == pytest.approx(0.0)
    assert decoded[-1] == pytest.approx(1.0)
    assert np.all(np.diff(decoded) >= 0)


def test_load_icc_profile_still_verifies_and_returns_bytes():
    data = load_icc_profile()
    assert len(data) == 568
    assert hashlib.sha256(data).hexdigest() == PROFILE_SHA256


def test_verify_icc_profile_rejects_corrupted_data():
    with pytest.raises(IccProfileError) as exc_info:
        verify_icc_profile(b"not an icc profile")
    assert exc_info.value.code.value == "ICC_PROFILE_INVALID"
