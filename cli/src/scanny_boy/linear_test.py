import numpy as np
import pytest
import rawpy

from scanny_boy.linear import MAX_CODE, decode_to_linear, encode_from_linear
from scanny_boy.raw_decode import RAW_PARAMS
from scanny_boy.sample_nef_support import (
    FIXTURES_DIR,
    NEGATIVE_1,
    requires_real_samples,
)


def test_round_trip_is_exact_for_every_code():
    codes = np.arange(MAX_CODE + 1, dtype=np.uint16)
    linear = decode_to_linear(codes)
    back = encode_from_linear(linear)
    diff = back.astype(np.int64) - codes.astype(np.int64)
    assert np.abs(diff).max() == 0


@requires_real_samples
def test_round_trip_is_exact_on_a_real_intermediate():
    path = FIXTURES_DIR / NEGATIVE_1[0]
    with rawpy.imread(str(path)) as raw:
        pixels = raw.postprocess(**RAW_PARAMS)

    linear = decode_to_linear(pixels)
    back = encode_from_linear(linear)
    diff = back.astype(np.int64) - pixels.astype(np.int64)
    assert np.abs(diff).max() == 0


def test_decode_maps_endpoints_and_spans_the_range():
    assert decode_to_linear(np.array([0], dtype=np.uint16))[0] == 0.0
    assert decode_to_linear(np.array([MAX_CODE], dtype=np.uint16))[0] == pytest.approx(1.0)
    codes = np.arange(MAX_CODE + 1, dtype=np.uint16)
    assert np.all(np.diff(decode_to_linear(codes)) > 0)
    assert decode_to_linear(codes).dtype == np.float32


def test_encode_clamps_out_of_range_input():
    result = encode_from_linear(np.array([-0.5, 1.5], dtype=np.float32))
    assert int(result[0]) == 0
    assert int(result[1]) == MAX_CODE


def test_encode_rounds_to_nearest_code():
    half = 0.5 / MAX_CODE
    assert int(encode_from_linear(np.array([half * 0.9], dtype=np.float32))[0]) == 0
    assert int(encode_from_linear(np.array([half * 1.1], dtype=np.float32))[0]) == 1


@requires_real_samples
def test_decode_is_the_linear_identity_on_a_real_intermediate():
    """The decode is linear, so the decoded value must be the code's own
    fraction of full scale — no curve. Guarded against a real frame so a
    reintroduced transfer curve cannot pass unnoticed."""
    path = FIXTURES_DIR / NEGATIVE_1[0]
    with rawpy.imread(str(path)) as raw:
        pixels = raw.postprocess(**RAW_PARAMS)

    decoded = decode_to_linear(pixels).astype(np.float64)
    reference = pixels.astype(np.float64) / MAX_CODE
    assert np.max(np.abs(decoded - reference)) < 1e-6
