import hashlib
import importlib.util
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from scanny_boy.icc_profile import (
    DENSITY_GREY_PROFILE_SHA256,
    DENSITY_PROFILE_SHA256,
    EXPORT_GREY_PROFILE_SHA256,
    EXPORT_RGB_PROFILE_SHA256,
    LINEAR_PROFILE_SHA256,
    PROFILES,
    TRC_FUNCTION_TYPE,
    TRC_G_DENSITY,
    TRC_G_EXPORT,
    TRC_G_LINEAR,
    IccProfileError,
    ProfileKind,
    export_profile_kind,
    load_icc_profile,
    published_profile_kind,
    verify_icc_profile,
)
from scanny_boy.linear import MAX_CODE

REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATOR = REPO_ROOT / "cli" / "tools" / "generate_icc_profile.py"
RESOURCES = Path(__file__).resolve().parent / "resources"
COMMITTED_LINEAR = RESOURCES / "ScannyBoy-Linear-v1.icc"
COMMITTED_DENSITY = RESOURCES / "ScannyBoy-Density-v1.icc"
COMMITTED_DENSITY_GREY = RESOURCES / "ScannyBoy-Density-Grey-v1.icc"
COMMITTED_EXPORT_RGB = RESOURCES / "ScannyBoy-Export-AdobeRGB-v1.icc"
COMMITTED_EXPORT_GREY = RESOURCES / "ScannyBoy-Export-Grey-v1.icc"

_spec = importlib.util.spec_from_file_location("generate_icc_profile", GENERATOR)
_generator = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_generator)
PROPHOTO_BYTES = _generator.prophoto_source_bytes()

TRC_SIGNATURES = (b"rTRC", b"gTRC", b"bTRC")
LINEAR_TRC_PARAMS = (TRC_G_LINEAR,)
DENSITY_TRC_PARAMS = (TRC_G_DENSITY,)
LINEAR_PROFILE_ID = bytes.fromhex("ded7f19b8a02c80ba34ac389584e9cd9")

# The three profiles whose colorants are the vendored ProPhoto source's
# wide container (docs/PROFILE_HONESTY_PLAN.md). The two export profiles
# are built from Adobe RGB's published colorimetry instead
# (docs/EXPORT_PLAN.md §2), so the byte-identity test does not extend to
# them — and their descriptions *are* a measurement claim, so the "wide
# container" description test does not either.
WORKING_KINDS = (
    ProfileKind.LINEAR,
    ProfileKind.DENSITY,
    ProfileKind.DENSITY_GREY,
)
GREY_KINDS = frozenset({ProfileKind.DENSITY_GREY, ProfileKind.EXPORT_GREY})

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
    assert (
        generated / COMMITTED_DENSITY_GREY.name
    ).read_bytes() == COMMITTED_DENSITY_GREY.read_bytes()
    assert (
        generated / COMMITTED_EXPORT_RGB.name
    ).read_bytes() == COMMITTED_EXPORT_RGB.read_bytes()
    assert (
        generated / COMMITTED_EXPORT_GREY.name
    ).read_bytes() == COMMITTED_EXPORT_GREY.read_bytes()


def test_generator_is_deterministic(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    for directory in (first, second):
        subprocess.run([sys.executable, str(GENERATOR), str(directory)], check=True)
    for name in (
        COMMITTED_LINEAR.name,
        COMMITTED_DENSITY.name,
        COMMITTED_DENSITY_GREY.name,
        COMMITTED_EXPORT_RGB.name,
        COMMITTED_EXPORT_GREY.name,
    ):
        assert (first / name).read_bytes() == (second / name).read_bytes()


@pytest.mark.parametrize(
    ("path", "expected_sha"),
    [
        (COMMITTED_LINEAR, LINEAR_PROFILE_SHA256),
        (COMMITTED_DENSITY, DENSITY_PROFILE_SHA256),
        (COMMITTED_DENSITY_GREY, DENSITY_GREY_PROFILE_SHA256),
        (COMMITTED_EXPORT_RGB, EXPORT_RGB_PROFILE_SHA256),
        (COMMITTED_EXPORT_GREY, EXPORT_GREY_PROFILE_SHA256),
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
def test_trc_tags_share_one_offset(kind):
    data = load_icc_profile(kind)
    grey = kind in GREY_KINDS
    signatures = (b"kTRC",) if grey else TRC_SIGNATURES
    trc_entries = [
        (sig, off, size)
        for sig, off, size in _tag_entries(data)
        if sig in signatures
    ]
    assert len(trc_entries) == (1 if grey else 3)
    offsets = {entry[1] for entry in trc_entries}
    sizes = {entry[2] for entry in trc_entries}
    assert len(offsets) == 1
    assert len(sizes) == 1


@pytest.mark.parametrize("kind", WORKING_KINDS)
def test_wide_container_colorants_white_point_and_chad_are_unchanged_from_the_vendored_source(kind):
    data = load_icc_profile(kind)
    # The grey profile is a gray-class profile: it carries no RGB matrix,
    # by construction (MONOCHROME_PLAN section 4).
    matrix_tags = (
        ()
        if kind is ProfileKind.DENSITY_GREY
        else (b"rXYZ", b"gXYZ", b"bXYZ")
    )
    for tag_name in (b"wtpt", b"chad", *matrix_tags):
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


def _profile_description(data: bytes, tag_signature: bytes = b"desc") -> str:
    """The en-US string of an `mluc` tag — `desc` by default, `cprt` for
    the export profiles' Adobe disclaimer (EXPORT_PLAN §2.3)."""
    sig, tag_offset, _size = next(
        entry for entry in _tag_entries(data) if entry[0] == tag_signature
    )
    assert sig == tag_signature
    assert data[tag_offset : tag_offset + 4] == b"mluc"
    record_count = struct.unpack(">I", data[tag_offset + 8 : tag_offset + 12])[0]
    record_size = struct.unpack(">I", data[tag_offset + 12 : tag_offset + 16])[0]
    for index in range(record_count):
        base = tag_offset + 16 + index * record_size
        string_length = struct.unpack(">I", data[base + 4 : base + 8])[0]
        string_offset = struct.unpack(">I", data[base + 8 : base + 12])[0]
        return (
            data[tag_offset + string_offset : tag_offset + string_offset + string_length]
            .decode("utf-16-be")
            .rstrip("\x00")
        )
    raise AssertionError("desc tag carries no records")


@pytest.mark.parametrize("kind", list(ProfileKind))
def test_no_filename_or_description_claims_prophoto(kind):
    data = load_icc_profile(kind)
    filename = PROFILES[kind][0]
    assert "ProPhoto" not in filename
    assert "ProPhoto" not in _profile_description(data)


@pytest.mark.parametrize("kind", WORKING_KINDS)
def test_every_description_declares_a_wide_container_not_a_measurement(kind):
    data = load_icc_profile(kind)
    description = _profile_description(data).lower()
    assert "wide container" in description
    assert "not a measurement" in description


def test_density_grey_is_a_gray_class_profile():
    """MONOCHROME_PLAN section 4: a mono roll's published TIFF is a
    1-channel (minisblack) file, which an RGB-colorspace profile cannot
    tag. The grey profile declares the GRAY data colour space, carries one
    kTRC with the density viewing gamma, and drops the RGB matrix."""
    data = load_icc_profile(ProfileKind.DENSITY_GREY)
    assert data[16:20] == b"GRAY"
    signatures = {sig for sig, _off, _size in _tag_entries(data)}
    assert signatures == {b"desc", b"cprt", b"wtpt", b"chad", b"kTRC"}
    assert _parametric_curve_params(data, b"kTRC") == list(DENSITY_TRC_PARAMS)


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
    assert len(data) == 1136
    assert hashlib.sha256(data).hexdigest() == LINEAR_PROFILE_SHA256
    density = load_icc_profile(ProfileKind.DENSITY)
    assert len(density) == 1776
    assert hashlib.sha256(density).hexdigest() == DENSITY_PROFILE_SHA256
    grey = load_icc_profile(ProfileKind.DENSITY_GREY)
    assert len(grey) == 1508
    assert hashlib.sha256(grey).hexdigest() == DENSITY_GREY_PROFILE_SHA256


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
    # And the grey profile is a third, distinct byte string (MONOCHROME_PLAN
    # section 4): it fails both RGB verifications, they fail its own.
    grey = load_icc_profile(ProfileKind.DENSITY_GREY)
    with pytest.raises(IccProfileError):
        verify_icc_profile(grey, ProfileKind.DENSITY)
    with pytest.raises(IccProfileError):
        verify_icc_profile(density, ProfileKind.DENSITY_GREY)
    verify_icc_profile(grey, ProfileKind.DENSITY_GREY)


def test_published_profile_kind_selects_by_film_kind():
    """MONOCHROME_PLAN §2.3/§4: the invariant seed and the published-TIFF
    tag site both select through `published_profile_kind` — DENSITY_GREY on
    a mono roll, DENSITY otherwise; unknown kinds fall to colour (the
    pre-§2 default)."""
    assert published_profile_kind("colour") is ProfileKind.DENSITY
    assert published_profile_kind("monochrome") is ProfileKind.DENSITY_GREY
    assert published_profile_kind() is ProfileKind.DENSITY


def test_export_profile_kind_selects_by_channel_count():
    """EXPORT_PLAN §4.5: the export tag site selects on the channel count
    the writer actually sees — EXPORT_GREY for a 1-channel (mono roll)
    export, EXPORT_RGB otherwise."""
    assert export_profile_kind(1) is ProfileKind.EXPORT_GREY
    assert export_profile_kind(3) is ProfileKind.EXPORT_RGB


# --- the export profiles (docs/EXPORT_PLAN.md section 2) -------------------

# The §2.2 pinned values: the D50-adapted colorants lcms2 derives from the
# published Adobe RGB (1998) chromaticities. All but one agree byte for
# byte with Apple's AdobeRGB1998.icc; `bXYZ`'s Z is 48795 where Apple's
# file rounds 0.744568 up to 48796, a difference of 1/65536 (§2.2).
ADOBE_RGB_EXPORT_TAGS = {
    b"wtpt": (63190, 65536, 54061),
    b"rXYZ": (39960, 20389, 1276),
    b"gXYZ": (13453, 41004, 3989),
    b"bXYZ": (9777, 4143, 48795),
}
ADOBE_RGB_EXPORT_CHAD = (
    68674, 1502, -3291,
    1939, 64912, -1119,
    -606, 988, 49262,
)


def _typed_s15fixed16_array(
    data: bytes, tag_signature: bytes, expected_type: bytes
) -> tuple[int, ...]:
    payload = next(
        data[off : off + size]
        for sig, off, size in _tag_entries(data)
        if sig == tag_signature
    )
    assert payload[:4] == expected_type, (tag_signature, payload[:4])
    assert payload[4:8] == b"\x00\x00\x00\x00"
    body = payload[8:]
    return tuple(
        struct.unpack(">i", body[i : i + 4])[0] for i in range(0, len(body), 4)
    )


def _xyz_tag_payload(data: bytes, tag_signature: bytes) -> tuple[int, ...]:
    return _typed_s15fixed16_array(data, tag_signature, b"XYZ ")


def _chad_tag_payload(data: bytes) -> tuple[int, ...]:
    """`chad` must be `s15Fixed16ArrayType`. The hand-assembled v1 export
    profiles stamped `XYZ ` here instead, so a parser dispatching on the
    type signature read one XYZNumber and dropped the other six values;
    that is why this asserts the type and not only the numbers."""
    return _typed_s15fixed16_array(data, b"chad", b"sf32")


def test_export_rgb_profile_carries_the_pinned_adobe_rgb_values():
    data = load_icc_profile(ProfileKind.EXPORT_RGB)
    for signature, expected in ADOBE_RGB_EXPORT_TAGS.items():
        assert _xyz_tag_payload(data, signature) == expected, signature
    assert _chad_tag_payload(data) == ADOBE_RGB_EXPORT_CHAD
    assert _parametric_curve_params(data, b"rTRC") == [TRC_G_EXPORT]


def test_export_rgb_profile_is_an_rgb_monitor_profile():
    data = load_icc_profile(ProfileKind.EXPORT_RGB)
    assert data[12:16] == b"mntr"
    assert data[16:20] == b"RGB "
    assert data[20:24] == b"XYZ "
    signatures = {sig for sig, _off, _size in _tag_entries(data)}
    # `cprt` is required of every ICC profile and was missing from the
    # hand-assembled v1; `chrm` is lcms2 recording the chromaticities it
    # built the colorants from.
    assert signatures == {
        b"desc", b"cprt", b"chrm", b"wtpt", b"chad", b"rXYZ", b"gXYZ", b"bXYZ",
        b"rTRC", b"gTRC", b"bTRC",
    }


def test_export_grey_profile_is_a_gray_class_profile():
    """EXPORT_PLAN §2.2: the grey export profile carries a D50 `wtpt`, the
    same `chad`, a single `kTRC` with the *same* gamma as the RGB
    profile's TRCs, and no colorants at all."""
    data = load_icc_profile(ProfileKind.EXPORT_GREY)
    assert data[12:16] == b"mntr"
    assert data[16:20] == b"GRAY"
    assert data[20:24] == b"XYZ "
    signatures = {sig for sig, _off, _size in _tag_entries(data)}
    assert signatures == {b"desc", b"cprt", b"wtpt", b"chad", b"kTRC"}
    # lcms2's gray profile writes a D65 `wtpt` and no `chad` — the older
    # non-conformant convention §2.2 rejects. The generator replaces the
    # pair, so a mono and a colour export make the same white-point claim.
    assert _xyz_tag_payload(data, b"wtpt") == ADOBE_RGB_EXPORT_TAGS[b"wtpt"]
    assert _chad_tag_payload(data) == ADOBE_RGB_EXPORT_CHAD
    with pytest.raises(StopIteration):
        _xyz_tag_payload(data, b"rXYZ")


def test_the_export_profiles_trc_gammas_are_equal_and_equal_trc_g_export():
    rgb = load_icc_profile(ProfileKind.EXPORT_RGB)
    grey = load_icc_profile(ProfileKind.EXPORT_GREY)
    gammas = {
        tuple(_parametric_curve_params(rgb, b"rTRC")),
        tuple(_parametric_curve_params(rgb, b"gTRC")),
        tuple(_parametric_curve_params(rgb, b"bTRC")),
        tuple(_parametric_curve_params(grey, b"kTRC")),
    }
    assert gammas == {(TRC_G_EXPORT,)}
    # 563/256 = 2.19921875, the published Adobe RGB gamma, exactly
    # representable in s15Fixed16 — which is why one integer serves the
    # profiles and `render.GAMMA_ADOBE` alike.
    assert TRC_G_EXPORT == 563 / 256 * 65536


def test_the_export_profiles_name_themselves_in_a_description_macos_will_show():
    """§2.3: the short name goes in `desc` and the Adobe disclaimer in
    `cprt`. ColorSync returns an empty description for a `desc` of 100
    characters or more, which is why the disclaimer cannot live there."""
    for kind, name in (
        (ProfileKind.EXPORT_RGB, "ScannyBoy Export RGB"),
        (ProfileKind.EXPORT_GREY, "ScannyBoy Export Grey"),
    ):
        description = _profile_description(load_icc_profile(kind))
        assert description.startswith(name)
        assert "Adobe RGB 1998 compatible" in description
        assert len(description) < 100


def test_the_export_profiles_disclaim_adobe_in_their_copyright_tag():
    for kind in (ProfileKind.EXPORT_RGB, ProfileKind.EXPORT_GREY):
        copyright_text = _profile_description(
            load_icc_profile(kind), tag_signature=b"cprt"
        )
        assert "Adobe RGB (1998)" in copyright_text
        assert "not an Adobe product" in copyright_text
        assert "not derived from Adobe's profile" in copyright_text


def test_load_icc_profile_returns_the_export_profiles_with_pinned_hashes():
    rgb = load_icc_profile(ProfileKind.EXPORT_RGB)
    assert hashlib.sha256(rgb).hexdigest() == EXPORT_RGB_PROFILE_SHA256
    grey = load_icc_profile(ProfileKind.EXPORT_GREY)
    assert hashlib.sha256(grey).hexdigest() == EXPORT_GREY_PROFILE_SHA256


def test_verify_icc_profile_rejects_corrupted_data():
    with pytest.raises(IccProfileError) as exc_info:
        verify_icc_profile(b"not an icc profile", ProfileKind.LINEAR)
    assert exc_info.value.code.value == "ICC_PROFILE_INVALID"


def test_profiles_record_covers_every_kind():
    assert set(PROFILES) == set(ProfileKind)
    for filename, _sha in PROFILES.values():
        assert (RESOURCES / filename).exists()


def test_guard_nothing_outside_the_write_path_imports_the_loader():
    """The load-bearing rule of section 3.12: the profile must never creep
    into the render path, where a wrong TRC could corrupt pixels instead of
    merely looking odd. A grep-shaped test is the cheapest way to hold that
    line.

    `*_test.py` and `*_support.py` are exempt for the same reason: neither
    ships in the render path. The support modules build fixtures that stand in
    for real pipeline output — `work_dir_support.write_intermediate` writes a
    genuine Phase 1 TIFF, profile and all — so they need the loader precisely
    because they are imitating the write path this rule protects."""
    package = Path(__file__).resolve().parent
    offenders = []
    for path in sorted(package.glob("*.py")):
        if path.name in PROFILE_LOADER_MODULES or path.name.endswith(
            ("_test.py", "_support.py")
        ):
            continue
        text = path.read_text(encoding="utf-8")
        if "load_icc_profile" in text:
            offenders.append(path.name)
    assert offenders == []
