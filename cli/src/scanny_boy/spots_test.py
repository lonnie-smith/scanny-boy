"""Tests for the spot detector and repair (`spots.py`): synthetic
normalized-density arrays only — no RAW, no TIFF, fast-tier throughout
(docs/SPOTTING_PLAN.md §4)."""

from __future__ import annotations

import itertools

import numpy as np
import pytest

from scanny_boy import spots

# A fixture canvas that keeps the derived gates meaningful: at 1600 on the
# short edge, max_minor_px = 6.4 and se_radius_px = 7, so a radius-3 disk
# passes the blob gates and a radius-9 one fails them.
H, W = 1600, 2000
BASE_CODE = 30000
NOISE_SEED = 1234


def _canvas(noise_sigma: float = 0.0, seed: int = NOISE_SEED) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.full((H, W, 3), BASE_CODE, dtype=np.float32)
    if noise_sigma:
        image += rng.normal(0.0, noise_sigma, image.shape)
    return np.clip(np.rint(image), 0, 65535).astype(np.uint16)


def _plant_disk(
    image: np.ndarray,
    cy: int,
    cx: int,
    radius: int,
    delta: int,
    channel: int | None = None,
) -> None:
    yy, xx = np.ogrid[:H, :W]
    disk = (yy - cy) ** 2 + (xx - cx) ** 2 <= radius**2
    for ch in range(3):
        if channel is None or channel == ch:
            image[..., ch][disk] = np.clip(
                image[..., ch][disk].astype(np.int32) + delta, 0, 65535
            ).astype(np.uint16)


def _plant_line(
    image: np.ndarray, y: int, x0: int, length: int, width: int, delta: int
) -> None:
    image[y : y + width, x0 : x0 + length] = np.clip(
        image[y : y + width, x0 : x0 + length].astype(np.int32) + delta,
        0,
        65535,
    ).astype(np.uint16)


def _detect(
    image,
    sensitivity=spots.DEFAULT_SENSITIVITY,
    valid_rect=None,
    ranges=(1.0, 1.0, 1.0),
):
    return spots.detect(
        image, valid_rect=valid_rect, channel_ranges=ranges, sensitivity=sensitivity
    )


def _bbox_centre(spot):
    x, y, w, h = spot["bbox"]
    return x + w / 2, y + h / 2


# --- the detector -------------------------------------------------------------


def test_dark_disk_is_found_as_a_dense_blob():
    image = _canvas()
    _plant_disk(image, 800, 1000, 3, -60)
    result = _detect(image)
    assert result.found == 1
    assert len(result.spots) == 1
    spot = result.spots[0]
    assert spot["kind"] == "blob"
    assert spot["polarity"] == "dense"
    x, y, w, h = spot["bbox"]
    assert x <= 1000 <= x + w
    assert y <= 800 <= y + h


def test_bright_disk_is_found_as_a_thin_blob():
    image = _canvas()
    _plant_disk(image, 800, 1000, 3, 60)
    result = _detect(image)
    assert result.found == 1
    assert result.spots[0]["polarity"] == "thin"


def test_hair_is_found_as_a_streak_with_exact_mask():
    image = _canvas()
    _plant_line(image, 700, 400, 300, 3, -60)
    result = _detect(image)
    assert result.found == 1
    spot = result.spots[0]
    assert spot["kind"] == "streak"
    assert spot["polarity"] == "dense"
    mask = spots.decode_rle(spot["rle"], spot["bbox"][2], spot["bbox"][3])
    # The mask covers the line and not the box (§1.3).
    assert mask.sum() == 300 * 3
    assert mask.shape == (spot["bbox"][3], spot["bbox"][2])
    assert spot["area"] == 300 * 3


@pytest.mark.parametrize("seed", [7, 42, 99])
def test_grain_alone_finds_nothing(seed):
    for noise_sigma in (1.0, 2.0, 4.0):
        result = _detect(_canvas(noise_sigma=noise_sigma, seed=seed))
        assert result.found == 0


def test_neutrality_gate_rejects_a_coloured_feature():
    image = _canvas()
    _plant_disk(image, 800, 1000, 3, -180, channel=0)
    assert _detect(image).found == 0


def test_neutrality_gate_keeps_a_neutral_feature():
    image = _canvas()
    _plant_disk(image, 800, 1000, 3, -60)
    assert _detect(image).found == 1


def test_channel_ranges_are_honoured():
    # An equal *log* offset arrives in val space scaled by 1/span per
    # channel (§0.3); with unequal spans, planting the val offsets that
    # reconstruct an equal log offset is kept, planting an equal val
    # offset is rejected.
    ranges = (0.5, 1.0, 2.0)
    log_offset = -0.04  # normalized log-density units

    equal_log = _canvas()
    yy, xx = np.ogrid[:H, :W]
    disk = (yy - 800) ** 2 + (xx - 1000) ** 2 <= 9
    for ch, span in enumerate(ranges):
        val_offset = log_offset / span
        code_offset = round(val_offset / spots._CODE_TO_VAL)
        equal_log[..., ch][disk] = np.clip(
            equal_log[..., ch][disk].astype(np.int32) + code_offset, 0, 65535
        ).astype(np.uint16)
    assert _detect(equal_log, ranges=ranges).found == 1

    equal_val = _canvas()
    code_offset = round(log_offset / spots._CODE_TO_VAL)
    for ch in range(3):
        equal_val[..., ch][disk] = np.clip(
            equal_val[..., ch][disk].astype(np.int32) + code_offset, 0, 65535
        ).astype(np.uint16)
    assert _detect(equal_val, ranges=ranges).found == 0


def test_fill_and_valid_rect_are_excluded():
    # A defect outside valid_rect is never seen (§0.4).
    image = _canvas()
    _plant_disk(image, 100, 100, 3, -60)
    assert _detect(image, valid_rect=(600, 600, 800, 500)).found == 0

    # A fill-sentinel region abutting real content: the erosion by the SE
    # radius keeps a component from straddling the boundary, so a defect
    # inside that band — and one on the fill itself — yield nothing.
    image = _canvas()
    image[:, 1200:] = spots.FILL_CODE
    _plant_disk(image, 800, 1195, 3, -60)  # inside the eroded band
    _plant_disk(image, 400, 1400, 3, -60)  # on the fill plateau
    assert _detect(image).found == 0


def test_size_gates_bite():
    # A disk larger than 2 * max_minor_px is rejected as content.
    image = _canvas()
    _plant_disk(image, 800, 1000, 9, -60)
    assert _detect(image).found == 0

    # A streak longer than MAX_STREAK_LENGTH_FRACTION * long_edge.
    image = _canvas()
    _plant_line(image, 700, 400, 700, 3, -60)
    assert _detect(image).found == 0

    # A 3-pixel component is below MIN_SPOT_AREA_PX.
    image = _canvas()
    image[800, 1000:1003] -= 90
    assert _detect(image).found == 0


def test_sensitivity_is_monotone():
    image = _canvas(noise_sigma=2.0)
    _plant_disk(image, 500, 500, 2, -35)
    _plant_disk(image, 900, 1500, 3, -50)
    _plant_line(image, 1200, 300, 250, 2, -45)
    counts = [
        _detect(image, sensitivity=sensitivity).found
        for sensitivity in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]
    assert all(later >= earlier for earlier, later in itertools.pairwise(counts))
    assert counts[-1] >= counts[0]


def test_cap_holds_and_ids_run_in_raster_order():
    image = _canvas()
    # A 40x40 grid of cells holds 600 radius-3 disks, far apart enough
    # that the SE radius 7 never bridges two of them.
    centres = [(30 + 40 * row, 30 + 40 * col) for row in range(20) for col in range(30)]
    assert len(centres) == 600
    for cy, cx in centres:
        _plant_disk(image, cy, cx, 3, -60)
    result = _detect(image)
    assert result.found == 600
    assert len(result.spots) == spots.MAX_SPOTS
    assert [spot["id"] for spot in result.spots] == list(range(1, spots.MAX_SPOTS + 1))
    keys = [(spot["bbox"][1], spot["bbox"][0]) for spot in result.spots]
    assert keys == sorted(keys)


def test_sensitivity_out_of_range_raises():
    image = _canvas()
    with pytest.raises(ValueError):
        _detect(image, sensitivity=-0.1)
    with pytest.raises(ValueError):
        _detect(image, sensitivity=1.1)


# --- the RLE mask --------------------------------------------------------------


def test_rle_round_trips_a_random_mask():
    rng = np.random.default_rng(5)
    mask = rng.random((37, 41)) > 0.5
    rle = spots.encode_rle(mask)
    assert sum(rle) == 37 * 41
    np.testing.assert_array_equal(spots.decode_rle(rle, 41, 37), mask)


def test_rle_starts_with_a_zeros_run():
    mask = np.ones((2, 3), dtype=bool)
    assert spots.encode_rle(mask) == [0, 6]


def test_plan_example_rle_is_a_plus_shape():
    # §1.1's fixture: a 5x4 box holding a plus shape.
    rle = [1, 3, 1, 10, 1, 3, 1]
    assert sum(rle) == 5 * 4
    mask = spots.decode_rle(rle, 5, 4)
    assert mask.sum() == 16
    assert mask.tolist() == [
        [False, True, True, True, False],
        [True, True, True, True, True],
        [True, True, True, True, True],
        [False, True, True, True, False],
    ]


# --- the repair -----------------------------------------------------------------


def _params(image, spots_list, repair=True, canvas=None):
    return spots.spots_params(
        canvas=canvas or (image.shape[1], image.shape[0]),
        spots=spots_list,
        sensitivity=0.5,
        repair=repair,
    )


def _detected_params(image, repair=True):
    result = _detect(image)
    return _params(image, result.spots, repair=repair), result


def test_apply_repair_is_a_no_op_without_a_live_set():
    image = _canvas()
    _plant_disk(image, 800, 1000, 3, -60)
    params, _ = _detected_params(image)

    assert spots.apply_repair(image, None) is image
    np.testing.assert_array_equal(
        spots.apply_repair(image, _params(image, params["spots"], repair=False)), image
    )
    rejected = [{**spot, "rejected": True} for spot in params["spots"]]
    np.testing.assert_array_equal(
        spots.apply_repair(image, _params(image, rejected)), image
    )


def test_apply_repair_touches_only_the_dilated_mask():
    image = _canvas()
    _plant_disk(image, 800, 1000, 3, -60)
    params, result = _detected_params(image)
    spot = result.spots[0]
    repaired = spots.apply_repair(image, params)

    changed = np.any(repaired != image, axis=2)
    assert changed.sum() > spot["area"]  # the dilation grew it
    mask = np.zeros((H, W), dtype=np.uint8)
    x, y, w, h = spot["bbox"]
    mask[y : y + h, x : x + w] = spots.decode_rle(spot["rle"], w, h)
    dilated = cv2_dilate(mask)
    # Bit-exact outside the dilated mask. (On a flat field an inpainted
    # pixel inside it may coincidentally equal its original, so the
    # containment is one-way.)
    assert not (changed & ~dilated.astype(bool)).any()

    # Inside the mask the pixels moved toward their surroundings.
    before = image[..., 0][dilated.astype(bool)].astype(np.int32)
    after = repaired[..., 0][dilated.astype(bool)].astype(np.int32)
    assert abs(after.mean() - BASE_CODE) < abs(before.mean() - BASE_CODE)


def cv2_dilate(mask):
    import cv2

    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (2 * spots.REPAIR_DILATE_PX + 1, 2 * spots.REPAIR_DILATE_PX + 1),
    )
    return cv2.dilate(mask, kernel)


def test_apply_repair_handles_mono_and_colour():
    colour = _canvas()
    _plant_disk(colour, 800, 1000, 3, -60)
    params, _ = _detected_params(colour)
    repaired = spots.apply_repair(colour, params)
    assert repaired.shape == colour.shape
    assert np.any(repaired != colour)

    mono = colour[..., 0].copy()
    mono_params = spots.spots_params(
        canvas=(mono.shape[1], mono.shape[0]),
        spots=[
            {
                "id": 1,
                "kind": "blob",
                "polarity": "dense",
                "bbox": [997, 797, 7, 7],
                "rle": spots.encode_rle(
                    (np.ogrid[:7, :7][0] - 3) ** 2 + (np.ogrid[:7, :7][1] - 3) ** 2 <= 9
                ),
                "area": 29,
                "score": 10.0,
                "rejected": False,
            }
        ],
        sensitivity=0.5,
        repair=True,
    )
    mono_repaired = spots.apply_repair(mono, mono_params)
    assert mono_repaired.shape == mono.shape
    assert np.any(mono_repaired != mono)


def test_canvas_mismatch_repairs_nothing():
    # §1.5: a spot set recorded against one canvas repairs nothing on
    # another — this is the test that stops a re-stitch from inpainting
    # arbitrary pixels.
    image = _canvas()
    _plant_disk(image, 800, 1000, 3, -60)
    params, _result = _detected_params(image)

    other = _canvas()[:1500, :, :].copy()  # a different-shaped canvas
    assert other.shape[:2] != image.shape[:2]
    np.testing.assert_array_equal(spots.apply_repair(other, params), other)
    assert not spots.is_repairing(params, other.shape[:2])
    assert spots.is_repairing(params, image.shape[:2])


def test_repaired_negative_re_detects_clean():
    image = _canvas()
    _plant_disk(image, 800, 1000, 3, -60)
    params, _result = _detected_params(image)
    repaired = spots.apply_repair(image, params)
    again = _detect(repaired)
    cx, cy = 1000, 800
    for spot in again.spots:
        x, y = _bbox_centre(spot)
        assert (x - cx) ** 2 + (y - cy) ** 2 > 30**2
