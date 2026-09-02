"""Tests for flat-field correction: the gain-map maths, the store, and the
transfer-curve round trip the application depends on.

The maths tests run on synthetic linear arrays — no RAW decoding needed. The
one test that must prove the `decode_to_linear -> multiply ->
encode_from_linear` round trip on a *real* decoded frame (section 2.8) is
gated on the shared sample NEFs like every other decode test.
"""

from __future__ import annotations

import numpy as np
import pytest

from scanny_boy import flatfield
from scanny_boy.events import Code
from scanny_boy.flatfield import FlatFieldError, FlatFieldProfile
from scanny_boy.linear import decode_to_linear, encode_from_linear
from scanny_boy.sample_nef_support import FIXTURES_DIR, requires_real_samples

SAMPLE_FILE = "_DSC4638.NEF"


def _radial_falloff(height: int, width: int, edge_fraction: float) -> np.ndarray:
    """Linear float32 scene: bright centre, smoothly darker corners — the
    shape a bare light source shot actually has."""
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float64)
    radius = np.sqrt(
        ((yy - (height - 1) / 2) / ((height - 1) / 2)) ** 2
        + ((xx - (width - 1) / 2) / ((width - 1) / 2)) ** 2
    )
    radius = np.clip(radius / np.sqrt(2), 0.0, 1.0)
    value = 1.0 - edge_fraction * radius**2
    return np.stack([value] * 3, axis=-1).astype(np.float32)


# --- compute_gain ----------------------------------------------------------


def test_falloff_reference_produces_a_corner_boosting_map():
    # A mild falloff: the gain normalises each channel to its own mean, so
    # the centre sits near 1.0 and the corners are boosted.
    linear = _radial_falloff(400, 600, edge_fraction=0.1)

    gain = flatfield.compute_gain(linear)

    height, width = gain.shape[:2]
    centre = gain[height // 2, width // 2]
    corner = gain[2, 2]
    # The map undoes the falloff: near-unity in the middle, boosting where
    # the reference was dark.
    assert centre.dtype == np.float32
    assert 0.95 <= centre.min() <= 1.05
    assert corner.min() > centre.max()
    # ...and the whole map stays inside the locked bounds.
    assert gain.min() >= flatfield.GAIN_MIN
    assert gain.max() <= flatfield.GAIN_MAX


def test_falloff_beyond_the_clamp_is_clamped():
    # An extreme synthetic falloff would want far more than 4x at the
    # corners; the map must clamp instead of becoming an extreme multiplier.
    linear = _radial_falloff(300, 300, edge_fraction=0.999)
    linear[:, :, :] *= np.linspace(1.0, 0.001, 300, dtype=np.float32)[None, :, None]

    gain = flatfield.compute_gain(linear)

    assert gain.max() == pytest.approx(flatfield.GAIN_MAX)
    assert gain.min() >= flatfield.GAIN_MIN


def test_flat_reference_produces_an_all_ones_map():
    linear = np.full((128, 200, 3), 0.5, dtype=np.float32)

    gain = flatfield.compute_gain(linear)

    assert gain.shape == (128, 200, 3)
    assert np.all(gain == pytest.approx(1.0))


def test_gain_map_dimensions_never_exceed_the_max_edge():
    linear = _radial_falloff(1024, 1536, edge_fraction=0.5)

    gain = flatfield.compute_gain(linear)

    assert max(gain.shape[0], gain.shape[1]) == flatfield.GAIN_MAP_MAX_EDGE


def test_per_channel_constant_scales_cancel_identically():
    """Section 2.1's white-balance proof: two references differing only by a
    per-channel constant — which is exactly what decoding with
    `use_camera_wb=True` contributes — must produce byte-identical gain
    maps. The constants are powers of two so the invariance is exact rather
    than to within a float ULP."""
    linear = _radial_falloff(300, 450, edge_fraction=0.4)
    scaled = linear * np.array([2.0, 0.5, 4.0], dtype=np.float32)

    assert flatfield.compute_gain(scaled).tobytes() == flatfield.compute_gain(
        linear
    ).tobytes()


# --- apply_in_place ---------------------------------------------------------


def _encode(linear: np.ndarray) -> np.ndarray:
    return encode_from_linear(linear)


def test_applying_the_derived_map_flattens_the_falloff():
    height, width = 256, 384
    linear = _radial_falloff(height, width, edge_fraction=0.5)
    pixels = _encode(linear)
    gain = flatfield.compute_gain(linear)

    corrected = pixels.copy()
    flatfield.apply_in_place(corrected, flatfield.resize_gain_map(gain, width, height))
    corrected_linear = decode_to_linear(corrected)

    # Mean per channel preserved, spatial spread collapsed. The residual is
    # the part of the falloff smoother than the blur sigma removed — the
    # map is deliberately low-frequency, so "flattened" not "perfect".
    assert abs(corrected_linear.mean() - linear.mean()) < 0.02
    before = linear.std()
    after = corrected_linear.std()
    assert after < before / 4


def test_banded_application_equals_whole_array_application_exactly():
    height, width = 1100, 900  # deliberately not a multiple of FLATFIELD_BAND_ROWS
    rng = np.random.default_rng(7)
    gain = rng.uniform(0.5, 1.5, size=(height // 8, width // 8, 3)).astype(np.float32)
    full_res_gain = flatfield.resize_gain_map(gain, width, height)
    rng = np.random.default_rng(2)
    pixels = rng.integers(0, 65536, size=(height, width, 3)).astype(np.uint16)

    banded = pixels.copy()
    banded_clipped = flatfield.apply_in_place(banded, full_res_gain)

    whole = encode_from_linear(
        decode_to_linear(pixels) * full_res_gain
    )
    assert np.array_equal(banded, whole)
    # The clip count is likewise independent of where the bands fall.
    corrected = decode_to_linear(pixels) * full_res_gain
    whole_clipped = int(
        np.count_nonzero(np.any(corrected > 1.0, axis=-1))
    )
    assert banded_clipped == whole_clipped


def test_apply_in_place_counts_pixels_pushed_past_full_scale():
    height, width = 64, 64
    pixels = np.full((height, width, 3), 60000, dtype=np.uint16)
    gain = np.full((height, width, 3), 4.0, dtype=np.float32)  # GAIN_MAX

    clipped = flatfield.apply_in_place(pixels, gain)

    assert clipped == height * width
    assert np.all(pixels == 65535)


@requires_real_samples
def test_identity_gain_map_round_trips_a_real_frame_to_identical_bytes():
    """Section 2.8, proved not assumed: `DECODE_LUT` and
    `encode_from_linear` are exact inverses, so a gain map of exactly 1.0
    must leave a real decoded frame byte-identical — one extra round trip
    through the transfer curve that costs nothing."""
    from scanny_boy.raw_decode import decode_raw

    frame = decode_raw(FIXTURES_DIR / SAMPLE_FILE)
    original = frame.pixels.copy()
    ones = np.ones((frame.height, frame.width, 3), dtype=np.float32)

    clipped = flatfield.apply_in_place(frame.pixels, ones)

    assert np.array_equal(frame.pixels, original)
    assert clipped == 0


# --- the store ---------------------------------------------------------------


def _profile(**overrides) -> FlatFieldProfile:
    defaults = {
        "profile_id": "pid-1",
        "name": "Copy stand",
        "gain_map_path": "nowhere.npz",
        "gain_map_sha256": "deadbeef",
        "source_path": "/refs/bare.NEF",
        "reference_width": 6064,
        "reference_height": 4040,
        "params": flatfield.build_params(),
        "scanny_boy_version": "0.3.0",
        "created_at": "2026-09-01T00:00:00Z",
    }
    defaults.update(overrides)
    return FlatFieldProfile(**defaults)


def test_save_and_load_round_trip_the_array_and_the_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNY_BOY_LIBRARY_DB", str(tmp_path / "library.db"))
    from scanny_boy.library import db as library_db

    library_db.reset_engine_cache()
    gain_map = np.linspace(0.25, 4.0, 16 * 16 * 3, dtype=np.float32).reshape(16, 16, 3)
    try:
        path, sha256 = flatfield.save_gain_map("pid-1", gain_map)

        assert path == flatfield.flatfield_root() / "pid-1.npz"
        assert path.exists()

        profile = _profile(gain_map_path=str(path), gain_map_sha256=sha256)
        loaded = flatfield.load_gain_map(profile)
        assert np.array_equal(loaded, gain_map)
        assert loaded.dtype == np.float32
    finally:
        library_db.reset_engine_cache()


def test_load_gain_map_missing_file_raises_typed_error(tmp_path):
    profile = _profile(gain_map_path=str(tmp_path / "gone.npz"))

    with pytest.raises(FlatFieldError) as excinfo:
        flatfield.load_gain_map(profile)

    assert excinfo.value.code == Code.FLATFIELD_GAIN_MAP_MISSING


def test_load_gain_map_rejects_a_tampered_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SCANNY_BOY_LIBRARY_DB", str(tmp_path / "library.db"))
    from scanny_boy.library import db as library_db

    library_db.reset_engine_cache()
    try:
        path, sha256 = flatfield.save_gain_map(
            "pid-2", np.ones((8, 8, 3), dtype=np.float32)
        )
        profile = _profile(gain_map_path=str(path), gain_map_sha256=sha256)
        # Rewrite the file with a different map: same shape, wrong bytes.
        np.savez(path, format_version=flatfield.GAIN_MAP_FORMAT_VERSION,
                 gain_map=np.full((8, 8, 3), 2.0, dtype=np.float32))

        with pytest.raises(FlatFieldError) as excinfo:
            flatfield.load_gain_map(profile)

        assert excinfo.value.code == Code.FLATFIELD_GAIN_MAP_MISSING
    finally:
        library_db.reset_engine_cache()


def test_profile_token_carries_the_map_identity_and_not_the_name():
    profile = _profile(name="Whatever it is called tomorrow")

    token = flatfield.profile_token(profile)

    assert token == {
        "profile_id": "pid-1",
        "gain_map_sha256": "deadbeef",
        "params": flatfield.build_params(),
    }


def test_build_params_records_every_locked_constant():
    params = flatfield.build_params()

    assert params == {
        "gain_map_max_edge": 256,
        "blur_sigma_divisor": 16,
        "gain_min": 0.25,
        "gain_max": 4.0,
        "format_version": 1,
    }