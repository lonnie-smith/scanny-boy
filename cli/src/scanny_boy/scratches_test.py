"""Tests for the scratch detector and correction (`scratches.py`): synthetic
normalized-density arrays only — no RAW, no TIFF, fast-tier throughout."""

from __future__ import annotations

import numpy as np
import pytest

from scanny_boy import scratches

H, W = 1600, 2000
BASE_CODE = 30000
NOISE_SEED = 1234
SPANS = (1.0, 1.0, 1.0)


def _canvas(noise_sigma: float = 0.0, seed: int = NOISE_SEED) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.full((H, W, 3), BASE_CODE, dtype=np.float32)
    if noise_sigma:
        image += rng.normal(0.0, noise_sigma, image.shape)
    return np.clip(np.rint(image), 0, 65535).astype(np.uint16)


def test_detect_returns_list_on_clean_canvas():
    image = _canvas()
    candidates = scratches.detect(image, SPANS)
    assert isinstance(candidates, list)


def test_detect_returns_list_for_too_small_image():
    tiny = np.full((32, 64, 3), BASE_CODE, dtype=np.uint16)
    candidates = scratches.detect(tiny, SPANS)
    assert isinstance(candidates, list)
    assert len(candidates) == 0


def test_detect_rejects_non_uint16():
    image = np.full((H, W, 3), 0.5, dtype=np.float32)
    candidates = scratches.detect(image, SPANS)
    assert len(candidates) == 0


def test_detect_rejects_2d_image():
    image = np.full((H, W), BASE_CODE, dtype=np.uint16)
    candidates = scratches.detect(image, SPANS)
    assert len(candidates) == 0


def test_candidate_attributes():
    """A detected candidate has the required attributes."""
    image = _canvas()
    candidates = scratches.detect(image, SPANS)
    for c in candidates:
        assert hasattr(c, "axis")
        assert c.axis in ("vertical", "horizontal")
        assert hasattr(c, "centres")
        assert hasattr(c, "score")
        assert hasattr(c, "agreement")
        assert hasattr(c, "drift")


def test_fit_returns_scratch_fit_for_candidate():
    """fit() returns a ScratchFit for any valid candidate."""
    image = _canvas()
    candidates = scratches.detect(image, SPANS)
    for c in candidates:
        result = scratches.fit(image, c)
        assert hasattr(result, "axis")
        assert hasattr(result, "centres")
        assert hasattr(result, "table")
        assert hasattr(result, "half_width_px")
        assert hasattr(result, "levels")
        assert hasattr(result, "score")
        assert hasattr(result, "agreement")


def test_scratches_params_returns_expected_keys():
    image = _canvas()
    candidates = scratches.detect(image, SPANS)
    fits = [scratches.fit(image, c) for c in candidates]
    params = scratches.scratches_params(canvas=(W, H), fits=fits, enabled=True)
    assert "detector_version" in params
    assert "enabled" in params
    assert "canvas" in params
    assert "scratches" in params
    assert params["detector_version"] == scratches.DETECTOR_VERSION
    assert params["enabled"] is True
    assert params["canvas"] == [W, H]
    assert isinstance(params["scratches"], list)


def test_scratches_params_empty_fits():
    params = scratches.scratches_params(canvas=(W, H), fits=[], enabled=False)
    assert params["scratches"] == []
    assert params["enabled"] is False


# --- apply --------------------------------------------------------------------


def test_apply_no_op_when_none():
    image = _canvas()
    original = image.copy()
    result = scratches.apply(image, None)
    np.testing.assert_array_equal(result, original)


def test_apply_no_op_when_empty_scratches():
    image = _canvas()
    original = image.copy()
    params = {"detector_version": 1, "enabled": True, "scratches": []}
    result = scratches.apply(image, params)
    np.testing.assert_array_equal(result, original)


def test_apply_no_op_when_disabled():
    image = _canvas()
    original = image.copy()
    params = {"detector_version": 1, "enabled": False, "scratches": []}
    result = scratches.apply(image, params)
    np.testing.assert_array_equal(result, original)


def test_apply_no_op_when_wrong_canvas():
    image = _canvas()
    original = image.copy()
    params = {"detector_version": 1, "enabled": True, "canvas": [100, 100],
              "scratches": [{"axis": "vertical"}]}
    result = scratches.apply(image, params)
    np.testing.assert_array_equal(result, original)


# --- is_live ------------------------------------------------------------------


def test_is_live_returns_false_for_none():
    assert scratches.is_live(None, (H, W)) is False


def test_is_live_returns_false_when_disabled():
    params = {"enabled": False, "scratches": []}
    assert scratches.is_live(params, (H, W)) is False


def test_is_live_returns_false_when_wrong_canvas():
    params = {"enabled": True, "canvas": [100, 100],
              "scratches": [{"axis": "vertical"}]}
    assert scratches.is_live(params, (H, W)) is False


def test_is_live_returns_true_when_enabled_with_scratches():
    params = {
        "enabled": True,
        "canvas": [W, H],
        "scratches": [{"axis": "vertical"}],
    }
    assert scratches.is_live(params, (H, W)) is True


def test_is_live_returns_false_when_no_scratches_list():
    params = {"enabled": True, "canvas": [W, H]}
    assert scratches.is_live(params, (H, W)) is False
