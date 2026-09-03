"""Tests for the CA fit (docs/GEOMETRIC_PLAN.md section 8).

The direction test is the one that matters: most of this module could ship
backwards and still pass every other test. Synthesise a per-channel pure
scale, fit it, and prove the decoder scales remove the aberration rather
than doubling it (section 3.3 exists because of exactly this hazard).
"""

import numpy as np
import pytest

from scanny_boy.ca_fit import (
    CA_SCALE_ONLY_PX,
    RAW_PARAMS_HALF_SIZE,
    CAFitError,
    ChannelFit,
    channel_ca_px,
    fit_ca,
)
from scanny_boy.raw_decode import RAW_PARAMS

FRAME_WIDTH, FRAME_HEIGHT = 6048, 4024
# Pixel figures are reported in full-resolution pixels (section 4.6),
# whatever resolution the corner data was detected at.
FX = float(FRAME_WIDTH)


def _synthetic_frame(
    scale: float,
    radial: float = 0.0,
    centre: tuple[float, float] = (0.0, 0.0),
    n: int = 120,
    seed: int = 0,
):
    """One frame's three channels' normalised corners: green on a disc of
    points, red and blue where the model puts them. `radial` is the c1
    term; `centre` is the channel's decentring."""
    rng = np.random.default_rng(seed)
    angles = rng.uniform(0, 2 * np.pi, n)
    radii = np.sqrt(rng.uniform(0.02, 0.45**2, n))
    green = np.stack([radii * np.cos(angles), radii * np.sin(angles)], axis=-1)

    def channel(centre_offset):
        # The model's own construction: the channel's point sits at radius
        # r_g * s(r_g) along green's direction, measured from the channel's
        # own centre.
        c = np.asarray(centre_offset)
        r = np.hypot(green[:, 0], green[:, 1])
        s = scale + radial * r**2
        return c + green * s[:, np.newaxis]

    return channel(centre), green, channel(centre), green


def test_raw_params_half_size_derives_from_raw_params():
    """The half-size decode deviates from `RAW_PARAMS` in exactly one
    documented way; a change to `RAW_PARAMS` must flow through."""
    assert RAW_PARAMS_HALF_SIZE == {**RAW_PARAMS, "half_size": True}


def test_pure_scale_fit_recovers_the_scale():
    frames = [
        _synthetic_frame(scale=1.0004, seed=seed) for seed in range(6)
    ]
    train, heldout = frames[:4], frames[4:]
    result = fit_ca(train, heldout, FRAME_WIDTH, FRAME_HEIGHT, geometry=None)

    assert result.mode == "scale"
    assert result.red.c0 == pytest.approx(1.0004, abs=1e-6)
    assert result.blue.c0 == pytest.approx(1.0004, abs=1e-6)
    assert result.red_scale == pytest.approx(1 / 1.0004, rel=1e-6)
    assert result.blue_scale == pytest.approx(1 / 1.0004, rel=1e-6)
    assert result.accepted


def test_direction_scales_remove_the_aberration_rather_than_doubling_it():
    """The load-bearing direction test. The fit measures r_R = c0 * r_G;
    the decoder multiplies red by red_scale = 1/c0. Applied to the
    synthetic observation, the red corner must land on the green corner —
    a sign error here would double the aberration instead."""
    scale = 1.001
    red, green, _, _ = _synthetic_frame(scale=scale, seed=3)
    frames = [_synthetic_frame(scale=scale, seed=seed) for seed in range(4)]
    result = fit_ca(frames[:3], frames[3:], FRAME_WIDTH, FRAME_HEIGHT, None)

    corrected = red * result.red_scale  # what the decoder does to red
    misregistration = np.hypot(*(corrected - green).T) * FX
    assert np.max(misregistration) < 0.05

    # And the wrong direction would double it — pin the contrast so the
    # test cannot rot into a tautology.
    wrong = red * result.red.c0
    assert np.max(np.hypot(*(wrong - green).T) * FX) > np.max(misregistration) * 10


def test_radial_term_above_the_threshold_selects_maps_mode():
    frames = [
        _synthetic_frame(scale=1.0004, radial=0.02, seed=seed) for seed in range(6)
    ]
    result = fit_ca(frames[:4], frames[4:], FRAME_WIDTH, FRAME_HEIGHT, None)
    assert result.mode == "maps"
    assert result.red_scale is None and result.blue_scale is None
    assert result.radial_term_px_at_corner["red"] >= CA_SCALE_ONLY_PX


def test_radial_term_below_the_threshold_selects_scale_mode():
    frames = [
        _synthetic_frame(scale=1.0004, radial=0.00001, seed=seed) for seed in range(6)
    ]
    result = fit_ca(frames[:4], frames[4:], FRAME_WIDTH, FRAME_HEIGHT, None)
    assert result.mode == "scale"
    assert result.radial_term_px_at_corner["red"] < CA_SCALE_ONLY_PX


def test_decentring_is_fitted_not_assumed():
    """A decentred channel must recover its own centre; an assumed centre
    would show up as a spurious tangential component."""
    centre = (0.002, -0.003)
    frames = [
        _synthetic_frame(scale=1.0004, centre=centre, seed=seed) for seed in range(6)
    ]
    result = fit_ca(frames[:4], frames[4:], FRAME_WIDTH, FRAME_HEIGHT, None)
    assert result.red.center_x == pytest.approx(centre[0], abs=2e-4)
    assert result.red.center_y == pytest.approx(centre[1], abs=2e-4)


def test_maps_mode_residual_stays_below_the_accept_gate():
    frames = [
        _synthetic_frame(scale=1.0004, radial=0.02, seed=seed) for seed in range(6)
    ]
    result = fit_ca(frames[:4], frames[4:], FRAME_WIDTH, FRAME_HEIGHT, None)
    assert result.accepted
    assert result.misregistration_after_px["red"] < 0.3
    assert (
        result.misregistration_after_px["red"] < result.misregistration_before_px["red"]
    )


def test_no_improvement_is_rejected():
    """A fit that does not measurably help is dropped rather than carried
    (section 4.5's discipline, applied to CA)."""
    # No CA at all: before is ~0, so the improvement gate cannot clear.
    frames = [
        _synthetic_frame(scale=1.0, seed=seed) for seed in range(6)
    ]
    result = fit_ca(frames[:4], frames[4:], FRAME_WIDTH, FRAME_HEIGHT, None)
    assert not result.accepted
    assert result.rejection_reason is not None


def test_half_size_and_full_size_fits_agree():
    """Normalised coordinates make the resolution irrelevant (section 0.7):
    the same correspondences expressed at half size and full size must fit
    to the same coefficients."""
    frames = [
        _synthetic_frame(scale=1.0004, radial=0.02, seed=seed) for seed in range(6)
    ]
    half = fit_ca(frames[:4], frames[4:], FRAME_WIDTH, FRAME_HEIGHT, None)
    full = fit_ca(frames[:4], frames[4:], FRAME_WIDTH * 2, FRAME_HEIGHT * 2, None)
    assert half.red.c0 == pytest.approx(full.red.c0, abs=1e-9)
    assert half.red.c1 == pytest.approx(full.red.c1, abs=1e-9)
    # The coefficients are resolution-independent; the pixel figures are
    # reported at the fx the caller names, so they scale with it exactly.
    assert half.radial_term_px_at_corner["red"] == pytest.approx(
        full.radial_term_px_at_corner["red"] / 2, rel=1e-6
    )


def test_no_surviving_frames_raises():
    with pytest.raises(CAFitError):
        fit_ca([], [], FRAME_WIDTH, FRAME_HEIGHT, None)


def test_channel_ca_px_reports_the_pooled_displacement():
    green = [np.array([[0.1, 0.1], [-0.2, 0.0]])]
    red = [np.array([[0.1001, 0.1], [-0.2002, 0.0]])]
    # 0.0001 and 0.0002 normalised displacement -> * FX pixels.
    expected = np.mean([0.0001, 0.0002]) * FX
    assert channel_ca_px(green, red, FX) == pytest.approx(expected)


def test_channelfit_forward_and_inverse_round_trip():
    fit = ChannelFit(c0=1.0004, c1=0.01, c2=0.0, center_x=0.001, center_y=-0.002)
    points = np.array([[0.3, 0.2], [-0.4, 0.1], [0.0, 0.0]])
    recovered = fit.inverse(fit.forward(points))
    assert np.allclose(recovered, points, atol=1e-9)
