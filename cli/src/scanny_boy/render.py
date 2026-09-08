"""The export's render: a published TIFF's normalized log density becomes a
positive, in Adobe RGB (1998)-compatible colour, with the negative's
recorded tone op baked in (docs/EXPORT_PLAN.md §4).

This is the only place the published TIFF's codes become display pixels at
full resolution — deliberately separate from `exporter.py` (which stays
about files, edits and error handling) and from `previews.py` (which stays
8-bit and downscaled). It reproduces `tone.build_display_lut`'s rendering
at 16 bits instead of 8: that is the regression anchor `render_test` pins,
and the reason the exported file looks like the Edit tab's preview rather
than a second, differently-tuned rendering.

The chain, when a camera colour matrix is present, is:

    decode_normalized -> 1 - val (positive, normalized log exposure)
    -> ** GAMMA_ADOBE (linear light)
    -> 3x3 matrix (colour only: sensor RGB -> Adobe RGB, §4.3)
    -> clip to [0, 1]
    -> ** (1 / GAMMA_ADOBE) (back to display encoding)
    -> quantize to uint16
    -> tone curve (the negative's `tone` op), as the preview shows
    -> uint16

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
  published TIFF stays what `DECISIONS.md` says it is.

Stage 1 declares the modelling assumption this module is named for: *the
normalized positive log-exposure is read as an Adobe-RGB-encoded value.*
That assumption is not new — it is what the preview has always relied on
implicitly by writing `1 - val` into an 8-bit PNG and letting the viewer
apply a ~2.2 decode. This module only names it.
"""

from __future__ import annotations

import numpy as np

from scanny_boy import normalization, resample, tone
from scanny_boy.icc_profile import TRC_G_EXPORT

# One constant, three places (docs/EXPORT_PLAN.md §2.2): the profile's TRC
# tag writes it in s15Fixed16 (`TRC_G_EXPORT`), the generator's `curv` tag
# writes the same value in u8Fixed8, and this module derives its gamma
# from the pinned constant. 563/256 = 2.19921875, the published Adobe RGB
# (1998) gamma.
GAMMA_ADOBE = TRC_G_EXPORT / 65536.0

MAX_CODE = 65535


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
    convention's **XYZ -> camera RGB** matrix (docs/EXPORT_PLAN.md §3.1;
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


def _display_codes_lut(tone_params: dict[str, float] | None) -> np.ndarray:
    """The display-encoded code -> display code table the tone curve
    maps: `TONE_ENCODE_LUT[j] = rint(tone_curve(j / 65535) * 65535)`."""
    codes = np.arange(MAX_CODE + 1, dtype=np.float64) / MAX_CODE
    return np.rint(tone_curve(codes, tone_params) * MAX_CODE).astype(np.uint16)


def _positive_values(codes: np.ndarray) -> np.ndarray:
    """Codes -> positive normalized log exposure: `1 - decode_normalized`,
    clipped at 0 — the fill sentinel (above 1.0 decoded) renders black,
    exactly as the stitch-side property (MONOCHROME_PLAN §3.4) expects."""
    return np.clip(1.0 - normalization.decode_normalized(codes), 0.0, 1.0)


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


def render_export(
    image: np.ndarray,
    matrix: np.ndarray | None,
    tone_params: dict[str, float] | None,
    long_edge: int | None = None,
) -> tuple[np.ndarray, tuple[float, ...]]:
    """Renders one published TIFF's codes to the export's display pixels.

    `image` is uint16, `(H, W)` or `(H, W, 3)`; `matrix` is the 3x3
    camera -> Adobe RGB matrix, or `None` for a mono roll (§4.5 — the
    selection is the channel count, the fact the writer actually sees; a
    mono roll has one channel, no primaries to rotate, and its published
    channel is a noise-argument collapse, not a colorimetric one, so
    applying a colour matrix to it would be inventing a luminance
    weighting nobody measured). `tone_params` is the net `tone` op's
    params, or `None`.

    `long_edge` is the optional downsample: when set and the image is
    larger than it, the linear values are Lanczos3-resampled to the
    target long edge (`resample.target_size`, aspect preserved, never an
    upscale). The resize sits deliberately between the gamut clip and
    the display re-encode — it averages *linear light*, not the gamma-
    encoded codes a resampler handed the finished display pixels would
    average (resample's module docstring has the full argument). The
    `clip_fractions` stay the full-resolution gamut clip's measurement;
    a resize's ringing may kiss [0, 1] again and is clipped back before
    the re-encode, silently — that is resampling, not gamut mapping, and
    it does not belong in the provenance's clip record.

    Returns `(rendered, clipped_fractions)`, rendered uint16, downsampled
    when a resize was applied.

    Two paths, and the no-matrix one is a single table (§4.1): with no
    matrix between them the two gamma steps are a mathematical no-op, and
    writing them out only costs precision — so the mono path (and the
    anchor tests) is one precomputed 65536-entry LUT reproducing
    `tone.build_display_lut` exactly, at 16 bits instead of 8. The LUT
    stays the full-resolution path even when a `long_edge` was *asked
    for* but fits (no resize to do): only an actual resize leaves it.
    """
    if image.dtype != np.uint16:
        raise ValueError(
            f"render_export needs uint16 codes; got dtype {image.dtype}"
        )
    if image.ndim in (1, 2):
        # A mono roll's published TIFF is 2-D (§4.5 — selected on the
        # channel count, the fact the writer actually sees; a 1-D array is
        # the full-ramp domain the anchor tests index). One LUT, no
        # exponentiation, no matrix stage.
        if matrix is not None:
            raise ValueError(
                "a mono (single-channel) image takes no colour matrix; "
                "pass None"
            )
        size = (
            None
            if image.ndim == 1
            else resample.target_size(
                image.shape[0], image.shape[1], long_edge
            )
        )
        if size is None:
            lut = np.rint(
                tone_curve(_positive_values(np.arange(MAX_CODE + 1)), tone_params)
                * MAX_CODE
            ).astype(np.uint16)
            return lut[image], (0.0,)
        # The downsampled mono path walks the chain it collapses: linear
        # light, the resize, back to display encoding, then the same
        # tone LUT the fast path ends in.
        linear = np.power(
            _positive_values(image), GAMMA_ADOBE, dtype=np.float32
        )
        linear = np.clip(
            resample.resize_lanczos3(linear, size[0], size[1]), 0.0, 1.0
        )
        display = np.power(linear, 1.0 / GAMMA_ADOBE, dtype=np.float32)
        j = np.rint(display * MAX_CODE).astype(np.uint16)
        return _display_codes_lut(tone_params)[j], (0.0,)

    if matrix is None:
        raise ValueError(
            "a colour image needs the camera colour matrix; a silent "
            "identity fallback is precisely the bug the export plan "
            "exists to remove (EXPORT_PLAN §3.4)"
        )
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(
            f"render_export needs a (H, W, 3) colour image; got shape {image.shape}"
        )

    # Stage 1 — code to linear, via a float32 LUT.
    codes = np.arange(MAX_CODE + 1, dtype=np.float64)
    linear_lut = (
        np.clip(1.0 - normalization.decode_normalized(codes), 0.0, 1.0)
        ** GAMMA_ADOBE
    ).astype(np.float32)
    linear = linear_lut[image]

    # Stage 2 — the matrix (colour only), then the gamut clip. Work in
    # float32; the camera gamut is not contained in Adobe RGB, so
    # saturated colours land outside [0, 1] and are clipped per channel —
    # measured and recorded (§4.4), not corrected (gamut compression is a
    # feature with its own plan).
    linear = linear @ np.asarray(matrix, dtype=np.float32).T
    preclip = linear
    linear = np.clip(preclip, 0.0, 1.0)
    fractions = _clipped_fractions(preclip, linear)

    # Stage 2.5 — the optional downsample, in linear light: after the
    # gamut clip (its fractions are a full-resolution measurement), before
    # the display re-encode — the one place a resampler averages light
    # rather than encoded codes. Lanczos3, aspect preserved, never an
    # upscale (`resample.target_size`).
    size = resample.target_size(image.shape[0], image.shape[1], long_edge)
    if size is not None:
        linear = np.clip(
            resample.resize_lanczos3(linear, size[0], size[1]), 0.0, 1.0
        )

    # Stage 3 — back to display, tone, quantize, via a second LUT. The
    # quantize-before-LUT is why the colour path is not bit-exact against
    # the no-matrix path: a half-code rounding error enters the tone
    # curve, whose slope reaches 4.0. Measured worst case is 3 codes of
    # 65535; §4.7 pins the bound at 4.
    display = np.power(linear, 1.0 / GAMMA_ADOBE, dtype=np.float32)
    j = np.rint(display * MAX_CODE).astype(np.uint16)
    tone_lut = _display_codes_lut(tone_params)
    return tone_lut[j], fractions
