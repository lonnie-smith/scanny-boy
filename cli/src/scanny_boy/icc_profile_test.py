import hashlib
import struct

import pytest

from scanny_boy.icc_profile import (
    PROFILE_SHA256,
    IccProfileError,
    load_icc_profile,
    verify_icc_profile,
)

# The ROMM encoded-domain breakpoint from section 3.4. Do not use
# 0.001953125 — that is the same breakpoint expressed in the linear domain.
EXPECTED_TRC_BREAKPOINT = 0.03125
WRONG_LINEAR_DOMAIN_BREAKPOINT = 0.001953125


def _s15fixed16(raw: bytes) -> float:
    return struct.unpack(">i", raw)[0] / 65536.0


def _parametric_curve_params(data: bytes, tag_signature: bytes) -> list[float]:
    """Parse an ICC `para` (parametricCurveType) tag's parameters directly
    from the profile bytes, independent of any third-party ICC library, so
    this test exercises the actual bundled file rather than trusting a
    description of it."""
    tag_count = struct.unpack(">I", data[128:132])[0]
    offset = 132
    for _ in range(tag_count):
        sig, tag_offset, _size = struct.unpack(">4sII", data[offset : offset + 12])
        offset += 12
        if sig == tag_signature:
            assert data[tag_offset : tag_offset + 4] == b"para"
            func_type = struct.unpack(">H", data[tag_offset + 8 : tag_offset + 10])[0]
            assert func_type == 3, "expected parametric curve type 3 (g, a, b, c, d)"
            param_count = 5
            params_raw = data[tag_offset + 12 : tag_offset + 12 + 4 * param_count]
            return [
                _s15fixed16(params_raw[i : i + 4]) for i in range(0, 4 * param_count, 4)
            ]
    raise AssertionError(f"tag {tag_signature!r} not found in profile")


def test_load_icc_profile_matches_expected_sha256():
    data = load_icc_profile()
    assert len(data) == 480
    assert hashlib.sha256(data).hexdigest() == PROFILE_SHA256


def test_verify_icc_profile_accepts_the_bundled_file():
    verify_icc_profile(load_icc_profile())


def test_verify_icc_profile_rejects_corrupted_data():
    with pytest.raises(IccProfileError) as exc_info:
        verify_icc_profile(b"not an icc profile")
    assert exc_info.value.code.value == "ICC_PROFILE_INVALID"


def test_transfer_curve_breakpoint_matches_romm_encoded_domain():
    data = load_icc_profile()
    for tag in (b"rTRC", b"gTRC", b"bTRC"):
        g, _a, _b, c, d = _parametric_curve_params(data, tag)
        assert d == pytest.approx(EXPECTED_TRC_BREAKPOINT)
        assert d != pytest.approx(WRONG_LINEAR_DOMAIN_BREAKPOINT)
        # gamma=(1.8, 16) in section 3.4: rawpy inverts 1.8 and LibRaw
        # receives slope 16, i.e. c == 1/16 in the encoded-domain curve.
        assert g == pytest.approx(1.8, abs=1e-3)
        assert c == pytest.approx(1 / 16, abs=1e-6)
