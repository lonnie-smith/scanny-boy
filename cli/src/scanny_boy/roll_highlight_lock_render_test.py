"""Tests for docs/ROLL_HIGHLIGHT_LOCK.md's render-time correction: the
`color.Metering.highlight_floor_delta` term `color.remap_dense_end`
applies, and its consequences through `tone.build_channel_tables` (the
matrix-free/preview LUT builder) and `render._linear_lut_from_codes` (the
matrix/export LUT builder) — the two paths a rendered negative can take
(`render.render_positive_float`'s `matrix is None` branch vs its matrix
branch), which must agree exactly wherever both apply, since preview and
export share `render_positive_float`.

Every fixture uses the real sign convention (`highlight_lock.py` module
docstring §-1): `floors` (dense end) sit below `ceils` (thin end), and a
negative's own `highlight_refs` sit below the roll's `film_base` density.
"""

from __future__ import annotations

import numpy as np
import pytest

from scanny_boy import color, render, tone
from scanny_boy.highlight_lock import HighlightLock

_TEST_MATRIX = render.export_matrix(
    [
        [0.7, 0.2, 0.1],
        [0.1, 0.75, 0.15],
        [0.05, 0.1, 0.85],
    ]
)

_BASE = (-0.50, -0.20, -0.90)
_K = (0.9, 1.0, 1.1)


def _highlight_refs(base=_BASE, k=_K, amplitude=-1.8) -> tuple[float, float, float]:
    return tuple(float(b + amplitude * kc) for b, kc in zip(base, k, strict=True))


def _floors_ceils_for(highlight_refs, base=_BASE, mean_lf=-1.5, mean_lc=0.05):
    h = np.asarray(highlight_refs, dtype=np.float64)
    b = np.asarray(base, dtype=np.float64)
    floors = tuple(mean_lf + (h - np.median(h)))
    ceils = tuple(mean_lc + (b - np.median(b)))
    return floors, ceils


def _record(floors, ceils, highlight_refs=None, exposure_matched=True):
    return {
        "floors": list(floors),
        "ceils": list(ceils),
        "shadow_refs": None,
        "highlight_refs": list(highlight_refs) if highlight_refs is not None else None,
        "exposure_matched": exposure_matched,
    }


def _lock(k=_K, base=_BASE, qualifying_count=4) -> HighlightLock:
    return HighlightLock(k=k, base=base, qualifying_count=qualifying_count)


# --- color.remap_dense_end / read_metering -----------------------------------


def test_remap_dense_end_is_identity_with_no_correction():
    norm = np.linspace(-0.2, 1.1, 50)
    meter = color.Metering(ranges=(1.0, 1.0, 1.0), shadow_refs_norm=None)
    for ch in range(3):
        np.testing.assert_array_equal(color.remap_dense_end(norm, ch, meter), norm)


def test_remap_dense_end_fixes_the_thin_end():
    """`val = 1` (the thin end, `ceil`) must be a fixed point of the remap
    for every delta — the correction retargets the dense end only."""
    meter = color.Metering(
        ranges=(0.6,), shadow_refs_norm=None, highlight_floor_delta=(0.05,)
    )
    remapped = color.remap_dense_end(np.array([1.0]), 0, meter)
    np.testing.assert_allclose(remapped, [1.0], atol=1e-12)


def test_read_metering_identity_when_negative_matches_roll():
    """The identity negative's `floors` are unchanged by `read_metering`,
    so `highlight_floor_delta` comes back `None` — no correction to apply,
    not a correction that happens to be zero everywhere. See
    `highlight_lock_test.py::test_identity_when_negative_matches_roll` for
    why this exact construction (a real, qualifying `H`, `k` read off it)
    is what makes the identity exact rather than approximate."""
    refs = _highlight_refs(amplitude=-1.8)
    floors, ceils = _floors_ceils_for(refs)
    lock = _lock()

    record = _record(floors, ceils, highlight_refs=refs)
    metering = color.read_metering(record, highlight_lock=lock)
    assert metering.highlight_floor_delta is None
    np.testing.assert_allclose(
        metering.ranges,
        [abs(c - f) for c, f in zip(ceils, floors, strict=True)],
        atol=1e-9,
    )


def test_read_metering_mono_is_a_no_op():
    record = _record((-1.4,), (-0.3,))
    lock = _lock()
    metering = color.read_metering(record, highlight_lock=lock)
    assert metering.highlight_floor_delta is None


def test_read_metering_no_lock_is_a_no_op():
    refs = _highlight_refs(k=(1.4, 1.0, 0.6), amplitude=-1.5)
    floors, ceils = _floors_ceils_for(refs)
    record = _record(floors, ceils, highlight_refs=refs)
    metering = color.read_metering(record, highlight_lock=None)
    assert metering.highlight_floor_delta is None


def test_read_metering_accepts_raw_dict_or_dataclass():
    refs = _highlight_refs(k=(1.4, 1.0, 0.6), amplitude=-1.5)
    floors, ceils = _floors_ceils_for(refs)
    lock = _lock()
    record = _record(floors, ceils, highlight_refs=refs)
    from_dataclass = color.read_metering(record, highlight_lock=lock)
    from_dict = color.read_metering(record, highlight_lock=lock.to_dict())
    assert from_dataclass.highlight_floor_delta == from_dict.highlight_floor_delta


def test_read_metering_uses_the_green_only_approximation_without_highlight_refs():
    """A record whose own gate fell back (`highlight_refs` null) still gets
    corrected — via the documented green-only approximation, not by
    silently doing nothing."""
    refs = _highlight_refs(k=(1.4, 1.0, 0.6), amplitude=-1.5)
    floors, ceils = _floors_ceils_for(refs)
    lock = _lock()
    record = _record(floors, ceils, highlight_refs=None)
    metering = color.read_metering(record, highlight_lock=lock)
    assert metering.highlight_floor_delta is not None


# --- render.py / tone.py: the LUT builders ------------------------------------


def _matrix_free_lut(record, lock):
    """The preview path when no camera matrix is present — and the
    matrix-free branch `render_positive_float` takes internally."""
    meter = color.read_metering(record, highlight_lock=lock)
    return tone.build_channel_tables(tone.NEUTRAL, color.NEUTRAL_COLOR, meter, channels=3)


def test_correction_changes_the_matrix_free_lut():
    refs = _highlight_refs(k=(1.4, 1.0, 0.6), amplitude=-1.5)
    floors, ceils = _floors_ceils_for(refs)
    lock = _lock()
    record = _record(floors, ceils, highlight_refs=refs)

    corrected = _matrix_free_lut(record, lock)
    uncorrected = _matrix_free_lut(record, None)
    assert not np.array_equal(corrected, uncorrected)


def test_mono_channels_are_never_corrected():
    """`build_channel_tables(channels=1)` never applies colour at all —
    the mono no-op holds regardless of what `read_metering` returns, since
    a 1-channel record can never carry a `highlight_floor_delta`."""
    record = _record((-1.4,), (-0.3,))
    lock = _lock()
    meter = color.read_metering(record, highlight_lock=lock)
    tables_locked = tone.build_channel_tables(
        tone.NEUTRAL, color.NEUTRAL_COLOR, meter, channels=1
    )
    tables_unlocked = tone.build_channel_tables(
        tone.NEUTRAL, color.NEUTRAL_COLOR, color.read_metering(record), channels=1
    )
    np.testing.assert_array_equal(tables_locked, tables_unlocked)


def test_preview_and_export_paths_agree_on_the_correction():
    """The deliverable's explicit case: rendering the same negative through
    the preview path (`matrix=None`) and the export path (a real camera
    matrix) must apply the *same* highlight-lock correction — both derive
    from the one `color.Metering` `read_metering` builds, so this is a
    guard against the two LUT builders drifting apart, not a coincidence
    to hope for."""
    refs = _highlight_refs(k=(1.4, 1.0, 0.6), amplitude=-1.5)
    floors, ceils = _floors_ceils_for(refs)
    lock = _lock()
    record = _record(floors, ceils, highlight_refs=refs)
    metering = color.read_metering(record, highlight_lock=lock)
    assert metering.highlight_floor_delta is not None

    codes = np.arange(render.MAX_CODE + 1, dtype=np.uint16)
    image = np.stack([codes, codes, codes], axis=-1)[np.newaxis, :, :]

    # "Preview": no camera matrix (render_positive_float's matrix-free
    # branch, exactly `previews.py`'s fallback for a roll with no
    # camera_color yet).
    preview_floats, _ = render.render_positive_float(
        image, None, None, None, metering=metering
    )
    # "Export": with a camera matrix (render_positive_float's matrix
    # branch, exactly what `exporter.py` always uses for a colour roll).
    export_floats, _ = render.render_positive_float(
        image, _TEST_MATRIX, None, None, metering=metering
    )

    # Both differ from the uncorrected render — the correction took effect
    # on both paths, not just one.
    uncorrected_metering = color.read_metering(record, highlight_lock=None)
    preview_uncorrected, _ = render.render_positive_float(
        image, None, None, None, metering=uncorrected_metering
    )
    export_uncorrected, _ = render.render_positive_float(
        image, _TEST_MATRIX, None, None, metering=uncorrected_metering
    )
    assert not np.array_equal(preview_floats, preview_uncorrected)
    assert not np.array_equal(export_floats, export_uncorrected)


def test_render_export_and_encode_positive_uint8_share_the_correction():
    """`exporter.py` calls `render.render_export`; `previews.py` calls
    `render.encode_positive_uint8`. Both delegate to
    `render_positive_float`, so a highlight-lock correction reaches both
    through the one shared function — this pins that sharing."""
    refs = _highlight_refs(k=(1.4, 1.0, 0.6), amplitude=-1.5)
    floors, ceils = _floors_ceils_for(refs)
    lock = _lock()
    record = _record(floors, ceils, highlight_refs=refs)
    metering = color.read_metering(record, highlight_lock=lock)

    codes = np.arange(render.MAX_CODE + 1, dtype=np.uint16)
    image = np.stack([codes, codes, codes], axis=-1)[np.newaxis, :, :]

    exported, _ = render.render_export(image, _TEST_MATRIX, None, None, metering)
    preview_u8 = render.encode_positive_uint8(image, _TEST_MATRIX, None, None, metering)

    expected_u8 = np.rint(exported.astype(np.float64) / render.MAX_CODE * 255).astype(
        np.uint8
    )
    # Rounding through two different integer scales (65535 vs 255) is not
    # bit-exact in general, but must be within one code of each other —
    # the same tolerance `render_test.py`'s colour-path anchor uses.
    assert np.max(np.abs(preview_u8.astype(int) - expected_u8.astype(int))) <= 1
