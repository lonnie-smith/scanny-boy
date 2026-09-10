"""The film-base detector's fast-tier tests, on synthetic arrays.

`measure()` is fed float32 linear light in [0, 1] directly — what
`film_base.load` produces after decode and flat-fielding — so no RAW
decoding happens here and nothing carries the `slow` marker.
"""

from __future__ import annotations

import numpy as np
import pytest

from scanny_boy import film_base
from scanny_boy.events import Code

# The analysis grid passes images at or below 1024 on a side through
# unchanged; these frames are small enough to stay fast and large enough
# that every area fraction clears the gates.
_SIZE = 1024
_CELLS = _SIZE * _SIZE

# A plausible colour-negative film base: an orange mask, dense in blue.
_BASE_DENSITY = (-0.42, -0.12, -0.99)
# Denser than base: picture content.
_PICTURE_DENSITY = (-1.0, -0.85, -1.4)
# Thinner than base: bare light around the film edge.
_BARE_DENSITY = (0.0, 0.0, 0.0)


def _linear_from_density(
    density: tuple[float, float, float], shape=_SIZE
) -> np.ndarray:
    """A uniform linear field at the given per-channel log10 densities."""
    linear = np.power(10.0, np.asarray(density, dtype=np.float64))
    return np.broadcast_to(linear, (shape, shape, 3)).astype(np.float32).copy()


def _gradient_picture(shape=_SIZE) -> np.ndarray:
    """Picture content with a smooth per-row density gradient spanning 0.9
    decades around _PICTURE_DENSITY. Real scene content is never
    featureless; a *uniform* dense field would itself be found by the peel
    as a flat population (and correctly trip the ambiguity gate), which is
    not what the picture-background tests are about."""
    rows = np.linspace(0.0, 0.9, shape, dtype=np.float64)
    density = (
        np.asarray(_PICTURE_DENSITY, dtype=np.float64)[np.newaxis, :]
        - rows[:, np.newaxis]
    )
    linear = np.power(10.0, density)[:, np.newaxis, :]
    return np.broadcast_to(linear, (shape, shape, 3)).astype(np.float32).copy()


def _painted(base_rects, bare_rects=(), shape=_SIZE) -> np.ndarray:
    """A frame: `base_rects` and `bare_rects` are (y0, y1, x0, x1) slices
    painted at the base/bare densities over gradient picture content."""
    linear = _gradient_picture(shape)
    for y0, y1, x0, x1 in base_rects:
        linear[y0:y1, x0:x1] = _linear_from_density(_BASE_DENSITY, 1)[0]
    for y0, y1, x0, x1 in bare_rects:
        linear[y0:y1, x0:x1] = _linear_from_density(_BARE_DENSITY, 1)[0]
    return linear


def test_uniform_field_returns_known_offset_and_passes():
    measurement = film_base.measure(_linear_from_density(_BASE_DENSITY))
    assert measurement.chosen_index == 0
    assert len(measurement.populations) == 1
    for measured, expected in zip(measurement.density, _BASE_DENSITY, strict=True):
        assert measured == pytest.approx(expected, abs=1e-5)
    assert measurement.populations[0].area_fraction == pytest.approx(1.0)
    assert measurement.grid_cells == _CELLS
    assert max(measurement.clipped_fractions) == pytest.approx(0.0)
    film_base.gate(measurement)  # does not raise


def test_exposure_invariance_of_the_deviations():
    """Exposure invariance at the measurement site: scaling the whole
    field (a shutter/aperture/ISO change) shifts every channel's log
    density by the same common-mode amount, so the per-channel deviations
    from their own median must not move."""
    reference = _linear_from_density(_BASE_DENSITY)
    deviations = []
    for scale in (1.0, 0.5, 0.125):
        measurement = film_base.measure(reference * scale)
        density = np.asarray(measurement.density)
        deviations.append(density - np.median(density))
    for channel in range(3):
        assert deviations[1][channel] == pytest.approx(deviations[0][channel], abs=1e-5)
        assert deviations[2][channel] == pytest.approx(deviations[0][channel], abs=1e-5)


def test_two_rebate_bands_merge_into_one_population():
    """The common case: two rebate bands with picture
    between them are ONE measurement of the summed area, not two of half."""
    band = _SIZE // 4
    linear = _painted(
        base_rects=[(0, band, 0, _SIZE), (_SIZE - band, _SIZE, 0, _SIZE)],
    )
    measurement = film_base.measure(linear)
    assert measurement.chosen_index == 0
    assert len(measurement.populations) == 1
    population = measurement.populations[0]
    assert population.area_fraction == pytest.approx(0.5)
    for measured, expected in zip(population.density, _BASE_DENSITY, strict=True):
        assert measured == pytest.approx(expected, abs=1e-5)
    film_base.gate(measurement)


def test_all_rebate_frame_yields_one_whole_grid_population():
    """A frame that is entirely base — the case detect_rebate cannot
    handle — is the easy case here."""
    measurement = film_base.measure(_linear_from_density(_BASE_DENSITY))
    assert len(measurement.populations) == 1
    assert measurement.populations[0].area_fraction == pytest.approx(1.0)
    film_base.gate(measurement)


def test_small_bare_light_sliver_loses_to_larger_rebate():
    """Bare light is thinner but small: the rebate wins the largest-area
    rule and the sliver is recorded as a second, thinner population."""
    linear = _painted(
        base_rects=[(0, _SIZE // 2, 0, _SIZE)],
        bare_rects=[(0, 8, 0, _SIZE)],
    )
    measurement = film_base.measure(linear)
    assert len(measurement.populations) == 2
    # Thinnest first: the bare-light sliver (log 0.0) sorts above the base.
    assert measurement.populations[0].luma > measurement.populations[1].luma
    chosen = measurement.populations[measurement.chosen_index]
    for measured, expected in zip(chosen.density, _BASE_DENSITY, strict=True):
        assert measured == pytest.approx(expected, abs=1e-5)
    assert chosen.area_fraction == pytest.approx(0.49, abs=0.01)
    assert measurement.populations[0].area_fraction < 0.02
    film_base.gate(measurement)


def test_two_similar_sized_flat_regions_are_ambiguous():
    """The refuse-rather-than-guess rule: a second large flat
    population at a different density (a dense uniform scene object, here)
    refuses the frame even though the rebate is the largest region."""
    linear = _painted(base_rects=[(0, 540, 0, _SIZE)])
    # One featureless dense block covering ~47% of the frame, just under
    # the rebate's ~51%: a flat population the peel finds below the rebate.
    linear[560:1020, :, :] = _linear_from_density(_PICTURE_DENSITY, 1)[0]
    measurement = film_base.measure(linear)
    chosen = measurement.populations[measurement.chosen_index]
    assert chosen.area_fraction == pytest.approx(540 / _SIZE, abs=0.01)
    with pytest.raises(film_base.FilmBaseError) as exc_info:
        film_base.gate(measurement)
    assert exc_info.value.code is Code.FILM_BASE_AMBIGUOUS


def test_rebate_under_twenty_percent_is_too_small():
    band = int(_SIZE * 0.15)
    linear = _painted(
        base_rects=[(0, band, 0, _SIZE)],
    )
    measurement = film_base.measure(linear)
    with pytest.raises(film_base.FilmBaseError) as exc_info:
        film_base.gate(measurement)
    assert exc_info.value.code is Code.FILM_BASE_TOO_SMALL


def test_clipped_rebate_is_refused():
    measurement = film_base.measure(_linear_from_density(_BARE_DENSITY))
    with pytest.raises(film_base.FilmBaseError) as exc_info:
        film_base.gate(measurement)
    assert exc_info.value.code is Code.FILM_BASE_CLIPPED
    assert "red" in exc_info.value.message


def test_blue_channel_below_floor_fails_while_luma_stays_comfortable():
    """Proof that the channel-floor gate is per-channel and not a luma test: blue
    through the orange mask runs out first, and the luma is still well
    above FILM_BASE_MIN_CHANNEL."""
    density = (-0.30, -0.30, -2.50)
    luma = 0.2126 * density[0] + 0.7152 * density[1] + 0.0722 * density[2]
    assert luma > film_base.FILM_BASE_MIN_CHANNEL
    measurement = film_base.measure(_linear_from_density(density))
    with pytest.raises(film_base.FilmBaseError) as exc_info:
        film_base.gate(measurement)
    assert exc_info.value.code is Code.FILM_BASE_TOO_DARK
    assert "blue" in exc_info.value.message


def test_single_hot_pixel_does_not_change_density():
    """The block-median reduction is what makes the statistic dust-immune;
    one saturated pixel must not move the per-channel medians."""
    linear = _linear_from_density(_BASE_DENSITY, shape=2048)
    linear[900, 900] = 1.0
    clean = film_base.measure(_linear_from_density(_BASE_DENSITY, shape=2048))
    hot = film_base.measure(linear)
    for measured, expected in zip(hot.density, clean.density, strict=True):
        assert measured == pytest.approx(expected, abs=1e-6)


def test_frame_with_no_flat_thin_region_is_not_found():
    """No population at all (nothing survives the flatness
    gate in any peel pass) is refused, not guessed. A whole-frame smooth
    density gradient — real scene content, no rebate anywhere — has no flat
    population: every candidate band's own spread exceeds the gate."""
    linear = _gradient_picture()
    measurement = film_base.measure(linear)
    assert measurement.populations == ()
    assert measurement.chosen_index == -1
    with pytest.raises(film_base.FilmBaseError) as exc_info:
        film_base.gate(measurement)
    assert exc_info.value.code is Code.FILM_BASE_NOT_FOUND


def test_build_params_records_every_constant():
    params = film_base.build_params()
    assert params == {
        "band_width": film_base.FILM_BASE_BAND_WIDTH,
        "anchor_percentile": film_base.FILM_BASE_ANCHOR_PERCENTILE,
        "max_component_spread": film_base.FILM_BASE_MAX_COMPONENT_SPREAD,
        "merge_separation": film_base.FILM_BASE_MERGE_SEPARATION,
        "max_passes": film_base.FILM_BASE_MAX_PASSES,
        "min_area_fraction": film_base.FILM_BASE_MIN_AREA_FRACTION,
        "ambiguous_ratio": film_base.FILM_BASE_AMBIGUOUS_RATIO,
        "max_clipped": film_base.FILM_BASE_MAX_CLIPPED,
        "min_channel": film_base.FILM_BASE_MIN_CHANNEL,
        "min_cells": film_base.FILM_BASE_MIN_CELLS,
        "measure_version": film_base.FILM_BASE_MEASURE_VERSION,
    }
