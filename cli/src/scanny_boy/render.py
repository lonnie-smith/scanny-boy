"""The positive render: a published TIFF's normalized log density becomes a
display-encoded positive in Adobe RGB (1998)-compatible colour, with the
negative's recorded tone and colour ops baked in.

This is the only place the published TIFF's codes become display pixels at
full resolution — deliberately separate from `exporter.py` (which stays
about files, edits and error handling) and from `previews.py` (which stays
8-bit and downscaled). Preview and export share `render_positive_float` so
the Edit tab soft-proofs the file Lightroom gets.

The chain, when a camera colour matrix is present, is:

    decode_normalized -> global CMY -> 1 - val (positive)
    -> ** GAMMA_ADOBE (linear light)
    -> 3x3 matrix (colour only: sensor RGB -> Adobe RGB, §4.3)
    -> clip linear light to [0, DISPLAY_CEILING ** GAMMA_ADOBE]
    -> Lanczos3 resize to `long_edge`, when asked (still linear light)
    -> ** (1 / GAMMA_ADOBE) (back to display encoding on [0, DISPLAY_CEILING])
    -> quantize to uint16
    -> tone + colour curve (the negative's `tone` and `color` ops)
    -> dye separation when active
    -> uint16 / uint8

**Why the matrix sits between the two exponentiations** (§4.2):

- Not before the inversion — the published pixels are a negative; a
  primaries matrix on negative-sense values mixes complements.
- Not after the tone curve — the curve is a strong per-channel
  nonlinearity; mixing channels after it makes the hue of a highlight
  depend on how hard the grade was (coloured fringes on speculars).
- Not in true scene-linear light (undoing the stitch stage's per-channel
  normalization first) — that would undo the orange-mask removal, the one
  thing the normalization exists to do. The mask must come off per
  channel *before* any channel mixing; the ordering is not negotiable.
- Not at stitch time, baked into the published TIFF — that would change
  every published pixel, break the roll invariants, and make
  `decode_normalized` no longer the single inverse of the encode. The
  published TIFF stays a normalized log-density working intermediate.

When `matrix is None` on a three-channel image the gamma sandwich is
skipped — the preview fallback for a colour roll whose stitch has not yet
written `camera_color`, and the exact mono path (§4.1).
"""

from __future__ import annotations

import numpy as np

from scanny_boy import color, normalization, resample, tone
from scanny_boy.icc_profile import TRC_G_EXPORT

# One constant, three places: the profile's TRC
# tag writes it in s15Fixed16 (`TRC_G_EXPORT`), the generator's `curv` tag
# writes the same value in u8Fixed8, and this module derives its gamma
# from the pinned constant. 563/256 = 2.19921875, the published Adobe RGB
# (1998) gamma.
GAMMA_ADOBE = TRC_G_EXPORT / 65536.0

MAX_CODE = 65535

# The display value the encode's dense-end headroom reaches: normalized
# -NORMALIZED_HEADROOM_LOW inverts to 1 + NORMALIZED_HEADROOM_LOW. Every
# stage between the inversion and the tone curve carries values on
# [0, DISPLAY_CEILING] rather than [0, 1]; the curve is what brings them
# back.
DISPLAY_CEILING = 1.0 + normalization.NORMALIZED_HEADROOM_LOW

_LINEAR_CEILING = DISPLAY_CEILING**GAMMA_ADOBE


def tone_curve(
    values: np.ndarray, tone_params: dict[str, float] | None
) -> np.ndarray:
    """The positive display values through the negative's `tone` op, the
    same `tone.curve_values` the preview's LUT is built from — not a
    re-derivation. The preview and the export must agree, and the only way
    to guarantee that is to share the function. `tone_params is None` (no
    tone op, or the reset) means the identity ramp — the flat look the
    preview shows today, and the correct default, not a placeholder."""
    if tone_params is None:
        return np.clip(values, 0.0, 1.0)
    return tone.curve_values(values, tone.ToneParams(**tone_params))


# XYZ (D65) -> Adobe RGB (1998) linear. The published inverse of the
# Adobe RGB (1998) specification's RGB -> XYZ matrix.
XYZ_TO_ADOBE_RGB = np.array(
    [
        [2.0413690, -0.5649464, -0.3446944],
        [-0.9692660, 1.8760108, 0.0415560],
        [0.0134474, -0.1183897, 1.0154096],
    ]
)


def export_matrix(rgb_xyz_matrix) -> np.ndarray:
    """The 3x3 camera-RGB -> Adobe RGB matrix for one body (§4.3).

    `rgb_xyz_matrix` is LibRaw's `rgb_xyz_matrix` — the DNG `ColorMatrix`
    convention's **XYZ -> camera RGB** matrix (
    the name reads the other way, verified against real NEFs by
    `metadata_test`'s slow direction check). Compose its pseudo-inverse
    (camera -> XYZ, D65) with the XYZ -> Adobe RGB matrix, then
    **row-normalize** so every row sums to one.

    The row normalization is what makes it safe to apply a *camera* matrix
    to the *already normalized* channels: the stitch stage's per-channel
    normalization removes the orange mask and sets grey to grey, and a raw
    camera matrix would tint that carefully established neutral. Row
    normalization guarantees an equal-parts input `(x, x, x)` comes out as
    `(x, x, x)` — the matrix then acts purely on the *difference* between
    the channels, which is exactly the sensor-crosstalk part it is
    qualified to fix. Do not simplify this into a plain matrix multiply;
    that would silently re-tint every export (§0.3).

    Raises on a singular (rank-deficient) or non-finite matrix rather than
    returning something `pinv` improvised.
    """
    raw = np.asarray(rgb_xyz_matrix, dtype=np.float64)
    if raw.shape != (3, 3) or not np.all(np.isfinite(raw)):
        raise ValueError(
            "export_matrix needs a finite 3x3 rgb_xyz_matrix; got shape "
            f"{raw.shape}"
        )
    if np.linalg.matrix_rank(raw) < 3:
        raise ValueError(
            "the camera colour matrix is singular (rank < 3); refusing to "
            "let pinv improvise an export matrix"
        )
    m = XYZ_TO_ADOBE_RGB @ np.linalg.pinv(raw)
    m = m / m.sum(axis=1, keepdims=True)
    # The invariant this function exists to guarantee (§0.3): an
    # equal-parts input survives the matrix unchanged.
    assert np.allclose(m @ np.ones(3), np.ones(3), atol=1e-12)
    return m


def camera_matrix_from_roll(roll) -> np.ndarray | None:
    """The roll's frozen camera -> Adobe RGB matrix, or `None` when the
    block is absent (mono rolls, or a colour roll mid-stitch)."""
    if roll.camera_color is None:
        return None
    return export_matrix(roll.camera_color.rgb_xyz_matrix)


def _resolve_render_params(
    tone_params: dict[str, float] | None,
    color_params: dict[str, float] | None,
    metering: color.Metering | None,
    channels: int,
) -> tuple[tone.ToneParams | None, color.ColorParams, color.Metering]:
    tone_obj = tone.ToneParams(**tone_params) if tone_params else None
    color_obj = (
        color.ColorParams(**color_params) if color_params else color.NEUTRAL_COLOR
    )
    meter = metering or color.Metering(
        ranges=(1.0,) * channels, shadow_refs_norm=None
    )
    return tone_obj, color_obj, meter


def _flat_positive_lut() -> np.ndarray:
    """uint16 code -> float positive display with no tone or colour ops."""
    codes = np.arange(MAX_CODE + 1, dtype=np.float64)
    return np.clip(_positive_values(codes), 0.0, 1.0).astype(np.float32)


def _flat_positive(image: np.ndarray) -> np.ndarray:
    return _flat_positive_lut()[image]


def _is_flat_render(
    tone_params: dict[str, float] | None,
    color_params: dict[str, float] | None,
    metering: color.Metering | None = None,
    *,
    channels: int,
) -> bool:
    """The identity fast path (`_flat_positive_lut`/`_flat_positive`): true
    only when nothing — no tone op, no colour op, and (docs/ROLL_HIGHLIGHT_LOCK.md
    §2.3) no roll highlight-lock correction either — would make the
    rendered pixels differ from a bare `1 - decode_normalized`. The fast
    LUT is shared across all three channels; a highlight-lock correction is
    per-channel, so its presence must route through the per-channel path
    exactly as a `color` op does."""
    if metering is not None and metering.highlight_floor_delta is not None:
        return False
    return tone_params is None and (
        color_params is None or channels == 1
    )


def _needs_separation(color_obj: color.ColorParams, channels: int) -> bool:
    return (
        channels > 1
        and (
            color_obj.dye_separation != 1.0
            or color_obj.separation_damping != 0.0
        )
    )


def _has_render_op(
    tone_obj: tone.ToneParams | None,
    color_obj: color.ColorParams,
) -> bool:
    return tone_obj is not None or color_obj != color.NEUTRAL_COLOR


def _linear_lut_from_codes(
    color_obj: color.ColorParams,
    meter: color.Metering,
    *,
    channels: int,
    allow_headroom: bool = True,
) -> np.ndarray:
    """uint16 code -> linear light, shape `(channels, 65536)`."""
    codes = np.arange(MAX_CODE + 1, dtype=np.float64)
    norm = normalization.decode_normalized(codes)
    apply_color = channels > 1
    offsets = color.cmy_offsets(color_obj, meter) if apply_color else (0.0,) * channels
    luts = np.empty((channels, MAX_CODE + 1), dtype=np.float32)
    for ch in range(channels):
        offset = offsets[ch] if ch < len(offsets) else 0.0
        # docs/ROLL_HIGHLIGHT_LOCK.md §2.3: the roll highlight-lock
        # correction, applied immediately after the decode and before the
        # CMY offset / `1 - val` inversion — identity when no lock applies.
        channel_norm = color.remap_dense_end(norm, ch, meter) if apply_color else norm
        if apply_color:
            positive = np.maximum(1.0 - (channel_norm + offset), 0.0)
        else:
            positive = np.maximum(1.0 - channel_norm, 0.0)
        if not allow_headroom:
            positive = np.clip(positive, 0.0, 1.0)
        luts[ch] = np.power(positive, GAMMA_ADOBE).astype(np.float32)
    return luts


def _curve_lut_from_display_codes(
    tone_obj: tone.ToneParams | None,
    color_obj: color.ColorParams,
    meter: color.Metering,
    *,
    channels: int,
    display_ceiling: float,
) -> np.ndarray:
    """Post-matrix display code j -> curved float, shape `(channels, 65536)`."""
    if tone_obj is None and color_obj == color.NEUTRAL_COLOR:
        display_codes = np.arange(MAX_CODE + 1, dtype=np.float32) / MAX_CODE
        return np.broadcast_to(display_codes, (channels, MAX_CODE + 1)).copy()

    display_codes = (
        np.arange(MAX_CODE + 1, dtype=np.float64) / MAX_CODE * display_ceiling
    )
    apply_color = channels > 1
    tables = np.empty((channels, MAX_CODE + 1), dtype=np.float32)
    for ch in range(channels):
        tables[ch] = tone.curve_values(
            display_codes,
            tone_obj,
            color_obj,
            channel=ch if apply_color else None,
            metering=meter,
            apply_color=apply_color,
        ).astype(np.float32)
    return tables


def _positive_values(codes: np.ndarray) -> np.ndarray:
    """Codes -> positive normalized log exposure: `1 - decode_normalized`,
    clipped at 0 — the fill sentinel (above 1.0 decoded) renders black,
    exactly as the stitch-side property expects."""
    return np.maximum(1.0 - normalization.decode_normalized(codes), 0.0)


def _clipped_fractions(
    linear: np.ndarray, clipped: np.ndarray
) -> tuple[float, ...]:
    """Per-channel fraction of samples the gamut clip (§4.4) actually
    moved: out-of-gamut on either side. Per-channel clipping shifts hue
    slightly on the most saturated pixels — the ordinary, accepted
    behaviour of every matrix-based render; this records how much of it
    happened, for the export's XMP provenance."""
    channels = clipped.shape[-1]
    return tuple(
        float(np.count_nonzero(linear[..., c] != clipped[..., c])) / linear[..., c].size
        for c in range(channels)
    )


def _gather_channel_lut(
    image: np.ndarray, tables: np.ndarray
) -> np.ndarray:
    """Apply per-channel `(C, 65536)` tables to a uint16 `(H, W, C)` image."""
    channels = image.shape[-1]
    gathered = np.empty(image.shape, dtype=np.float32)
    for ch in range(channels):
        gathered[..., ch] = tables[ch][image[..., ch]]
    return gathered


def _as_density_codes(image: np.ndarray) -> np.ndarray:
    """Normalise `(H, W)`, `(H, W, 1)`, or `(H, W, 3)` uint16 density codes."""
    if image.ndim == 3 and image.shape[2] == 1:
        return image[:, :, 0]
    return image


def render_positive_float(
    image: np.ndarray,
    matrix: np.ndarray | None,
    tone_params: dict[str, float] | None,
    color_params: dict[str, float] | None = None,
    metering: color.Metering | None = None,
    long_edge: int | None = None,
) -> tuple[np.ndarray, tuple[float, ...]]:
    """Render density codes to float display pixels in `[0, 1]`.

    `image` is uint16 `(H, W)` or `(H, W, 3)`. Returns `(float32, clip
    fractions)` with the same shape as `image` (mono stays 2-D).

    `long_edge` is the optional downsample (the export's; the preview
    never passes it): when set and the image is larger than it, the
    linear values are Lanczos3-resampled to the target long edge after
    the gamut clip and before the display re-encode — the one place a
    resampler averages light rather than encoded codes. The clip
    fractions stay the full-resolution measurement.
    """
    if image.dtype != np.uint16:
        raise ValueError(
            f"render_positive_float needs uint16 codes; got dtype {image.dtype}"
        )
    image = _as_density_codes(image)

    if image.ndim in (1, 2):
        if matrix is not None:
            raise ValueError(
                "a mono (single-channel) image takes no colour matrix; "
                "pass None"
            )
        size = (
            None
            if image.ndim == 1
            else resample.target_size(image.shape[0], image.shape[1], long_edge)
        )
        if size is None:
            if _is_flat_render(tone_params, color_params, channels=1):
                return np.clip(_flat_positive(image), 0.0, 1.0), (0.0,)
            tone_obj, color_obj, meter = _resolve_render_params(
                tone_params, color_params, metering, channels=1
            )
            tables = tone.build_channel_tables(
                tone_obj, color.NEUTRAL_COLOR, meter, channels=1
            )
            result = tables[0][image]
            return np.clip(result, 0.0, 1.0), (0.0,)
        # The downsampled mono path walks the chain it collapses (§4.1):
        # linear light, the resize, back to display encoding, then the
        # same curve the fast path ends in.
        positive = _positive_values(image)
        if _is_flat_render(tone_params, color_params, channels=1):
            positive = np.clip(positive, 0.0, 1.0)
        linear = np.power(positive, GAMMA_ADOBE, dtype=np.float32)
        linear = np.clip(
            resample.resize_lanczos3(linear, size[0], size[1]),
            0.0,
            _LINEAR_CEILING,
        )
        display = np.power(linear, 1.0 / GAMMA_ADOBE, dtype=np.float32)
        if tone_params is None:
            return np.clip(display, 0.0, 1.0), (0.0,)
        tone_obj, _, meter = _resolve_render_params(
            tone_params, color_params, metering, channels=1
        )
        curved = tone.curve_values(
            display,
            tone_obj,
            color.NEUTRAL_COLOR,
            metering=meter,
            apply_color=False,
        )
        return np.clip(curved, 0.0, 1.0), (0.0,)

    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(
            f"render_positive_float needs a (H, W, 3) colour image; "
            f"got shape {image.shape}"
        )

    if (
        _is_flat_render(tone_params, color_params, metering, channels=3)
        and matrix is None
    ):
        return np.clip(_flat_positive(image), 0.0, 1.0), (0.0, 0.0, 0.0)

    tone_obj, color_obj, meter = _resolve_render_params(
        tone_params, color_params, metering, channels=3
    )

    has_op = _has_render_op(tone_obj, color_obj)
    display_ceiling = DISPLAY_CEILING if has_op else 1.0
    linear_ceiling = _LINEAR_CEILING if has_op else 1.0

    if matrix is None:
        tables = tone.build_channel_tables(tone_obj, color_obj, meter, channels=3)
        result = _gather_channel_lut(image, tables.astype(np.float32))
        if _needs_separation(color_obj, channels=3):
            result = color.apply_separation(result, color_obj)
        return np.clip(result, 0.0, 1.0), (0.0, 0.0, 0.0)

    linear_luts = _linear_lut_from_codes(
        color_obj,
        meter,
        channels=3,
        allow_headroom=has_op,
    )
    linear = _gather_channel_lut(image, linear_luts)
    linear = linear @ np.asarray(matrix, dtype=np.float32).T
    preclip = linear
    linear = np.clip(preclip, 0.0, linear_ceiling)
    fractions = _clipped_fractions(preclip, linear)

    # Stage 2.5 — the optional downsample, in linear light: after the
    # gamut clip (its fractions are a full-resolution measurement), before
    # the display re-encode — the one place a resampler averages light
    # rather than encoded codes. Lanczos3, aspect preserved, never an
    # upscale (`resample.target_size`); the ringing it may add is clipped
    # back to the same ceiling silently — resampling, not gamut mapping.
    size = resample.target_size(image.shape[0], image.shape[1], long_edge)
    if size is not None:
        linear = np.clip(
            resample.resize_lanczos3(linear, size[0], size[1]),
            0.0,
            linear_ceiling,
        )

    display = np.power(linear, 1.0 / GAMMA_ADOBE, dtype=np.float32)
    j = np.rint(display / display_ceiling * MAX_CODE).astype(np.uint16)
    curve_luts = _curve_lut_from_display_codes(
        tone_obj, color_obj, meter, channels=3, display_ceiling=display_ceiling
    )
    result = _gather_channel_lut(j, curve_luts)
    if _needs_separation(color_obj, channels=3):
        result = color.apply_separation(result, color_obj)
    return np.clip(result, 0.0, 1.0), fractions


def encode_positive_uint8(
    image: np.ndarray,
    matrix: np.ndarray | None,
    tone_params: dict[str, float] | None,
    color_params: dict[str, float] | None = None,
    metering: color.Metering | None = None,
) -> np.ndarray:
    """The shared positive render, quantized to 8-bit display codes."""
    floats, _ = render_positive_float(
        image, matrix, tone_params, color_params, metering
    )
    return np.rint(floats * 255).astype(np.uint8)


def render_export(
    image: np.ndarray,
    matrix: np.ndarray | None,
    tone_params: dict[str, float] | None,
    color_params: dict[str, float] | None = None,
    metering: color.Metering | None = None,
    long_edge: int | None = None,
) -> tuple[np.ndarray, tuple[float, ...]]:
    """Renders one published TIFF's codes to the export's display pixels.

    `image` is uint16, `(H, W)` or `(H, W, 3)`; `matrix` is the 3x3
    camera -> Adobe RGB matrix, or `None` for a mono roll (§4.5 — the
    selection is the channel count, the fact the writer actually sees; a
    mono roll has one channel, no primaries to rotate, and its published
    channel is a noise-argument collapse, not a colorimetric one, so
    applying a colour matrix to it would be inventing a luminance
    weighting nobody measured). `tone_params` and `color_params` are the
    net ops' param dicts, or `None`.

    `long_edge` is the optional downsample: when set and the image is
    larger than it, the linear values are Lanczos3-resampled to the
    target long edge (`resample.target_size`, aspect preserved, never an
    upscale). The resize sits deliberately between the gamut clip and
    the display re-encode — it averages *linear light*, not the gamma-
    encoded codes a resampler handed the finished display pixels would
    average (resample's module docstring has the full argument). The
    `clip_fractions` stay the full-resolution gamut clip's measurement;
    a resize's ringing may kiss the ceiling again and is clipped back
    before the re-encode, silently — that is resampling, not gamut
    mapping, and it does not belong in the provenance's clip record.

    Returns `(rendered, clipped_fractions)`, rendered uint16, downsampled
    when a resize was applied.
    """
    if image.ndim == 3 and matrix is None:
        raise ValueError(
            "a colour image needs the camera colour matrix; a silent "
            "identity fallback is precisely the bug the export plan "
            "exists to remove"
        )
    floats, fractions = render_positive_float(
        image, matrix, tone_params, color_params, metering, long_edge=long_edge
    )
    return np.rint(floats * MAX_CODE).astype(np.uint16), fractions
