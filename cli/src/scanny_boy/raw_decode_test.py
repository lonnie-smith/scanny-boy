import hashlib

import numpy as np
import pytest
import rawpy

from scanny_boy.fake_nef_support import write_fake_nef
from scanny_boy.metadata import UnreadableRawError, UnsupportedRawError
from scanny_boy.raw_decode import RAW_PARAMS, decode_raw, jsonable_raw_params
from scanny_boy.sample_nef_support import FIXTURES_DIR, requires_real_samples

SAMPLE_FILE = "_DSC4638.NEF"


def test_jsonable_params_are_linear_and_wb_free():
    params = jsonable_raw_params()
    assert params["gamma"] == [1, 1]
    assert params["output_color"] == "raw"
    assert params["user_wb"] == [1, 1, 1, 1]
    assert params["use_camera_wb"] is False
    assert params["use_auto_wb"] is False
    assert params["adjust_maximum_thr"] == 0.0


def _pixel_hash(pixels: np.ndarray) -> str:
    return hashlib.sha256(pixels.tobytes()).hexdigest()


def test_decode_raw_maps_garbage_file_to_unreadable_raw(tmp_path):
    path = tmp_path / "garbage.NEF"
    path.write_bytes(b"not a raw file at all")

    with pytest.raises(UnreadableRawError):
        decode_raw(path)


def test_decode_raw_maps_non_raw_tiff_to_unsupported_raw(tmp_path):
    path = write_fake_nef(tmp_path / "a.NEF")

    with pytest.raises(UnsupportedRawError):
        decode_raw(path)


@requires_real_samples
def test_decode_sample_frame_shape_dtype_and_dimensions():
    frame = decode_raw(FIXTURES_DIR / SAMPLE_FILE)

    # Per appendix A: postprocess(**RAW_PARAMS) returns (4040, 6064, 3) uint16.
    assert frame.pixels.dtype == np.uint16
    assert frame.pixels.shape == (4040, 6064, 3)
    assert frame.height == 4040
    assert frame.width == 6064


@requires_real_samples
def test_decode_sample_frame_is_repeatable():
    first = decode_raw(FIXTURES_DIR / SAMPLE_FILE)
    second = decode_raw(FIXTURES_DIR / SAMPLE_FILE)

    # Do not hard-code the expected hash (section 7): it depends on the
    # LibRaw build. Compare two decodes of the same file to each other.
    assert _pixel_hash(first.pixels) == _pixel_hash(second.pixels)


@requires_real_samples
def test_no_auto_bright_changes_output_relative_to_default():
    path = FIXTURES_DIR / SAMPLE_FILE
    fixed = decode_raw(path)

    control_params = dict(RAW_PARAMS, no_auto_bright=False)
    with rawpy.imread(str(path)) as raw:
        control_pixels = raw.postprocess(**control_params)

    # RAW_PARAMS disables histogram-based auto brightening; the default
    # (auto bright on) must produce different pixel values for a real frame.
    assert not np.array_equal(fixed.pixels, control_pixels)


@requires_real_samples
def test_adjust_maximum_thr_changes_output_relative_to_default():
    path = FIXTURES_DIR / SAMPLE_FILE
    fixed = decode_raw(path)

    # rawpy's own default for adjust_maximum_thr is 0.75; RAW_PARAMS locks
    # it to 0.0 (see section 3.4) to keep the maximum fixed across a
    # negative rather than content-dependent.
    control_params = dict(RAW_PARAMS, adjust_maximum_thr=0.75)
    with rawpy.imread(str(path)) as raw:
        control_pixels = raw.postprocess(**control_params)

    assert not np.array_equal(fixed.pixels, control_pixels)


@requires_real_samples
def test_decode_is_linear_raw_with_unity_white_balance():
    """NegPy's pipeline assumes linear RGB straight from the raw decode:
    gamma (1, 1), sensor channels, unity WB. RAW_PARAMS must equal that
    decode exactly — pinned here against accidental drift."""
    path = FIXTURES_DIR / SAMPLE_FILE
    fixed = decode_raw(path)

    reference_params = {
        "output_bps": 16,
        "gamma": (1, 1),
        "no_auto_bright": True,
        "adjust_maximum_thr": 0.0,
        "use_camera_wb": False,
        "use_auto_wb": False,
        "user_wb": [1, 1, 1, 1],
        "output_color": rawpy.ColorSpace.raw,
        "demosaic_algorithm": rawpy.DemosaicAlgorithm.AHD,
        "four_color_rgb": False,
        "median_filter_passes": 0,
        "highlight_mode": rawpy.HighlightMode.Clip,
    }
    with rawpy.imread(str(path)) as raw:
        reference = raw.postprocess(**reference_params)

    assert np.array_equal(fixed.pixels, reference)


@requires_real_samples
def test_unity_white_balance_differs_from_camera_white_balance():
    """The decode applies no per-channel white-balance gain (NegPy handles
    channel balance in film terms); the camera's as-shot multipliers would
    rescale the channels, so the two decodes must differ."""
    path = FIXTURES_DIR / SAMPLE_FILE
    fixed = decode_raw(path)

    camera_params = dict(RAW_PARAMS, use_camera_wb=True)
    with rawpy.imread(str(path)) as raw:
        camera_pixels = raw.postprocess(**camera_params)

    assert not np.array_equal(fixed.pixels, camera_pixels)
