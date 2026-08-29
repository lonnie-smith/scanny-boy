import numpy as np
import pytest
import rawpy

from scanny_boy.raw_decode import RAW_PARAMS
from scanny_boy.romm import (
    CURVE_OFFSET,
    DECODE_LUT,
    ENCODED_BREAKPOINT,
    LINEAR_BREAKPOINT,
    MAX_CODE,
    ROMM_GAMMA,
    ROMM_SLOPE,
    decode_to_linear,
    encode_from_linear,
)
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


def test_encoded_breakpoint_is_handled():
    # breakpoint code is ~554.05: code 554 is below it (linear branch),
    # code 555 is above it (power branch).
    below_code = 554
    above_code = 555
    assert (below_code / MAX_CODE) < ENCODED_BREAKPOINT
    assert (above_code / MAX_CODE) >= ENCODED_BREAKPOINT

    below_expected = (below_code / MAX_CODE) / ROMM_SLOPE
    above_expected = (
        ((above_code / MAX_CODE) + CURVE_OFFSET) / (1.0 + CURVE_OFFSET)
    ) ** ROMM_GAMMA

    assert DECODE_LUT[below_code] == pytest.approx(below_expected, abs=1e-7)
    assert DECODE_LUT[above_code] == pytest.approx(above_expected, abs=1e-7)


def test_linear_breakpoint_is_handled():
    below = LINEAR_BREAKPOINT * 0.9
    above = LINEAR_BREAKPOINT * 1.1

    below_encoded = encode_from_linear(np.array([below], dtype=np.float32))[0]
    above_encoded = encode_from_linear(np.array([above], dtype=np.float32))[0]

    below_expected = round(below * ROMM_SLOPE * MAX_CODE)
    above_expected = round(
        (above ** (1.0 / ROMM_GAMMA) * (1.0 + CURVE_OFFSET) - CURVE_OFFSET) * MAX_CODE
    )

    assert int(below_encoded) == below_expected
    assert int(above_encoded) == above_expected


@requires_real_samples
def test_decode_recovers_rawpy_linear_output():
    path = FIXTURES_DIR / NEGATIVE_1[0]

    with rawpy.imread(str(path)) as raw:
        gamma_pixels = raw.postprocess(**RAW_PARAMS)

    linear_params = dict(RAW_PARAMS)
    linear_params["gamma"] = (1, 1)
    with rawpy.imread(str(path)) as raw:
        linear_pixels = raw.postprocess(**linear_params)

    decoded = decode_to_linear(gamma_pixels).astype(np.float64)
    reference = linear_pixels.astype(np.float64) / MAX_CODE

    denominator = np.clip(reference, 1e-4, None)
    relative_error = np.abs(decoded - reference) / denominator

    assert np.max(relative_error) < 0.0005


def test_decode_is_monotonic_and_maps_endpoints():
    assert DECODE_LUT[0] == 0.0
    assert DECODE_LUT[MAX_CODE] == 1.0
    assert np.all(np.diff(DECODE_LUT) >= 0)


def test_encode_clamps_out_of_range_input():
    result = encode_from_linear(np.array([-0.5, 1.5], dtype=np.float32))
    assert int(result[0]) == 0
    assert int(result[1]) == MAX_CODE
