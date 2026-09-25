"""Tests for `auto_crop`: the format ratio table, the picture-only window
detector, the display-image builder, and the carrier hint."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from scanny_boy import auto_crop, previews
from scanny_boy.auto_crop import (
    FORMAT_RATIOS,
    AutoCrop,
    Refusal,
    analysis_display,
    estimate_crop,
    exclusion_hint,
)
from scanny_boy.auto_rotate import REBATE_SLACK, picture_mask, rotate_with_fill
from scanny_boy.library import repo
from scanny_boy.normalization import (
    NORMALIZED_FILL,
    decode_normalized,
    encode_normalized,
)
from scanny_boy.roll_folder import FORMAT_CHOICES

_REBATE = 1.02
_PICTURE = 0.5
_CARRIER = -0.1


def _scene(
    height: int = 600,
    width: int = 1000,
    *,
    picture: tuple[int, int, int, int] | None = (150, 120, 700, 360),
    rebate: tuple[int, int, int, int] | None = (50, 50, 900, 500),
    fill_outside: bool = True,
) -> np.ndarray:
    """A normalized-density scene: fill, then a rebate rect, then a picture
    rect (each `(x, y, w, h)`)."""
    normalized = np.full(
        (height, width, 3), NORMALIZED_FILL if fill_outside else _REBATE, np.float32
    )
    if rebate is not None:
        x, y, w, h = rebate
        normalized[y : y + h, x : x + w] = _REBATE
    if picture is not None:
        x, y, w, h = picture
        normalized[y : y + h, x : x + w] = _PICTURE
    return normalized


def _encode(normalized: np.ndarray) -> np.ndarray:
    return encode_normalized(normalized)


def _crop(normalized: np.ndarray, format_name: str | None = "35mm", **kwargs):
    ratio = None if format_name is None else FORMAT_RATIOS[format_name]
    height, width = normalized.shape[:2]
    return estimate_crop(
        _encode(normalized), full_size=(height, width), ratio=ratio, **kwargs
    )


def _inside(rect, bounds) -> bool:
    x, y, w, h = rect
    bx, by, bw, bh = bounds
    return x >= bx and y >= by and x + w <= bx + bw and y + h <= by + bh


# --- AC-1: the ratio table ---------------------------------------------------

# The literal table from CONTRACT.md's crop section, hard-coded so a change to
# either side fails loudly (Swift's `CropSessionTests` holds the other copy).
_CONTRACT_RATIOS = {
    "half-frame": 18 / 24,
    "35mm": 36 / 24,
    "6x3": 56 / 28,
    "645": 56 / 41.5,
    "6x6": 1.0,
    "6x7": 56 / 69.5,
    "xpan": 65 / 24,
    "6x9": 56 / 84,
    "6x12": 112 / 56,
    "6x17": 168 / 56,
}


def test_format_ratios_match_the_contract_table():
    assert FORMAT_RATIOS == pytest.approx(_CONTRACT_RATIOS)


def test_format_ratios_cover_every_format_set_setup_accepts():
    assert set(FORMAT_RATIOS) == set(FORMAT_CHOICES)


# --- AC-2: the detector ------------------------------------------------------


@pytest.mark.parametrize("format_name", ["35mm", "645", "6x9", "half-frame"])
def test_window_lies_inside_the_picture_at_the_exact_ratio_and_centred(format_name):
    picture = (150, 120, 700, 360)

    result = _crop(_scene(picture=picture), format_name)

    assert isinstance(result, AutoCrop)
    x, y, w, h = result.rect
    assert _inside(result.rect, picture)
    landscape = max(FORMAT_RATIOS[format_name], 1 / FORMAT_RATIOS[format_name])
    assert w / h == pytest.approx(landscape, abs=1.5 / h)
    assert x + w / 2 == pytest.approx(picture[0] + picture[2] / 2, abs=2)
    assert y + h / 2 == pytest.approx(picture[1] + picture[3] / 2, abs=2)


def test_window_is_the_largest_that_fits():
    picture = (150, 120, 700, 360)

    result = _crop(_scene(picture=picture), "35mm")

    _x, _y, w, h = result.rect
    # 700 x 360 is wider than 3:2, so height is the limit: the window is
    # the picture's height less the safety margin on both sides.
    assert h >= 360 - 2 * (0.005 * 360 + 3)
    assert w == pytest.approx(h * 1.5, abs=1.5)


def test_a_picture_wider_than_the_ratio_fills_its_height_and_is_centred():
    picture = (150, 150, 700, 300)  # 7:3, wider than 3:2

    result = _crop(_scene(picture=picture, rebate=(100, 100, 800, 400)), "35mm")

    x, y, w, h = result.rect
    assert h >= 300 - 2 * (0.005 * 300 + 3)
    assert w / h == pytest.approx(1.5, abs=0.02)
    assert x + w / 2 == pytest.approx(picture[0] + picture[2] / 2, abs=2)
    assert y + h / 2 == pytest.approx(picture[1] + picture[3] / 2, abs=2)


def test_portrait_picture_orients_the_ratio_to_its_long_axis():
    picture = (300, 50, 300, 500)

    result = _crop(_scene(picture=picture, rebate=(250, 25, 400, 550)), "645")

    _x, _y, w, h = result.rect
    assert h > w
    assert _inside(result.rect, picture)
    assert h / w == pytest.approx(FORMAT_RATIOS["645"], abs=0.02)


def test_a_panoramic_format_orients_to_a_wide_picture():
    picture = (100, 250, 800, 220)

    result = _crop(_scene(picture=picture, rebate=(50, 200, 900, 320)), "xpan")

    assert isinstance(result, AutoCrop)
    _x, _y, w, h = result.rect
    assert w / h == pytest.approx(FORMAT_RATIOS["xpan"], abs=0.03)


def test_window_avoids_the_fill_wedges_of_a_tilted_edge_to_edge_picture():
    """No rebate band: the picture runs to the canvas edges, so the window
    is the largest that stays out of the wedges a rotation leaves."""
    height, width = 600, 1000
    ramp = np.linspace(0.2, 0.7, width, dtype=np.float32)
    normalized = np.broadcast_to(ramp[None, :, None], (height, width, 3)).copy()
    tilted = rotate_with_fill(_encode(normalized), 3.0)

    result = estimate_crop(
        tilted, full_size=(height, width), ratio=FORMAT_RATIOS["35mm"]
    )

    assert isinstance(result, AutoCrop)
    x, y, w, h = result.rect
    fill = np.all(decode_normalized(tilted) >= NORMALIZED_FILL - 1e-6, axis=-1)
    assert not fill[y : y + h, x : x + w].any()
    assert w * h > 0.4 * height * width  # substantial, not degenerate


def test_rounded_gate_corners_are_avoided():
    normalized = _scene(picture=None)
    left, top, right, bottom, radius = 150, 120, 849, 479, 60
    rounded = np.zeros(normalized.shape[:2], np.uint8)
    cv2.rectangle(rounded, (left + radius, top), (right - radius, bottom), 1, -1)
    cv2.rectangle(rounded, (left, top + radius), (right, bottom - radius), 1, -1)
    for cx, cy in (
        (left + radius, top + radius),
        (right - radius, top + radius),
        (left + radius, bottom - radius),
        (right - radius, bottom - radius),
    ):
        cv2.circle(rounded, (cx, cy), radius, 1, -1)
    normalized[rounded.astype(bool)] = _PICTURE

    result = _crop(normalized, "35mm")

    assert isinstance(result, AutoCrop)
    x, y, w, h = result.rect
    assert rounded[y : y + h, x : x + w].all()
    assert result.fill_fraction >= auto_crop.AUTO_CROP_MIN_FILL_FRACTION


def test_a_dust_speck_inside_the_picture_does_not_shrink_the_window():
    clean = _scene()
    dusty = _scene()
    dusty[290:294, 490:494] = _REBATE  # a thin-valued speck mid-picture

    assert _crop(dusty).rect == _crop(clean).rect


def test_unconstrained_fit_returns_the_maximal_rect_of_an_l_shaped_mask():
    normalized = _scene(picture=None)
    normalized[100:400, 100:900] = _PICTURE  # the wide arm: 800 x 300
    normalized[100:500, 100:400] = _PICTURE  # the tall arm: 300 x 400

    result = _crop(normalized, None)

    assert isinstance(result, AutoCrop)
    _x, _y, w, h = result.rect
    assert _inside(result.rect, (100, 100, 800, 300))
    assert w * h > 0.9 * 800 * 300
    assert result.ratio is None


def test_maximal_rect_finds_the_largest_all_true_rectangle():
    mask = np.zeros((10, 12), bool)
    mask[1:4, 1:11] = True  # 10 x 3 = 30
    mask[1:9, 1:4] = True  # 3 x 8 = 24, overlapping
    assert auto_crop._maximal_rect(mask) == (1, 1, 10, 3)
    assert auto_crop._maximal_rect(np.zeros((5, 5), bool))[2:] == (0, 0)


def _dense_band_scene() -> np.ndarray:
    """A dense carrier band 60 px wide against the picture's left edge."""
    normalized = _scene(picture=(60, 60, 640, 480), rebate=None, fill_outside=False)
    # Rebate on the other three sides keeps the thin anchor honest.
    normalized[:60, :] = _REBATE
    normalized[540:, :] = _REBATE
    normalized[:, 700:] = _REBATE
    normalized[60:540, :60] = _CARRIER
    return normalized


def test_a_dense_carrier_band_is_excluded():
    result = _crop(_dense_band_scene())

    assert isinstance(result, AutoCrop)
    assert result.rect[0] >= 60


def test_the_exclusion_hint_alone_still_excludes_the_band(monkeypatch):
    monkeypatch.setattr(
        auto_crop,
        "_dense_carrier",
        lambda normalized, covered: (np.zeros_like(covered), set()),
    )
    without_hint = _crop(_dense_band_scene())
    assert without_hint.rect[0] < 60  # the mask alone cannot tell it apart

    with_hint = _crop(_dense_band_scene(), exclude=(60, 0, 940, 600))

    assert with_hint.rect[0] >= 60


def test_the_hint_is_a_fallback_only_on_edges_the_dense_pass_found_nothing():
    """On the band's own edge the dense pass has already spoken; the
    meter-deep hint must not eat picture there."""
    deep_hint = (200, 0, 800, 600)  # would exclude x < 200

    result = _crop(_dense_band_scene(), exclude=deep_hint)

    assert isinstance(result, AutoCrop)
    assert 60 <= result.rect[0] < 200


def test_a_flat_dense_blob_that_is_not_a_band_is_not_carrier():
    normalized = _scene()
    normalized[130:150, 160:180] = _CARRIER  # a small dense mark in the picture

    assert isinstance(_crop(normalized), AutoCrop)


@pytest.mark.parametrize(
    "quarter_turns, flipped",
    [(0, False), (1, False), (2, False), (3, False), (0, True), (1, True)],
    ids=["plain", "cw1", "cw2", "cw3", "flip", "flip-cw1"],
)
def test_rect_under_a_display_transform_maps_back_to_one_tiff_window(
    quarter_turns, flipped
):
    """The same picture, viewed under every display transform, must come
    back through `display_crop_window_to_tiff` as the same TIFF window."""
    tilt = 2.0
    canvas = _encode(_scene())
    tiff = rotate_with_fill(canvas, tilt)  # the picture, tilted clockwise 2 deg
    height, width = tiff.shape[:2]
    # Squaring it up takes -tilt; a mirror reverses the tilt, so +tilt.
    fine = tilt if flipped else -tilt

    analysis, scale = analysis_display(
        tiff, quarter_turns=quarter_turns, flipped=flipped, fine_angle_deg=fine
    )
    assert scale == 1.0
    display_h, display_w = previews.display_shape(
        (height, width), quarter_turns=quarter_turns, crop_params=None
    )
    result = estimate_crop(
        analysis, full_size=(display_h, display_w), ratio=FORMAT_RATIOS["35mm"]
    )
    assert isinstance(result, AutoCrop)
    window = previews.display_crop_window_to_tiff(
        result.rect,
        (height, width),
        tilt_deg=0.0,
        quarter_turns=quarter_turns,
        flipped_horizontally=flipped,
        fine_angle_deg=fine,
        crop_params=None,
        full_frame=True,
    )

    reference = _reference_window()
    for got, want in zip(window[:4], reference[:4], strict=True):
        assert got == pytest.approx(want, abs=2)
    assert window[4] == pytest.approx(reference[4], abs=0.2)


def _reference_window() -> tuple[int, int, int, int, float]:
    """The window the un-transformed display produces, mapped to TIFF."""
    tilt = 2.0
    tiff = rotate_with_fill(_encode(_scene()), tilt)
    height, width = tiff.shape[:2]
    analysis, _ = analysis_display(tiff, fine_angle_deg=-tilt)
    result = estimate_crop(
        analysis, full_size=(height, width), ratio=FORMAT_RATIOS["35mm"]
    )
    return previews.display_crop_window_to_tiff(
        result.rect,
        (height, width),
        tilt_deg=0.0,
        quarter_turns=0,
        flipped_horizontally=False,
        fine_angle_deg=-tilt,
        crop_params=None,
        full_frame=True,
    )


def test_analysis_display_downscales_first_and_reports_the_scale():
    image = np.zeros((2000, 4000, 3), np.uint16)

    analysis, scale = analysis_display(image, quarter_turns=1)

    assert scale == pytest.approx(auto_crop.ANALYSIS_MAX_EDGE / 4000)
    assert analysis.shape[:2] == (round(4000 * scale), round(2000 * scale))


def test_the_estimate_scales_a_downscaled_analysis_up_to_full_resolution():
    normalized = _scene(
        1200, 2000, picture=(300, 240, 1400, 720), rebate=(100, 100, 1800, 1000)
    )
    full = _encode(normalized)
    analysis, _ = analysis_display(full)
    assert analysis.shape[1] == auto_crop.ANALYSIS_MAX_EDGE

    result = estimate_crop(
        analysis, full_size=(1200, 2000), ratio=FORMAT_RATIOS["35mm"]
    )

    assert isinstance(result, AutoCrop)
    assert _inside(result.rect, (300, 240, 1400, 720))
    _x, _y, w, h = result.rect
    assert w / h == pytest.approx(1.5, abs=0.01)


def test_a_monochrome_composite_works():
    codes = _encode(_scene())
    mono = codes[..., 0]

    result = estimate_crop(mono, full_size=mono.shape, ratio=FORMAT_RATIOS["35mm"])

    assert isinstance(result, AutoCrop)


def test_picture_mask_is_what_auto_rotate_calls_rebate():
    normalized = _scene()

    covered, rebate = picture_mask(normalized)

    assert covered[300, 500] and not rebate[300, 500]  # picture
    assert rebate[60, 500]  # rebate band
    assert not covered[10, 10]  # fill
    thin = normalized.min(axis=-1)
    assert thin[rebate].min() >= thin[covered].max() - 1  # sanity of ordering
    assert REBATE_SLACK > 0


# --- refusals ----------------------------------------------------------------


def test_refuses_no_coverage_on_an_all_fill_canvas():
    result = _crop(np.full((300, 400, 3), NORMALIZED_FILL, np.float32))

    assert result == Refusal("no_coverage")


def test_refuses_little_picture_when_the_mask_is_mostly_rebate():
    result = _crop(_scene(picture=(480, 290, 60, 40)))

    assert isinstance(result, Refusal)
    assert result.reason == "little_picture"


def test_refuses_ragged_when_the_format_does_not_suit_the_picture():
    result = _crop(
        _scene(picture=(250, 100, 500, 400), rebate=(200, 50, 600, 500)), "6x17"
    )  # a 3:1 gate on a 5:4 picture

    assert isinstance(result, Refusal)
    assert result.reason == "ragged"
    assert result.fill_fraction < auto_crop.AUTO_CROP_MIN_FILL_FRACTION


def test_refuses_no_fit_when_no_window_can_fit():
    result = estimate_crop(_encode(_scene()), full_size=(600, 1000), ratio=1e6)

    assert isinstance(result, Refusal)
    assert result.reason == "no_fit"


def test_refuses_too_small_below_the_crop_size_floor():
    result = estimate_crop(
        _encode(_scene()),
        full_size=(20, 30),
        ratio=FORMAT_RATIOS["35mm"],
    )

    assert isinstance(result, Refusal)
    assert result.reason == "too_small"
    assert repo.CROP_MIN_SIZE_PX > 0


def test_refusal_reasons_are_the_documented_tokens():
    assert set(auto_crop.REASONS) == {
        "no_coverage",
        "little_picture",
        "no_fit",
        "ragged",
        "too_small",
    }


# --- the carrier hint --------------------------------------------------------


def _normalization(insets, *, detected=True, rect=(100, 50, 800, 400)):
    return {
        "analysis_rect": list(rect),
        "film_extent": {"detected": detected, "insets": list(insets)},
    }


def test_no_hint_when_no_carrier_was_detected():
    assert (
        exclusion_hint(
            None,
            (600, 1000),
            quarter_turns=0,
            flipped=False,
            fine_angle_deg=0.0,
            analysis_size=(600, 1000),
        )
        is None
    )
    assert (
        exclusion_hint(
            _normalization((0, 0, 0, 0)),
            (600, 1000),
            quarter_turns=0,
            flipped=False,
            fine_angle_deg=0.0,
            analysis_size=(600, 1000),
        )
        is None
    )
    assert (
        exclusion_hint(
            _normalization((3, 3, 3, 3), detected=False),
            (600, 1000),
            quarter_turns=0,
            flipped=False,
            fine_angle_deg=0.0,
            analysis_size=(600, 1000),
        )
        is None
    )


def test_hint_is_the_inner_window_in_analysis_pixels():
    # 600 x 1000 is below the passthrough size, so a cell is one pixel.
    hint = exclusion_hint(
        _normalization((10, 0, 20, 0)),
        (600, 1000),
        quarter_turns=0,
        flipped=False,
        fine_angle_deg=0.0,
        analysis_size=(600, 1000),
    )

    x, y, w, h = hint
    assert x == 120  # analysis_rect x 100 + left inset 20
    assert y == 60  # analysis_rect y 50 + top inset 10
    # No inset on the right or bottom: nothing constrains those sides.
    assert x + w == 1000
    assert y + h == 600


def test_hint_follows_the_display_transform():
    hint = exclusion_hint(
        _normalization((0, 0, 20, 0)),  # a carrier on the TIFF's left edge
        (600, 1000),
        quarter_turns=1,  # ... which a clockwise turn puts on top
        flipped=False,
        fine_angle_deg=0.0,
        analysis_size=(1000, 600),
    )

    x, y, w, h = hint
    assert (x, w) == (0, 600)
    assert y == 120
    assert y + h == 1000


def test_hint_scales_to_the_analysis_copy():
    hint = exclusion_hint(
        _normalization((0, 0, 20, 0), rect=(0, 0, 4000, 2000)),
        (2000, 4000),
        quarter_turns=0,
        flipped=False,
        fine_angle_deg=0.0,
        analysis_size=(512, 1024),
    )

    block = 6  # ANALYSIS_BLOCK_PX: the canvas is over the passthrough size
    assert hint[0] == pytest.approx(20 * block * 0.256, abs=1)
