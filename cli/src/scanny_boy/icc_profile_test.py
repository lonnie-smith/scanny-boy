import hashlib
import importlib.util
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from scanny_boy.icc_profile import (
    DENSITY_PROFILE_SHA256,
    LINEAR_PROFILE_SHA256,
    PROFILES,
    TRC_FUNCTION_TYPE,
    TRC_G_DENSITY,
    TRC_G_LINEAR,
    IccProfileError,
    ProfileKind,
    load_icc_profile,
    verify_icc_profile,
)
from scanny_boy.linear import MAX_CODE

REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATOR = REPO_ROOT / "cli" / "tools" / "generate_icc_profile.py"
RESOURCES = Path(__file__).resolve().parent / "resources"
COMMITTED_LINEAR = RESOURCES / "ScannyBoy-Linear-ProPhoto-v1.icc"
COMMITTED_DENSITY = RESOURCES / "ScannyBoy-Density-ProPhoto-v1.icc"

_spec = importlib.util.spec_from_file_location("generate_icc_profile", GENERATOR)
_generator = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_generator)
PROPHOTO_BYTES = _generator.prophoto_source_bytes()

TRC_SIGNATURES = (b"rTRC", b"gTRC", b"bTRC")
LINEAR_TRC_PARAMS = (TRC_G_LINEAR,)
DENSITY_TRC_PARAMS = (TRC_G_DENSITY,)
LINEAR_PROFILE_ID = bytes.fromhex("a1108f985e63b5fa50788c48fad2ddd0")

# The guard test's exception set (docs/DECISIONS.md, "Normalization decisions"):
# the profile must never become load-bearing for the render, so only the
# modules that *write* a tagged file (or the profile module itself) may
# import the loader. Everything else — previews, the edit stage, the
# library — decodes through `normalization.decode_normalized`.
PROFILE_LOADER_MODULES = frozenset(
    {
        "icc_profile.py",
        "tiff_writer.py",
        "stitched_tiff.py",
        "exporter.py",
        "pipeline.py",
        "probe.py",
        "stitch_pipeline.py",
    }
)


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
        params_raw = data[tag_offset + 12 : tag_offset + 16]
        return [struct.unpack(">i", params_raw[i : i + 4])[0] for i in range(0, 4, 4)]
    raise AssertionError(f"tag {tag_signature!r} not found in profile")


def _trc_params(data: bytes, trc_params: tuple[int, ...]) -> list[int]:
    for sig, tag_offset, _size in _tag_entries(data):
        if sig != b"rTRC":
            continue
        assert data[tag_offset : tag_offset + 4] == b"para"
        func_type = struct.unpack(">H", data[tag_offset + 8 : tag_offset + 10])[0]
        assert func_type == TRC_FUNCTION_TYPE
        raw = data[tag_offset + 12 : tag_offset + 12 + 4 * len(trc_params)]
        return [struct.unpack(">i", raw[i : i + 4])[0] for i in range(0, len(raw), 4)]
    raise AssertionError("rTRC not found")


def _decode_parametric_type_zero(
    params: tuple[int, ...], encoded: np.ndarray
) -> np.ndarray:
    (g,) = params
    return np.power(encoded, g / 65536.0)


def test_generator_reproduces_the_committed_profiles(tmp_path):
    generated = tmp_path / "profiles"
    subprocess.run(
        [sys.executable, str(GENERATOR), str(generated)],
        check=True,
    )
    assert (
        generated / COMMITTED_LINEAR.name
    ).read_bytes() == COMMITTED_LINEAR.read_bytes()
    assert (
        generated / COMMITTED_DENSITY.name
    ).read_bytes() == COMMITTED_DENSITY.read_bytes()


def test_generator_is_deterministic(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    for directory in (first, second):
        subprocess.run([sys.executable, str(GENERATOR), str(directory)], check=True)
    for name in (COMMITTED_LINEAR.name, COMMITTED_DENSITY.name):
        assert (first / name).read_bytes() == (second / name).read_bytes()


@pytest.mark.parametrize(
    ("path", "expected_sha"),
    [
        (COMMITTED_LINEAR, LINEAR_PROFILE_SHA256),
        (COMMITTED_DENSITY, DENSITY_PROFILE_SHA256),
    ],
)
def test_committed_profile_hash_matches_the_constant(path, expected_sha):
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_sha


def test_linear_trc_is_parametric_type_zero_with_the_identity_gamma():
    data = load_icc_profile(ProfileKind.LINEAR)
    assert _trc_params(data, LINEAR_TRC_PARAMS) == list(LINEAR_TRC_PARAMS)
    assert TRC_FUNCTION_TYPE == 0


def test_density_trc_is_parametric_type_zero_with_viewing_gamma_2_2():
    data = load_icc_profile(ProfileKind.DENSITY)
    assert _trc_params(data, DENSITY_TRC_PARAMS) == list(DENSITY_TRC_PARAMS)
    assert TRC_G_DENSITY == 144179  # round(2.2 * 65536)


@pytest.mark.parametrize("kind", list(ProfileKind))
def test_three_trc_tags_share_one_offset(kind):
    data = load_icc_profile(kind)
    trc_entries = [
        (sig, off, size)
        for sig, off, size in _tag_entries(data)
        if sig in TRC_SIGNATURES
    ]
    assert len(trc_entries) == 3
    offsets = {entry[1] for entry in trc_entries}
    sizes = {entry[2] for entry in trc_entries}
    assert len(offsets) == 1
    assert len(sizes) == 1


@pytest.mark.parametrize("kind", list(ProfileKind))
def test_primaries_white_point_and_chad_are_byte_identical_to_prophoto(kind):
    data = load_icc_profile(kind)
    for tag_name in (b"wtpt", b"chad", b"rXYZ", b"gXYZ", b"bXYZ"):
        src = next(
            PROPHOTO_BYTES[off : off + size]
            for sig, off, size in _tag_entries(PROPHOTO_BYTES)
            if sig == tag_name
        )
        new = next(
            data[off : off + size]
            for sig, off, size in _tag_entries(data)
            if sig == tag_name
        )
        assert new == src, tag_name


def test_linear_profile_id_is_the_specified_md5():
    data = load_icc_profile(ProfileKind.LINEAR)
    assert data[84:100] == LINEAR_PROFILE_ID


@pytest.mark.parametrize("kind", list(ProfileKind))
def test_profile_header_declares_icc_v4(kind):
    data = load_icc_profile(kind)
    # Preferred CMM 4 bytes, then the header's version field: 0x04300000 is
    # the "ICC Version 4.3.0.0" lineage the vendored ProPhoto-v4 header
    # carries, preserved byte-identical by both profiles.
    version = struct.unpack(">I", data[8:12])[0]
    assert version >> 24 == 0x04


def test_decoded_linear_curve_is_the_identity():
    data = load_icc_profile(ProfileKind.LINEAR)
    params = tuple(_trc_params(data, LINEAR_TRC_PARAMS))
    codes = np.arange(MAX_CODE + 1, dtype=np.float64) / MAX_CODE
    decoded = _decode_parametric_type_zero(params, codes)
    assert np.max(np.abs(decoded - codes)) < 1e-9


def test_decoded_density_curve_is_monotonic_and_spans_zero_to_one():
    data = load_icc_profile(ProfileKind.DENSITY)
    params = tuple(_trc_params(data, DENSITY_TRC_PARAMS))
    codes = np.arange(MAX_CODE + 1, dtype=np.float64) / MAX_CODE
    decoded = _decode_parametric_type_zero(params, codes)
    assert decoded[0] == pytest.approx(0.0)
    assert decoded[-1] == pytest.approx(1.0)
    assert np.all(np.diff(decoded) >= 0)


def test_load_icc_profile_still_verifies_and_returns_bytes():
    data = load_icc_profile(ProfileKind.LINEAR)
    assert len(data) == 568
    assert hashlib.sha256(data).hexdigest() == LINEAR_PROFILE_SHA256
    density = load_icc_profile(ProfileKind.DENSITY)
    assert len(density) == 1232
    assert hashlib.sha256(density).hexdigest() == DENSITY_PROFILE_SHA256


def test_the_two_profiles_are_never_silently_swappable():
    """Section 3.12: a DENSITY byte string must fail a LINEAR verification,
    and vice versa — a swapped tag can never pass unnoticed."""
    linear = load_icc_profile(ProfileKind.LINEAR)
    density = load_icc_profile(ProfileKind.DENSITY)
    with pytest.raises(IccProfileError):
        verify_icc_profile(density, ProfileKind.LINEAR)
    with pytest.raises(IccProfileError):
        verify_icc_profile(linear, ProfileKind.DENSITY)
    verify_icc_profile(linear, ProfileKind.LINEAR)
    verify_icc_profile(density, ProfileKind.DENSITY)


def test_verify_icc_profile_rejects_corrupted_data():
    with pytest.raises(IccProfileError) as exc_info:
        verify_icc_profile(b"not an icc profile", ProfileKind.LINEAR)
    assert exc_info.value.code.value == "ICC_PROFILE_INVALID"


def test_profiles_record_covers_both_kinds():
    assert set(PROFILES) == {ProfileKind.LINEAR, ProfileKind.DENSITY}
    for filename, _sha in PROFILES.values():
        assert (RESOURCES / filename).exists()


def test_guard_nothing_outside_the_write_path_imports_the_loader():
    """The load-bearing rule of section 3.12: the profile must never creep
    into the render path, where a wrong TRC could corrupt pixels instead of
    merely looking odd. A grep-shaped test is the cheapest way to hold that
    line."""
    package = Path(__file__).resolve().parent
    offenders = []
    for path in sorted(package.glob("*.py")):
        if path.name in PROFILE_LOADER_MODULES or path.name.endswith("_test.py"):
            continue
        text = path.read_text(encoding="utf-8")
        if "load_icc_profile" in text:
            offenders.append(path.name)
    assert offenders == []
