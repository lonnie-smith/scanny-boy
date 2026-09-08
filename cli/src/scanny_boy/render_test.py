"""Tests for `render`: the export's rendering, at full resolution.

docs/EXPORT_PLAN.md §4.7. The two anchor tests are the load-bearing ones:
the no-matrix path is exact by construction (single LUT, no
exponentiation), and the colour path is bounded at 4 codes against it.
"""

from __future__ import annotations

import numpy as np
import pytest

from scanny_boy import normalization, render, tone
from scanny_boy import color
from scanny_boy import previews as _previews
from scanny_boy.normalization import encode_normalized

_GAMMA = render.GAMMA_ADOBE

# The §4.7 sweep: the grade/snap corners, a mid pair, and None.
_TONE_PARAM_SWEEP: list[dict[str, float] | None] = [None] + [
    {"grade_r": grade_r, "snap_gamma": snap_gamma}
    for grade_r, snap_gamma in (
        (50.0, 0.0),
        (50.0, 0.5),
        (50.0, -0.5),
        (115.0, 0.0),
        (115.0, 0.3),
        (180.0, 0.5),
        (180.0, -0.5),
    )
]

# A well-conditioned camera -> Adobe RGB matrix (row-normalized), standing
# in for a real body's in the fast-tier tests; the real one is exercised by
# the --slow direction check in metadata_test.
_TEST_MATRIX = render.export_matrix(
    [
        [0.7, 0.2, 0.1],
        [0.1, 0.75, 0.15],
        [0.05, 0.1, 0.85],
    ]
)


# --- the anchor, exact (no-matrix path) ------------------------------------


@pytest.mark.parametrize("tone_params", _TONE_PARAM_SWEEP)
def test_the_no_matrix_path_is_exact_against_the_tone_curve(tone_params):
    """With `matrix=None`, the render's output equals
    `rint(tone.curve_values(clip(1 - decode_normalized(code)), ...) *
    65535)` for *every* code — true by construction (one LUT, no
    exponentiation); the test is the guard against someone reintroducing
    the gamma round-trip there."""
    codes = np.arange(tone.MAX_CODE + 1, dtype=np.uint16)
    rendered, fractions = render.render_export(codes, None, tone_params)

    positive = np.maximum(
        1.0 - normalization.decode_normalized(codes.astype(np.float64)), 0.0
    )
    if tone_params is None:
        positive = np.clip(positive, 0.0, 1.0)
    expected = np.rint(tone_curve_reference(positive, tone_params) * tone.MAX_CODE)
    assert np.array_equal(rendered, expected.astype(np.uint16))
    assert fractions == (0.0,)


def tone_curve_reference(positive: np.ndarray, tone_params) -> np.ndarray:
    if tone_params is None:
        return positive
    return tone.curve_values(positive, tone.ToneParams(**tone_params))


# --- the anchor, bounded (colour path) -------------------------------------


@pytest.mark.parametrize("tone_params", _TONE_PARAM_SWEEP)
def test_the_colour_path_agrees_with_the_no_matrix_path_within_four_codes(
    tone_params,
):
    """The colour path with an identity matrix agrees with the no-matrix
    path to within 4 codes over the whole ramp (measured worst case 3),
    and the mean absolute difference is under 1 code — so a genuine
    regression cannot hide under a loose ceiling."""
    codes = np.arange(tone.MAX_CODE + 1, dtype=np.uint16)
    identity = np.eye(3, dtype=np.float32)
    colour, _ = render.render_export(
        np.stack([codes] * 3, axis=-1).reshape(1, -1, 3), identity, tone_params
    )
    flat, _ = render.render_export(codes.reshape(1, -1), None, tone_params)

    difference = np.abs(colour[0, :, 0].astype(np.int32) - flat[0].astype(np.int32))
    assert difference.max() <= 4
    assert difference.mean() < 1.0


def test_the_8_bit_preview_lut_and_the_16_bit_render_agree_within_one_8_bit_code():
    for tone_params in _TONE_PARAM_SWEEP:
        codes = np.arange(tone.MAX_CODE + 1, dtype=np.uint16)
        rendered, _ = render.render_export(codes, None, tone_params)
        preview_8bit = preview_lut(tone_params)

        rendered_8bit = np.rint(rendered / 257.0).astype(np.uint8)
        difference = np.abs(rendered_8bit.astype(np.int32) - preview_8bit.astype(np.int32))
        assert difference.max() <= 1, tone_params


def preview_lut(tone_params) -> np.ndarray:
    """The 8-bit preview LUT with the tone curve composed in — the shared
    `render.encode_positive_uint8` ramp."""
    codes = np.arange(tone.MAX_CODE + 1, dtype=np.uint16)
    return render.encode_positive_uint8(codes, None, tone_params)


# --- export_matrix ---------------------------------------------------------


def test_export_matrix_rows_sum_to_one_and_preserve_neutrals():
    m = _TEST_MATRIX
    assert np.allclose(m.sum(axis=1), 1.0)
    assert np.allclose(m @ np.ones(3), np.ones(3), atol=1e-12)


def test_export_matrix_rejects_singular_input():
    with pytest.raises(ValueError, match="singular"):
        render.export_matrix(
            [
                [0.7, 0.2, 0.1],
                [1.4, 0.4, 0.2],
                [0.05, 0.1, 0.85],
            ]
        )


def test_export_matrix_rejects_non_finite_input():
    with pytest.raises(ValueError, match="finite"):
        render.export_matrix([[np.nan] * 3] * 3)


def test_a_saturated_primary_is_changed_by_a_non_identity_matrix():
    """The test that proves the matrix is actually being applied, and not
    silently identity."""
    saturated = np.zeros((1, 1, 3), dtype=np.uint16)
    saturated[0, 0] = (60000, 2000, 2000)
    identity = np.eye(3, dtype=np.float32)
    unchanged, _ = render.render_export(saturated, identity, None)
    changed, _ = render.render_export(saturated, _TEST_MATRIX, None)
    assert not np.array_equal(changed, unchanged)


def test_a_neutral_wedge_survives_the_full_colour_chain_unchanged():
    """Neutral in, neutral out — the end-to-end version of the row-sum
    invariant (§0.3)."""
    grey = 12000
    wedge = np.full((1, 8, 3), grey, dtype=np.uint16)
    rendered, _ = render.render_export(wedge, _TEST_MATRIX, None)
    assert np.all(rendered[:, :, 0] == rendered[:, :, 1])
    assert np.all(rendered[:, :, 1] == rendered[:, :, 2])


def test_a_neutral_wedge_at_display_white_survives_the_colour_chain():
    """The encode's white point inverts to DISPLAY_CEILING; neutrals must
    still track (docs/HEADROOM.md §7.0)."""
    white_code = int(
        encode_normalized(np.array([0.0], dtype=np.float32))[0].astype(np.uint16)
    )
    wedge = np.full((1, 4, 3), white_code, dtype=np.uint16)
    rendered, fractions = render.render_export(
        wedge, _TEST_MATRIX, {"grade_r": 115.0, "snap_gamma": 0.0}
    )
    assert np.all(rendered[:, :, 0] == rendered[:, :, 1])
    assert np.all(rendered[:, :, 1] == rendered[:, :, 2])
    assert fractions == (0.0, 0.0, 0.0)


def test_the_uint16_gather_round_trips_the_extended_display_domain():
    """§2.3: rescaling the gather and the curve LUT must stay paired."""
    tone_params = {"grade_r": 115.0, "snap_gamma": 0.0, "shoulder": 0.3}
    codes = np.arange(tone.MAX_CODE + 1, dtype=np.uint16)
    rendered, _ = render.render_export(
        np.stack([codes] * 3, axis=-1).reshape(1, -1, 3),
        _TEST_MATRIX,
        tone_params,
    )
    assert rendered.max() <= tone.MAX_CODE
    white_code = int(
        encode_normalized(np.array([0.0], dtype=np.float32))[0].astype(np.uint16)
    )
    assert rendered[0, white_code, 0] > rendered[0, white_code + 1, 0]


def test_headroom_in_gamut_does_not_count_as_a_gamut_clip():
    """§2.2: recoverable headroom is no longer reported as clipping."""
    white_code = int(
        encode_normalized(np.array([0.0], dtype=np.float32))[0].astype(np.uint16)
    )
    image = np.full((2, 2, 3), white_code, dtype=np.uint16)
    _, fractions = render.render_export(
        image, _TEST_MATRIX, {"grade_r": 115.0, "snap_gamma": 0.0}
    )
    assert fractions == (0.0, 0.0, 0.0)


# --- the fill, mono, and clip fraction -------------------------------------


def test_normalized_fill_renders_to_black_through_the_full_colour_chain():
    """The fill sits above 1.0; `1 - val` is negative; the clip takes it
    to 0; every later stage maps 0 to 0 (§4.7; MONOCHROME_PLAN §3.4 tests
    the same property on the stitch side)."""
    fill_code = int(
        encode_normalized(
            np.full((1, 1, 3), normalization.NORMALIZED_FILL, dtype=np.float32)
        )[0, 0]
        .flat[0]
        .astype(np.uint16)
    )
    image = np.full((1, 1, 3), fill_code, dtype=np.uint16)
    rendered, _ = render.render_export(image, _TEST_MATRIX, _TONE_PARAM_SWEEP[1])
    assert np.all(rendered == 0)


def test_mono_renders_2d_and_matches_the_colour_path_within_the_bound():
    codes = np.arange(tone.MAX_CODE + 1, dtype=np.uint16).reshape(256, 256)
    rendered, fractions = render.render_export(codes, None, {"grade_r": 90.0, "snap_gamma": 0.2})
    assert rendered.shape == codes.shape
    assert fractions == (0.0,)

    colour, _ = render.render_export(
        np.stack([codes.ravel()] * 3, axis=-1).reshape(1, -1, 3),
        np.eye(3, dtype=np.float32),
        {"grade_r": 90.0, "snap_gamma": 0.2},
    )
    difference = np.abs(
        colour[0, :, 0].astype(np.int32) - rendered.ravel().astype(np.int32)
    )
    assert difference.max() <= 4


def test_mono_rejects_a_matrix_and_colour_requires_one():
    codes = np.zeros((4, 4), dtype=np.uint16)
    with pytest.raises(ValueError, match="mono"):
        render.render_export(codes, _TEST_MATRIX, None)
    colour = np.zeros((4, 4, 3), dtype=np.uint16)
    with pytest.raises(ValueError, match="identity"):
        render.render_export(colour, None, None)


def test_the_reported_clip_fraction_is_zero_in_gamut_and_non_zero_out():
    in_gamut = np.full((2, 2, 3), 12000, dtype=np.uint16)
    _, fractions = render.render_export(in_gamut, _TEST_MATRIX, None)
    assert fractions == (0.0, 0.0, 0.0)

    out_of_gamut = np.zeros((2, 2, 3), dtype=np.uint16)
    out_of_gamut[..., 0] = 65535
    _, fractions = render.render_export(out_of_gamut, _TEST_MATRIX, None)
    assert any(f > 0.0 for f in fractions)
    assert all(f <= 1.0 for f in fractions)


def test_render_rejects_non_uint16_codes():
    with pytest.raises(ValueError, match="uint16"):
        render.render_export(np.zeros((4, 4), dtype=np.uint8), None, None)


def test_the_gamma_constant_is_derived_from_the_pinned_trc():
    """§2.2: one gamma constant, three places. The render derives its
    value from `icc_profile.TRC_G_EXPORT`; the generator's u8Fixed8 563 is
    asserted equal in `icc_profile_test`."""
    assert _GAMMA == 563 / 256
    assert tone.MAX_CODE == 65535 == render.MAX_CODE


def test_preview_reference_matches_the_preview_module_s_luts():
    """The preview reference in this module is the same construction as
    `previews.py`'s LUT builders — pin it, so a change in either shows up
    here. `previews.NORMALIZED_DISPLAY_LUT` is the flat (tone-less) LUT."""
    assert np.array_equal(
        preview_lut(None), _previews.NORMALIZED_DISPLAY_LUT
    )


def test_preview_and_export_agree_with_matrix_and_color_within_one_8_bit_code():
    """The shared render: 8-bit preview encode and 16-bit export agree
    when a camera matrix and a colour op are both active."""
    matrix = _TEST_MATRIX
    tone_params = {"grade_r": 115.0, "snap_gamma": 0.0}
    color_params = {"wb_cyan": 0.05, "dye_separation": 1.2}
    metering = color.Metering(ranges=(1.0, 1.0, 1.0), shadow_refs_norm=None)
    codes = np.stack(
        [np.arange(tone.MAX_CODE + 1, dtype=np.uint16)] * 3, axis=-1
    ).reshape(1, -1, 3)

    rendered, _ = render.render_export(
        codes, matrix, tone_params, color_params, metering
    )
    preview_8 = render.encode_positive_uint8(
        codes, matrix, tone_params, color_params, metering
    )
    rendered_8 = np.rint(rendered / 257.0).astype(np.uint8)
    difference = np.abs(rendered_8.astype(np.int32) - preview_8.astype(np.int32))
    assert difference.max() <= 1


def test_preview_and_export_agree_with_headroom_and_shoulder():
    """EXPORT_PLAN §4.7 with source codes inside the encode headroom."""
    matrix = _TEST_MATRIX
    tone_params = {
        "grade_r": 115.0,
        "snap_gamma": 0.0,
        "shoulder": 0.5,
        "shoulder_width": 2.5,
    }
    white_code = int(
        encode_normalized(np.array([0.0], dtype=np.float32))[0].astype(np.uint16)
    )
    headroom_codes = np.arange(white_code, dtype=np.uint16)
    codes = np.stack([headroom_codes] * 3, axis=-1).reshape(1, -1, 3)

    rendered, _ = render.render_export(codes, matrix, tone_params)
    preview_8 = render.encode_positive_uint8(codes, matrix, tone_params)
    rendered_8 = np.rint(rendered / 257.0).astype(np.uint8)
    difference = np.abs(rendered_8.astype(np.int32) - preview_8.astype(np.int32))
    assert difference.max() <= 1
    assert rendered_8[0, 0, 0] < 255
    assert rendered_8[0, 0, 0] != rendered_8[0, -1, 0]
