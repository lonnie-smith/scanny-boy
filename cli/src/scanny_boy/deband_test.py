"""Algorithm-level tests for `deband.py` (DEBAND_PLAN.md §6.1).

All synthetic — a copy of `synthetic_scene` with development bands injected
by `deband_support.make_banded_scene`. Edits/pipeline/Swift-facing behaviour
(the `deband` op in `library/repo.py`, `previews.py`/`exporter.py` replay,
`edit deband`, re-stitch refit) is out of scope for this module and is
tested where that code lands (DEBAND_PLAN.md §8 step 2+).
"""

from __future__ import annotations

import numpy as np
import pytest

from scanny_boy import deband, normalization
from scanny_boy import deband_support as ds

H, W = 760, 1800
SPANS = ds.DEFAULT_SPANS
FULL_WINDOW = (0.0, 0.0, float(W), float(H), 0.0)


def _decode(codes: np.ndarray) -> np.ndarray:
    return normalization.decode_normalized(codes).astype(np.float32)


def _bg_chroma(codes: np.ndarray, rows=slice(None), cols=slice(None)) -> np.ndarray:
    val = _decode(codes)[rows][:, cols] if isinstance(rows, slice) else _decode(codes)
    return val[..., 2] - val[..., 1]


# --- removal / gradient -----------------------------------------------------


def test_removal_reduces_banding_below_bound():
    codes, _info = ds.make_banded_scene(H, W, seed=1)
    before = ds.banding_score(codes, SPANS)

    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    healed = deband.apply(codes, params)
    after = ds.banding_score(healed, SPANS)

    assert before > 10.0  # sanity: the injected bands are actually visible
    assert after < 8.0  # near the ~3-5 grain noise floor measured in §1/§8
    assert after < 0.5 * before


def test_gradient_is_preserved():
    codes, _info = ds.make_banded_scene(
        H, W, seed=1, sharp_amplitude=0, broad_amplitude=0
    )
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    healed = deband.apply(codes, params)

    before_profile = _decode(codes).mean(axis=(0, 2))
    after_profile = _decode(healed).mean(axis=(0, 2))
    tenth = W // 10
    before_rise = before_profile[-tenth:].mean() - before_profile[:tenth].mean()
    after_rise = after_profile[-tenth:].mean() - after_profile[:tenth].mean()
    assert after_rise == pytest.approx(before_rise, abs=0.002)


# --- do no harm next to large smooth objects --------------------------------

TALL_H, TALL_W = 2304, 1500
TALL_WINDOW = (0.0, 0.0, float(TALL_W), float(TALL_H), 0.0)
# A cloud (own chroma, no luminance texture) over several blocks, and a tree
# canopy hanging from the top over a different column range.
CLOUD = (1000, 1450, 480, 900, 0.04)
CANOPY = (600, 600, 1000)


def _tall_fit(codes):
    fit = deband.fit_region(codes, SPANS, TALL_WINDOW, "vertical")
    params = deband.deband_params((TALL_W, TALL_H), "vertical", [fit], True, 1.0)
    return fit, deband.apply(codes, params)


def test_cloud_and_canopy_without_bands_are_left_alone():
    codes, _info = ds.make_banded_scene(
        TALL_H,
        TALL_W,
        seed=11,
        flat=True,
        sharp_amplitude=0,
        broad_amplitude=0,
        cloud=CLOUD,
        canopy=CANOPY,
    )
    fit, healed = _tall_fit(codes)

    spans = np.asarray(SPANS, dtype=np.float32)
    assert np.abs(fit.corr * spans).max() < 0.007  # grain level, log10
    change = np.abs(_decode(healed) - _decode(codes)) * spans
    assert change.max() < 0.007
    # No streak: the chroma column profile through the cloud rows is unmoved.
    rows = slice(CLOUD[0], CLOUD[1])
    before = _bg_chroma(codes, rows=rows).mean(axis=0) * SPANS[2]
    after = _bg_chroma(healed, rows=rows).mean(axis=0) * SPANS[2]
    assert np.abs(after - before).max() < 0.007


def test_bands_are_still_removed_beside_a_cloud_and_canopy():
    codes, _info = ds.make_banded_scene(
        TALL_H, TALL_W, seed=11, flat=True, cloud=CLOUD, canopy=CANOPY
    )
    _fit, healed = _tall_fit(codes)

    # Judge on the columns left of the cloud (the sharp band edge is there).
    clear = slice(0, CLOUD[2] - 60)
    before = ds.banding_score(codes, SPANS, cols=clear)
    after = ds.banding_score(healed, SPANS, cols=clear)
    assert before > 10.0
    assert after < 0.5 * before
    # And the cloud itself keeps its own chroma (not "corrected" into the
    # band): the cloud-vs-sky chroma contrast survives to within a third.
    contrast_before = (
        _bg_chroma(codes)[700:900, 600:800].mean()
        - _bg_chroma(codes)[100:300, 600:800].mean()
    )
    contrast_after = (
        _bg_chroma(healed)[700:900, 600:800].mean()
        - _bg_chroma(healed)[100:300, 600:800].mean()
    )
    assert contrast_after == pytest.approx(contrast_before, rel=0.33)


# --- locality / protection --------------------------------------------------


def test_locality_outside_feathered_window_is_byte_identical():
    codes, _info = ds.make_banded_scene(H, W, seed=2)
    window = (200.0, 100.0, 900.0, 560.0, 0.0)
    fit = deband.fit_region(codes, SPANS, window, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    healed = deband.apply(codes, params)

    x, y, w, h, _tilt = window
    x, y, w, h = int(x), int(y), int(w), int(h)
    feather_w = min(deband.FEATHER_PX, deband.FEATHER_FRACTION * w)
    feather_h = min(deband.FEATHER_PX, deband.FEATHER_FRACTION * h)
    x0, y0 = int(x - feather_w) - 2, int(y - feather_h) - 2
    x1, y1 = int(x + w + feather_w) + 2, int(y + h + feather_h) + 2

    outside = np.ones((H, W), dtype=bool)
    outside[max(0, y0) : min(H, y1), max(0, x0) : min(W, x1)] = False
    assert np.array_equal(codes[outside], healed[outside])
    # sanity: something inside the window did change
    assert not np.array_equal(codes[y : y + h, x : x + w], healed[y : y + h, x : x + w])


def test_pole_chroma_is_protected():
    pole_x = int(W * 0.6)
    codes, info = ds.make_banded_scene(H, W, seed=3, pole_x=pole_x)
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    healed = deband.apply(codes, params)

    lo, hi = info["pole"]
    before = _bg_chroma(codes, cols=slice(lo, hi)).mean()
    after = _bg_chroma(healed, cols=slice(lo, hi)).mean()
    assert abs(after - before) < 0.05 * abs(before)


def test_high_contrast_pole_is_left_untouched():
    # The protection weight alone (not just the fit's background mask) keeps
    # a strongly contrasting object's own colour: a pole whose luminance
    # stands several tolerances proud of its surroundings is not corrected,
    # while the background right beside it is.
    pole_x = int(W * 0.7)  # on the shoulder of the broad band
    codes, info = ds.make_banded_scene(H, W, seed=3, pole_x=pole_x, pole_contrast=3.0)
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    healed = deband.apply(codes, params)

    lo, hi = info["pole"]
    delta = np.abs(_decode(healed) - _decode(codes))
    # Medians: the synthetic scene's own brightness blobs can sit beside the
    # pole and locally lower its contrast, so a max would test the blobs.
    pole_change = np.median(delta[:, lo + 2 : hi - 2])
    near_change = np.median(delta[:, hi + 20 : hi + 60])
    assert near_change > 0.001  # the background beside it really was corrected
    assert pole_change < 0.1 * near_change


def test_wire_is_unchanged():
    wire_y = H // 2
    codes, info = ds.make_banded_scene(H, W, seed=4, wire_y=wire_y)
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    healed = deband.apply(codes, params)

    lo, hi = info["wire"]
    before = _decode(codes)[lo:hi].copy()
    after = _decode(healed)[lo:hi].copy()
    assert np.abs(after - before).max() < 0.002


def test_luminance_is_preserved():
    codes, _info = ds.make_banded_scene(H, W, seed=5)
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    healed = deband.apply(codes, params)

    before_lum = _decode(codes).mean()
    after_lum = _decode(healed).mean()
    # "quantization" here is the uint16 encode step; a generous multiple of
    # it comfortably separates a real luminance shift from round-off.
    quantum = 1.0 / 65535.0
    assert abs(after_lum - before_lum) < 20 * quantum


def test_fill_code_pixels_are_skipped():
    codes, _info = ds.make_banded_scene(H, W, seed=4)
    codes[300:340, 700:900] = deband.FILL_CODE
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    healed = deband.apply(codes, params)

    assert np.array_equal(healed[300:340, 700:900], codes[300:340, 700:900])
    assert not np.array_equal(healed[:300], codes[:300])


@pytest.mark.parametrize("strength", [0.0, 0.5])
def test_strength_scales_the_correction(strength):
    codes, _info = ds.make_banded_scene(H, W, seed=4)
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    full = deband.apply(
        codes, deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    )
    scaled = deband.apply(
        codes, deband.deband_params((W, H), "vertical", [fit], True, strength)
    )
    if strength == 0.0:
        assert np.array_equal(scaled, codes)
        return
    full_delta = _decode(full) - _decode(codes)
    scaled_delta = _decode(scaled) - _decode(codes)
    interior = (slice(150, H - 150), slice(150, W - 150))
    ratio = scaled_delta[interior].std() / full_delta[interior].std()
    assert ratio == pytest.approx(strength, abs=0.03)


def test_tilted_window_stays_local_and_changes_inside():
    codes, _info = ds.make_banded_scene(H, W, seed=2)
    window = (300.0, 150.0, 1000.0, 480.0, 6.0)
    fit = deband.fit_region(codes, SPANS, window, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    healed = deband.apply(codes, params)

    changed = np.any(healed != codes, axis=-1)
    ys, xs = np.nonzero(changed)
    assert changed.sum() > 10_000
    # Bounding box of the 6 degree-tilted 1000x480 rect about its centre.
    cx, cy = 300 + 499.5, 150 + 239.5
    half_w = (1000 * np.cos(np.radians(6)) + 480 * np.sin(np.radians(6))) / 2
    half_h = (1000 * np.sin(np.radians(6)) + 480 * np.cos(np.radians(6))) / 2
    assert xs.min() >= cx - half_w - 2 and xs.max() <= cx + half_w + 2
    assert ys.min() >= cy - half_h - 2 and ys.max() <= cy + half_h + 2


def test_withheld_block_is_filled_from_its_neighbours():
    codes, _info = ds.make_banded_scene(H, W, seed=2)
    mask = np.ones(H, dtype=bool)
    mask[256:512] = False  # the middle block has no usable lines
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical", row_mask=mask)
    full = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    assert fit.blocks == 3
    assert np.abs(fit.corr).max() < 0.05  # no zero-profile blow-up
    np.testing.assert_allclose(fit.corr[1], full.corr[1], atol=0.003)
    with pytest.raises(ValueError, match="selects no lines"):
        deband.fit_region(
            codes, SPANS, FULL_WINDOW, "vertical", row_mask=np.zeros(H, dtype=bool)
        )


# --- axis --------------------------------------------------------------


def test_horizontal_axis_matches_vertical_behaviour():
    codes_v, _info = ds.make_banded_scene(H, W, seed=6)
    codes_h = np.transpose(codes_v, (1, 0, 2)).copy()
    window_h = (0.0, 0.0, float(H), float(W), 0.0)

    fit_h = deband.fit_region(codes_h, SPANS, window_h, "horizontal")
    params_h = deband.deband_params((H, W), "horizontal", [fit_h], True, 1.0)
    healed_h = deband.apply(codes_h, params_h)

    before_score = ds.banding_score(codes_v, SPANS)
    after_score = ds.banding_score(np.transpose(healed_h, (1, 0, 2)), SPANS)
    assert after_score < 0.5 * before_score


# --- ordering ------------------------------------------------------------


def test_overlapping_regions_apply_deterministically():
    codes, _info = ds.make_banded_scene(H, W, seed=7)
    window_a = (0.0, 0.0, float(W), float(H // 2) + 150, 0.0)
    window_b = (0.0, float(H // 2) - 150, float(W), float(H // 2) + 150, 0.0)

    fit_a = deband.fit_region(codes, SPANS, window_a, "vertical")
    fit_b = deband.fit_region(codes, SPANS, window_b, "vertical", prior_regions=[fit_a])
    params = deband.deband_params((W, H), "vertical", [fit_a, fit_b], True, 1.0)

    healed_1 = deband.apply(codes, params)
    healed_2 = deband.apply(codes, params)
    assert np.array_equal(healed_1, healed_2)


def test_removing_first_region_refits_the_second_differently():
    codes, _info = ds.make_banded_scene(H, W, seed=7)
    window_a = (0.0, 0.0, float(W), float(H // 2) + 150, 0.0)
    window_b = (0.0, float(H // 2) - 150, float(W), float(H // 2) + 150, 0.0)

    fit_a = deband.fit_region(codes, SPANS, window_a, "vertical")
    fit_b_with_a = deband.fit_region(
        codes, SPANS, window_b, "vertical", prior_regions=[fit_a]
    )
    fit_b_alone = deband.fit_region(codes, SPANS, window_b, "vertical")

    assert not np.allclose(fit_b_with_a.corr, fit_b_alone.corr)


# --- region render ---------------------------------------------------------


@pytest.mark.parametrize(
    ("axis", "tilt_deg"), [("vertical", 0.0), ("vertical", 4.0), ("horizontal", 4.0)]
)
def test_region_render_matches_full_apply_slice(axis, tilt_deg):
    codes, _info = ds.make_banded_scene(H, W, seed=8)
    if axis == "horizontal":
        codes = np.ascontiguousarray(np.transpose(codes, (1, 0, 2)))
    height, width = codes.shape[:2]
    window = (80.0, 60.0, float(width - 160), float(height - 120), tilt_deg)
    fit = deband.fit_region(codes, SPANS, window, axis)
    params = deband.deband_params((width, height), axis, [fit], True, 1.0)

    margin = deband.REGION_MARGIN
    x0, y0, w0, h0 = 250, 150, 400, 300
    bx0, by0 = max(0, x0 - margin), max(0, y0 - margin)
    bx1, by1 = min(width, x0 + w0 + margin), min(height, y0 + h0 + margin)
    padded = codes[by0:by1, bx0:bx1]
    region_local = (x0 - bx0, y0 - by0, w0, h0)

    region_out = deband.apply(padded, params, region=region_local, origin=(bx0, by0))
    full_out = deband.apply(codes, params)
    full_slice = full_out[y0 : y0 + h0, x0 : x0 + w0]
    assert np.array_equal(region_out, full_slice)


# --- params ------------------------------------------------------------


def test_params_round_trip():
    codes, _info = ds.make_banded_scene(H, W, seed=9)
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)

    # Decoding the serialized table reproduces the fitted correction to
    # within the int16/1e-5 quantization `_encode_table` uses.
    window, block_px, pitch_px, blocks, corr = deband._parse_region_dict(
        params["regions"][0], "vertical"
    )
    assert window == fit.window
    assert block_px == fit.block_px
    assert pitch_px == fit.pitch_px
    assert blocks == fit.blocks
    np.testing.assert_allclose(corr, fit.corr, atol=2 * deband.TABLE_SCALE)

    # And applying through the serialized params matches direct use of the
    # dataclass's own array.
    healed_via_params = deband.apply(codes, params)
    assert healed_via_params.shape == codes.shape
    assert not np.array_equal(healed_via_params, codes)


def test_malformed_region_raises():
    codes, _info = ds.make_banded_scene(H, W, seed=9)
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    params["regions"][0]["corr"] = "not valid base64 !!"
    with pytest.raises(ValueError):
        deband.apply(codes, params)


def test_malformed_region_missing_key_raises():
    codes, _info = ds.make_banded_scene(H, W, seed=9)
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    del params["regions"][0]["window"]
    with pytest.raises(ValueError):
        deband.apply(codes, params)


def test_newer_fit_version_is_not_live():
    codes, _info = ds.make_banded_scene(H, W, seed=9)
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    params["fit_version"] = deband.FIT_VERSION + 1
    assert deband.is_live(params, (H, W)) is False
    # And, being a whole-image apply with no `region`, it is a no-op.
    assert np.array_equal(deband.apply(codes, params), codes)


def test_canvas_mismatch_is_not_live():
    codes, _info = ds.make_banded_scene(H, W, seed=9)
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    params["canvas"] = [W + 10, H]
    assert deband.is_live(params, (H, W)) is False


def test_2d_image_is_a_noop():
    codes, _info = ds.make_banded_scene(H, W, seed=9)
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], True, 1.0)
    mono = codes[..., 0]
    assert np.array_equal(deband.apply(mono, params), mono)


def test_disabled_or_empty_is_not_live():
    codes, _info = ds.make_banded_scene(H, W, seed=9)
    fit = deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")
    params = deband.deband_params((W, H), "vertical", [fit], False, 1.0)
    assert deband.is_live(params, (H, W)) is False
    empty_params = deband.deband_params((W, H), "vertical", [], True, 1.0)
    assert deband.is_live(empty_params, (H, W)) is False


# --- fit_region failure modes ------------------------------------------


def test_region_too_small_along_band_raises():
    codes, _info = ds.make_banded_scene(H, W, seed=9)
    with pytest.raises(ValueError, match="band axis"):
        deband.fit_region(codes, SPANS, (0.0, 0.0, float(W), 200.0, 0.0), "vertical")


def test_region_too_small_across_band_raises():
    codes, _info = ds.make_banded_scene(H, W, seed=9)
    with pytest.raises(ValueError, match="across"):
        deband.fit_region(codes, SPANS, (0.0, 0.0, 400.0, float(H), 0.0), "vertical")


def test_region_with_too_little_background_raises():
    rng = np.random.default_rng(0)
    val = 0.5 + rng.normal(0, 0.25, (H, W, 3)).astype(np.float32)
    val = np.clip(
        val,
        -normalization.NORMALIZED_HEADROOM_LOW,
        1.0 + normalization.NORMALIZED_HEADROOM_HIGH,
    )
    codes = normalization.encode_normalized(val).astype(np.uint16)
    with pytest.raises(ValueError, match="background"):
        deband.fit_region(codes, SPANS, FULL_WINDOW, "vertical")


def test_non_3_channel_image_raises():
    codes, _info = ds.make_banded_scene(H, W, seed=9)
    mono = codes[..., 0]
    with pytest.raises(ValueError, match="3-channel"):
        deband.fit_region(mono, SPANS, FULL_WINDOW, "vertical")
