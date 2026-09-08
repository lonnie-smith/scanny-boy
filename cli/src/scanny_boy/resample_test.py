"""Tests for `resample`: the true Lanczos3 kernel the export's linear
stage resamples with — sizes, normalization, linear-ramp and constant
reproduction, banding, and the no-upscale contract."""

from __future__ import annotations

import numpy as np
import pytest

from scanny_boy import resample

# --- target_size ------------------------------------------------------------


def test_target_size_reduces_the_long_edge_and_preserves_aspect():
    assert resample.target_size(8064, 12096, 6048) == (4032, 6048)
    assert resample.target_size(12096, 8064, 6048) == (6048, 4032)
    # A 3:2 source at 3/4 scale: the short edge rounds, never hits zero.
    assert resample.target_size(800, 1200, 900) == (600, 900)


def test_target_size_refuses_to_upscale():
    assert resample.target_size(3, 4, 4) is None
    assert resample.target_size(3, 4, 6048) is None


def test_target_size_without_a_target_is_a_noop():
    assert resample.target_size(3, 4, None) is None


def test_target_size_never_returns_zero():
    # A 1000:1 aspect at a small target: the short edge clamps to 1.
    assert resample.target_size(10, 1000, 5) == (1, 5)


# --- the kernel ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("src", "dst"),
    [(4, 2), (8, 3), (3, 2), (7, 7), (8064, 6048), (12096, 6048)],
)
def test_weights_are_normalized_and_positions_in_range(src, dst):
    positions, weights = resample._weights(src, dst)
    assert weights.shape == positions.shape
    np.testing.assert_allclose(
        weights.sum(axis=1), 1.0, rtol=0, atol=1e-6
    )
    assert positions.min() >= 0 and positions.max() <= src - 1


def test_kernel_is_the_lanczos3_sinc_product():
    x = np.linspace(-2.9, 2.9, 41)
    expected = resample._sinc(x) * resample._sinc(x / 3.0)
    np.testing.assert_allclose(resample._lanczos3(x), expected)
    # Zero outside the support, exactly — not a tail.
    assert resample._lanczos3(np.array([3.0, 4.0, -5.0])).tolist() == [0.0, 0.0, 0.0]


def test_kernel_downscale_widens_the_window():
    """At 1:2 the kernel argument is scaled by 0.5, so one output sample
    reads 13 source samples (radius 6), not the 7 a fixed window gives."""
    positions, _ = resample._weights(8, 4)
    assert positions.shape[1] == 13
    positions_full, _ = resample._weights(8, 8)
    assert positions_full.shape[1] == 7


def test_constant_field_survives_any_scale_exactly():
    """Normalized weights: flat in, flat out — including across the band
    boundaries the two passes step through (height exceeds `_BAND`)."""
    image = np.full((600, 400, 3), 0.25, dtype=np.float32)
    out = resample.resize_lanczos3(image, 300, 200)
    assert out.shape == (300, 200, 3)
    np.testing.assert_allclose(out, 0.25, rtol=0, atol=1e-6)


def test_a_linear_ramp_is_reproduced_in_the_interior():
    """Lanczos3 reproduces linear polynomials; on a ramp the interior
    samples should return their source values to float precision (the
    clamped borders may stray slightly — that is edge handling, not the
    kernel)."""
    ramp = np.tile(np.linspace(0.0, 1.0, 600, dtype=np.float32), (4, 1))
    out = resample.resize_lanczos3(ramp, 4, 300)
    # The output sample centers land on (j + 0.5) * 2 - 0.5 in source
    # coordinates, and the ramp reads i / 599 there: an exact linear
    # function of j.
    expected = (((np.arange(300, dtype=np.float64) + 0.5) * 2 - 0.5) / 599.0)[
        None, :
    ].repeat(4, axis=0)
    np.testing.assert_allclose(out[:, 4:-4], expected[:, 4:-4], rtol=0, atol=1e-4)


def test_a_step_resizes_to_the_linear_midpoint_at_a_centered_boundary():
    """Half black, half white at a 3/4 scale whose boundary lands exactly
    on an output sample center: that sample reads the linear midpoint
    0.5 — the behaviour a display-encoded resample would NOT have. The
    clamped edge columns ring, as Lanczos does; the render clips that."""
    image = np.zeros((4, 4), dtype=np.float32)
    image[:, 2:] = 1.0
    out = resample.resize_lanczos3(image, 4, 3)
    np.testing.assert_allclose(out[:, 1], 0.5, rtol=0, atol=1e-6)

    vertical = np.zeros((4, 4), dtype=np.float32)
    vertical[2:, :] = 1.0
    out_v = resample.resize_lanczos3(vertical, 3, 4)
    np.testing.assert_allclose(out_v[1, :], 0.5, rtol=0, atol=1e-6)


def test_colour_channels_resample_independently():
    """Each channel is its own plane: a per-channel ramp must survive
    exactly as the 2-D resize of that channel."""
    image = np.zeros((6, 9, 3), dtype=np.float32)
    image[:, :, 0] = np.linspace(0.0, 1.0, 54).reshape(6, 9)
    image[:, :, 1] = 0.5
    image[:, :, 2] = 0.125

    out = resample.resize_lanczos3(image, 4, 6)

    assert out.shape == (4, 6, 3)
    np.testing.assert_allclose(
        out[:, :, 1], 0.5, rtol=0, atol=1e-6
    )
    np.testing.assert_allclose(out[:, :, 2], 0.125, rtol=0, atol=1e-6)
    np.testing.assert_allclose(
        out[:, :, 0],
        resample.resize_lanczos3(image[:, :, 0], 4, 6),
        rtol=0,
        atol=1e-6,
    )


def test_uint16_input_is_resampled_in_float_and_stays_16bit_fidelity():
    """The render hands float32; uint16 also works (the kernel resamples
    the given values), with float32 precision throughout — the centered
    boundary reads 0.5 * 65535, not the 32767 an area average or a
    display-encoded resample would give."""
    image = np.zeros((4, 4), dtype=np.uint16)
    image[:, 2:] = 65535
    out = resample.resize_lanczos3(image, 4, 3)
    assert out.dtype == np.float32
    np.testing.assert_allclose(out[:, 1], 32767.5, rtol=0, atol=0.5)


def test_resizing_to_a_larger_size_is_refused():
    """The export path never asks for this (`target_size`), but a direct
    caller gets a loud error, not a silently wrong upscale plan."""
    with pytest.raises(ValueError):
        resample.resize_lanczos3(np.zeros((4, 8), dtype=np.float32), 8, 8)
    with pytest.raises(ValueError):
        resample.resize_lanczos3(np.zeros((4, 8, 3), dtype=np.float32), 4, 9)


def test_identity_size_returns_a_copy():
    image = np.zeros((3, 4), dtype=np.float32)
    out = resample.resize_lanczos3(image, 3, 4)
    np.testing.assert_array_equal(out, image)
    out[0, 0] = 9.0
    assert image[0, 0] == 0.0


def test_2d_input_stays_2d():
    image = np.zeros((4, 8), dtype=np.float32)
    out = resample.resize_lanczos3(image, 2, 4)
    assert out.shape == (2, 4)
    assert out.ndim == 2
