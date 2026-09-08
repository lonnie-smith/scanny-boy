"""True Lanczos3 downsampling, in linear light.

Two properties matter, and both are the caller's pipeline's doing as much
as this module's:

- **Linear light.** The resampler must average scene-adjacent values, not
  gamma- or log-encoded codes: averaging display-encoded values darkens
  the midtones along a high-contrast edge (a black/white boundary would
  land on the midpoint of the *encoded* scale, noticeably darker than the
  midpoint of the light). `render.render_export` therefore calls this on
  its float32 linear stage — after the gamut clip, before the display
  re-encode and the tone curve.

- **A true Lanczos3 kernel**: `sinc(x) * sinc(x/3)`, six taps at 1:1, not
  OpenCV's `INTER_LANCZOS4` (an 8-tap, radius-4 variant). When downscaling
  by `scale` the kernel argument is scaled by `scale`, so a 2:1 reduction
  genuinely averages its twelve source samples per axis — a fixed-window
  resampler on downscaled geometry would alias. Weights are normalized to
  sum to one, and out-of-range source positions clamp to the edge sample
  (edge replication, what `cv2.resize` does too).

The resize is separable — horizontal pass, then vertical — and each pass
runs a gather plus weighted sum in row bands, so the transient memory at
a full 12096 x 8064 canvas stays a few hundred MB per band instead of the
several GB a whole-image gather would need. float32 throughout: the
inputs are display-linear values in [0, 1] and the encode quantizes to 16
bits, so nothing here needs float64.
"""

from __future__ import annotations

import numpy as np

# Row band size for both passes: small enough that a band's gathered
# `(band, out, taps, channels)` temp stays a few hundred MB at full-res
# colour, large enough that the per-band overhead is noise.
_BAND = 256


def target_size(
    height: int, width: int, long_edge: int
) -> tuple[int, int] | None:
    """The downscaled `(height, width)` for a `long_edge` target, aspect
    preserved — or `None` when there is nothing to do: no target, or an
    image whose long edge is already at or below it (never an upscale;
    the caller reports that skip rather than a resize)."""
    if long_edge is None:
        return None
    if max(height, width) <= long_edge:
        return None
    if height >= width:
        return long_edge, max(1, round(width * long_edge / height))
    return max(1, round(height * long_edge / width)), long_edge


def _sinc(x: np.ndarray) -> np.ndarray:
    # np.sinc(x) is sin(pi x) / (pi x) with the x=0 singularity handled.
    return np.sinc(x)


def _lanczos3(x: np.ndarray) -> np.ndarray:
    """The kernel: `sinc(x) * sinc(x/3)` inside |x| < 3, zero outside."""
    return np.where(np.abs(x) < 3.0, _sinc(x) * _sinc(x / 3.0), 0.0)


def _weights(src_size: int, dst_size: int) -> tuple[np.ndarray, np.ndarray]:
    """The separable resampling plan for one axis: `(positions, weights)`,
    both `(dst_size, taps)`. Each output sample reads `taps` consecutive
    source samples starting at its row's `left` edge; the tap count is
    fixed per axis (`2 * ceil(radius) + 1`, radius = 3 / scale when
    reducing) so the gather below needs no per-sample loop."""
    scale = dst_size / src_size
    centers = (np.arange(dst_size, dtype=np.float64) + 0.5) / scale - 0.5
    radius = 3.0 / min(scale, 1.0)
    taps = int(np.ceil(radius)) * 2 + 1
    left = np.floor(centers - radius).astype(np.int64)
    positions = left[:, None] + np.arange(taps, dtype=np.int64)[None, :]
    # The kernel argument is scaled so a reduction widens the window over
    # the source samples it covers; without the scale, a downscale would
    # keep sampling a fixed 6-sample window and alias.
    weights = _lanczos3((centers[:, None] - positions) * scale)
    weights = weights / weights.sum(axis=1, keepdims=True)
    positions = np.clip(positions, 0, src_size - 1)
    return positions, weights.astype(np.float32)


def _resize_last_axis(
    image: np.ndarray, positions: np.ndarray, weights: np.ndarray
) -> np.ndarray:
    """One separable pass along an array's last axis, in row bands.

    `image` is `(n, size, channels)` — the caller transposes as needed.
    The gather `image[start:stop][:, positions]` is the memory term:
    `(band, dst, taps, channels)`, bounded by `_BAND` rows per pass. The
    weighted sum is one broadcast `matmul` per band — BLAS does the
    `(1, taps) @ (taps, channels)` contraction per output sample without
    materializing the full product, which the equivalent
    multiply-and-reduce (or an unoptimized `np.einsum`) would."""
    out = np.empty(
        (image.shape[0], positions.shape[0], image.shape[2]),
        dtype=np.float32,
    )
    for start in range(0, image.shape[0], _BAND):
        stop = min(start + _BAND, image.shape[0])
        gathered = image[start:stop][:, positions]
        # (dst, 1, taps) @ (band, dst, taps, channels) -> (band, dst, 1, C)
        out[start:stop] = np.matmul(weights[:, None, :], gathered)[:, :, 0, :]
    return out


def resize_lanczos3(
    image: np.ndarray, out_height: int, out_width: int
) -> np.ndarray:
    """Resamples `image` to `(out_height, out_width)` with Lanczos3,
    separable: the horizontal pass banded over rows, then the vertical
    pass — on the transposed intermediate — banded over its rows (the
    output's columns).

    `image` is `(H, W)` or `(H, W, C)`; the result matches, float32.
    Values are resampled as given — keeping this module encoding-agnostic
    is what lets the render call it on linear values while tests pin the
    kernel on plain ramps."""
    if image.ndim == 2:
        return resize_lanczos3(image[:, :, None], out_height, out_width)[:, :, 0]
    src = np.asarray(image, dtype=np.float32)
    height, width, _ = src.shape
    if out_height > height or out_width > width:
        raise ValueError(
            "resize_lanczos3 is a downsample, never an upscaler: got "
            f"{height}x{width} asked for {out_height}x{out_width}"
        )
    if (height, width) == (out_height, out_width):
        return src.copy()
    positions_x, weights_x = _weights(width, out_width)
    positions_y, weights_y = _weights(height, out_height)

    mid = _resize_last_axis(src, positions_x, weights_x)  # (H, out_w, C)
    narrowed = _resize_last_axis(
        mid.transpose(1, 0, 2), positions_y, weights_y
    )  # (out_w, out_h, C)
    return np.ascontiguousarray(narrowed.transpose(1, 0, 2))