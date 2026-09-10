"""The committed synthetic base frame at
`tests/fixtures/base-frame/base-frame.dng`.

`cli/tools/generate_base_frame_dng.py`
writes it so the fast tier has a realistic film-base reference — the
common case, two rebate bands with a picture strip between them — without
needing a real NEF (which cannot be authored anyway). These checks pin what
the fixture must decode to and that the detector measures and gates it, so
a bad regeneration or a LibRaw change that breaks the DNG fails loudly here
instead of mysteriously in a slow-tier integration test. Skips when the
file is absent (a clean checkout carries it; CI has no use for it).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from scanny_boy import film_base
from scanny_boy.linear import decode_to_linear
from scanny_boy.raw_decode import decode_raw

BASE_FRAME_DNG = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "base-frame"
    / "base-frame.dng"
)

_requires_base_frame = pytest.mark.skipif(
    not BASE_FRAME_DNG.exists(),
    reason=f"synthetic base frame not present at {BASE_FRAME_DNG}; regenerate "
    "it with cli/tools/generate_base_frame_dng.py",
)


@_requires_base_frame
def test_base_frame_decodes_through_the_locked_params():
    frame = decode_raw(BASE_FRAME_DNG)

    assert (frame.height, frame.width) == (512, 768)
    assert frame.pixels.dtype == np.uint16


@_requires_base_frame
def test_base_frame_measures_the_rebate_bands_and_passes_the_gates():
    """The §0.4 common case: two rebate bands with picture between them
    merge into ONE dominant population of the summed area — the measurement
    the roll anchor would be — with an orange-mask shape (blue well below
    green), and the gates accept it."""
    linear = decode_to_linear(decode_raw(BASE_FRAME_DNG).pixels)
    measurement = film_base.measure(linear)

    chosen = measurement.populations[measurement.chosen_index]
    assert chosen.area_fraction == pytest.approx(0.5, abs=0.02)
    # The authored base is (-0.42, -0.12, -0.99): an orange mask, dense in
    # blue. The decode's absolute levels may carry LibRaw's own channel
    # scales, so pin the shape, not the level.
    deviations = np.asarray(chosen.density) - np.median(chosen.density)
    assert deviations[1] > 0.0  # green thinnest, through the mask
    assert deviations[2] < deviations[0]  # blue densest
    assert max(measurement.clipped_fractions) == pytest.approx(0.0, abs=1e-6)

    film_base.gate(measurement)  # does not raise


@_requires_base_frame
def test_base_frame_fixture_is_deterministic():
    """Regenerating the fixture must be byte-identical (the tool's fixed
    seed and layout), or a stale commit silently disagrees with its own
    generator."""
    import subprocess
    import sys

    regenerated = Path("/tmp") / "scanny-boy-base-frame-regen.dng"
    subprocess.run(
        [
            sys.executable,
            str(
                Path(__file__).resolve().parents[3]
                / "cli"
                / "tools"
                / "generate_base_frame_dng.py"
            ),
            "--out",
            str(regenerated),
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[3],
    )
    assert regenerated.read_bytes() == BASE_FRAME_DNG.read_bytes()
