"""The committed synthetic bare-light reference at
`tests/fixtures/flatfield/bare-light.dng`.

`cli/tools/generate_bare_light_dng.py` writes it for the Swift integration
scenarios, whose flat-field profile must come from a bare light — a film
frame's scene content survives the gain map's smoothing and corrupts the
correction. These checks pin what the reference must decode to, so a bad
regeneration or a LibRaw change that breaks the DNG fails loudly here
instead of mysteriously in a multi-minute integration scenario. Skips when
the file is absent (a clean checkout carries it; CI has no use for it).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from scanny_boy.flatfield import GAIN_MAP_MAX_EDGE, compute_gain
from scanny_boy.linear import decode_to_linear
from scanny_boy.raw_decode import decode_raw

BARE_LIGHT_DNG = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "flatfield" / "bare-light.dng"
)

_requires_bare_light_reference = pytest.mark.skipif(
    not BARE_LIGHT_DNG.exists(),
    reason=f"synthetic bare-light reference not present at {BARE_LIGHT_DNG}; "
    "regenerate it with cli/tools/generate_bare_light_dng.py",
)


@_requires_bare_light_reference
def test_bare_light_reference_decodes_through_the_locked_params():
    frame = decode_raw(BARE_LIGHT_DNG)

    assert (frame.height, frame.width) == (512, 768)
    assert frame.pixels.dtype == np.uint16


@_requires_bare_light_reference
def test_bare_light_reference_yields_a_smooth_near_unity_gain_map():
    linear = decode_to_linear(decode_raw(BARE_LIGHT_DNG).pixels)
    gain = compute_gain(linear)

    # Shaped like a gain map: capped at the module's downsample edge, one
    # channel plane per sensor channel.
    assert gain.shape[0] <= GAIN_MAP_MAX_EDGE
    assert gain.shape[2] == 3

    # The falloff is a gentle 10%, so every multiplier sits near unity —
    # a reference whose correction swings wildly is not a bare light.
    assert gain.min() > 0.75
    assert gain.max() < 1.4

    # And it is radially symmetric: the corners need the most correction.
    centre = gain[gain.shape[0] // 2, gain.shape[1] // 2]
    corner = gain[-1, -1]
    assert corner.mean() > centre.mean()
