"""Small per-negative previews, generated and rotated by the CLI.

The app's Edit tab shows a preview of each negative; with edits in the
picture, "what the negative looks like" is derived state — the CLI is its
only legitimate source (Python owns every decision), so the CLI generates a
small lossless PNG preview of each published TIFF and rewrites it whenever
an edit changes the rendering. The path is recorded on the negative row and
reported through `roll info`; Swift only displays the file it is told to.

The published TIFF holds **normalized log density** (section 3.11), a
negative in appearance — `val = 0` is the scene highlight, `val = 1` the
scene shadow. The positive preview runs the shared export render
(`render.encode_positive_uint8`): global CMY, invert, Adobe RGB gamma
sandwich, camera matrix when the roll records one, tone and colour ops,
dye separation — at 8-bit and downscaled. The negative view still encodes
raw densities with no tone, colour, or matrix. On top of that flat baseline
the user's nondestructive tone adjustment (`tone.py`, recorded as a `tone` op)
composes a paper-grade contrast curve into the same display encode — a
preview-time judgement aid whose curve the export's render bakes into the
exported pixels at full resolution (`render.py`; the published TIFF is
still never touched). The downscale happens in normalized density
(code space), not linear light, which is correct: averaging density is
what averaging a photographic image means. Uncovered canvas renders black
here, without special-casing: the fill sits at the thin end, so `1 - val`
takes it to zero (section 3.14).

The published TIFF itself is never touched — display encoding lives here
and only here, and this path decodes through
`normalization.decode_normalized`, never through the file's ICC profile
(section 3.12's rule).

A second display mode serves the app's positive/negative toggle: the
**negative view** encodes the normalized density *without* the `1 - val`
inversion — the published TIFF's own appearance, the flat un-inverted
negative, which is what the user looks at when judging densities. The tone
adjustment is deliberately absent from it: grading is a positive-view
judgement aid and would lie about the densities it is meant to illuminate.
Both modes are reachable through the pure-query `edit render-region` and
`edit render-preview` commands; the managed, on-disk preview stays a
positive.

For a 90-degree rotation or a horizontal flip the incremental update is a
lossless pixel transpose or mirror of the cached preview, not a re-decode of
a multi-megapixel TIFF. PNG, not
JPEG: repeated edits would otherwise compound generational loss.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import struct
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np

from scanny_boy import auto_rotate, color, normalization, render, scratches, spots, tone
from scanny_boy import highlight_lock as highlight_lock_module
from scanny_boy.library import repo
from scanny_boy.library.db import library_db_path

# Longest edge of a generated preview, in pixels.
PREVIEW_MAX_EDGE = 1024

MAX_CODE = 65535

# The daemon's decoded-pixel cache is bound by
# total bytes, not entry count, and this is that bound, in one place. One
# entry is a negative's preview-resolution display array — 887x1024 at
# 16-bit RGB, about 5.4 MB — so the bound holds roughly seventeen
# negatives: a roll stepped through in the fit view stays cached, and the
# worst case is bounded well under 1% of the 712 MB a full decode costs.
PREVIEW_CACHE_MAX_BYTES = 96 * 1024 * 1024

_DISPLAY_PREVIEW_CACHE: OrderedDict[tuple, np.ndarray] = OrderedDict()
_DISPLAY_PREVIEW_CACHE_BYTES = 0
_DISPLAY_PREVIEW_CACHE_LOCK = threading.Lock()

# Full-resolution display decode for `render_region`'s slow path. One
# 6000×4000 uint16 RGB array is ~144 MB; the bound holds a handful of
# negatives' full decodes without unbounded growth.
DISPLAY_IMAGE_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024

_DISPLAY_IMAGE_CACHE: OrderedDict[tuple, np.ndarray] = OrderedDict()
_DISPLAY_IMAGE_CACHE_BYTES = 0
_DISPLAY_IMAGE_CACHE_LOCK = threading.Lock()

REGION_RAW_MAGIC = b"SB01"


def _spots_cache_key(spots_params: dict | None) -> tuple:
    """The spot half of the pixel-cache key.

    A live spot set changes decoded pixels — its repair is the first step
    of the display replay — so the whole set is folded into the key,
    hashed rather than compared: the masks can be large and the set is
    bounded (`MAX_SPOTS`), so a hash of its canonical JSON is both cheap
    and exact. Swift's counterpart term is `EditModel.spotsTerm`
    (`repair#count#rejected`), a coarser summary of the same rule; the two
    sites are commented at each other because they cannot share a
    definition — one lives in Swift, one here."""
    if spots_params is None:
        return (None,)
    canonical = json.dumps(spots_params, sort_keys=True, default=str)
    digest = hashlib.blake2b(canonical.encode(), digest_size=16).hexdigest()
    return ("spots", digest)


def _scratches_cache_key(scratches_params: dict | None) -> tuple:
    """The scratches half of the pixel-cache key.

    A live scratches op changes decoded pixels — its correction is the
    first step of the display replay — so the whole set is folded into the
    key, hashed rather than compared.  Swift's counterpart term is
    `EditModel.scratchesTerm` (`enabled#count#stale`), a coarser summary
    of the same rule; the two sites are commented at each other."""
    if scratches_params is None:
        return (None,)
    canonical = json.dumps(scratches_params, sort_keys=True, default=str)
    digest = hashlib.blake2b(canonical.encode(), digest_size=16).hexdigest()
    return ("scratches", digest)


def _display_pixel_cache_key(
    tiff_path: Path,
    quarter_turns: int = 0,
    flipped_horizontally: bool = False,
    fine_angle_deg: float = 0.0,
    spots_params: dict | None = None,
    scratches_params: dict | None = None,
    crop_params: dict | None = None,
) -> tuple:
    stat = os.stat(tiff_path)
    crop_key: tuple | None = None
    if crop_params:
        crop_key = (
            int(crop_params["x"]),
            int(crop_params["y"]),
            int(crop_params["w"]),
            int(crop_params["h"]),
            round(float(crop_params["tilt_deg"]), 4),
        )
    return (
        str(tiff_path),
        stat.st_mtime_ns,
        stat.st_size,
        int(quarter_turns) % 4,
        bool(flipped_horizontally),
        round(float(fine_angle_deg), 6),
        _spots_cache_key(spots_params),
        _scratches_cache_key(scratches_params),
        crop_key,
    )


def cached_preview_codes(
    tiff_path: Path,
    quarter_turns: int = 0,
    flipped_horizontally: bool = False,
    fine_angle_deg: float = 0.0,
    spots_params: dict | None = None,
    scratches_params: dict | None = None,
    crop_params: dict | None = None,
) -> np.ndarray:
    """The display image's preview-resolution density codes — the decoded,
    transformed, downscaled array `generate_preview` and `render_preview`
    encode from — through the daemon's pixel cache.

    A full decode of a published TIFF is ~600
    ms and a one-shot process throws the array away, so the resident
    helper holds the preview-resolution cut instead — the fit view is
    where sliders live, and it only ever needs preview resolution. The key
    is the geometry plus the TIFF's identity, **never the tone or
    colour**: those are LUTs applied after the cached array (<1 ms, §0),
    so a slider drag must hit this cache, and it does. The `mtime` term is
    what catches a re-stitch rewriting the published TIFF (§3.3). Tone,
    colour, and the display mode never reach the key — they are encode
    steps, not decode steps.

    The returned array is shared, read-only by convention: every encode
    path indexes it and never writes it."""
    global _DISPLAY_PREVIEW_CACHE_BYTES

    key = _display_pixel_cache_key(
        tiff_path,
        quarter_turns,
        flipped_horizontally,
        fine_angle_deg,
        spots_params,
        scratches_params,
        crop_params,
    )
    with _DISPLAY_PREVIEW_CACHE_LOCK:
        cached = _DISPLAY_PREVIEW_CACHE.get(key)
        if cached is not None:
            _DISPLAY_PREVIEW_CACHE.move_to_end(key)
            return cached
    image = _downscale_codes(
        _display_image(
            tiff_path,
            quarter_turns,
            flipped_horizontally,
            fine_angle_deg,
            spots_params,
            crop_params,
            scratches_params,
        )
    )
    with _DISPLAY_PREVIEW_CACHE_LOCK:
        _DISPLAY_PREVIEW_CACHE[key] = image
        _DISPLAY_PREVIEW_CACHE_BYTES += image.nbytes
        while (
            _DISPLAY_PREVIEW_CACHE_BYTES > PREVIEW_CACHE_MAX_BYTES
            and len(_DISPLAY_PREVIEW_CACHE) > 1
        ):
            _key, evicted = _DISPLAY_PREVIEW_CACHE.popitem(last=False)
            _DISPLAY_PREVIEW_CACHE_BYTES -= evicted.nbytes
    return image


def cached_display_image(
    tiff_path: Path,
    quarter_turns: int = 0,
    flipped_horizontally: bool = False,
    fine_angle_deg: float = 0.0,
    spots_params: dict | None = None,
    scratches_params: dict | None = None,
    crop_params: dict | None = None,
) -> np.ndarray:
    """The full display-resolution density codes for `render_region`'s slow
    path — decoded and transformed, pre-LUT — through the daemon's full-
    resolution cache. Tone, colour, and display mode stay in the encode
    step, exactly like `cached_preview_codes`."""
    global _DISPLAY_IMAGE_CACHE_BYTES

    key = _display_pixel_cache_key(
        tiff_path,
        quarter_turns,
        flipped_horizontally,
        fine_angle_deg,
        spots_params,
        scratches_params,
        crop_params,
    )
    with _DISPLAY_IMAGE_CACHE_LOCK:
        cached = _DISPLAY_IMAGE_CACHE.get(key)
        if cached is not None:
            _DISPLAY_IMAGE_CACHE.move_to_end(key)
            return cached
    image = _display_image(
        tiff_path,
        quarter_turns,
        flipped_horizontally,
        fine_angle_deg,
        spots_params,
        crop_params,
        scratches_params,
    )
    with _DISPLAY_IMAGE_CACHE_LOCK:
        _DISPLAY_IMAGE_CACHE[key] = image
        _DISPLAY_IMAGE_CACHE_BYTES += image.nbytes
        while (
            _DISPLAY_IMAGE_CACHE_BYTES > DISPLAY_IMAGE_CACHE_MAX_BYTES
            and len(_DISPLAY_IMAGE_CACHE) > 1
        ):
            _key, evicted = _DISPLAY_IMAGE_CACHE.popitem(last=False)
            _DISPLAY_IMAGE_CACHE_BYTES -= evicted.nbytes
    return image


def _downscale_codes(image: np.ndarray) -> np.ndarray:
    """The preview-resolution cut of a display image — the downscale of
    `_write_downscaled`, in normalized density (code space, not linear
    light: averaging density is what averaging a photographic image means)."""
    edge = max(image.shape[0], image.shape[1])
    if edge > PREVIEW_MAX_EDGE:
        scale = PREVIEW_MAX_EDGE / edge
        image = cv2.resize(
            image,
            (round(image.shape[1] * scale), round(image.shape[0] * scale)),
            interpolation=cv2.INTER_AREA,
        )
    return np.ascontiguousarray(image)


def _build_display_lut() -> np.ndarray:
    """uint16 normalized-density code -> uint8 positive display code.

    `decode_normalized` recovers the normalized value, `1 - val` inverts it
    for legibility (the file is a negative; the Edit filmstrip should read
    as one), and the 8-bit encode is bare scaling — no gamma, because
    normalized log density is already roughly perceptually uniform. Values
    beyond the encode's headroom clip here, as everywhere else.
    """
    codes = np.arange(MAX_CODE + 1, dtype=np.float64)
    normalized = normalization.decode_normalized(codes)
    display = np.clip(1.0 - normalized, 0.0, 1.0)
    return np.rint(display * 255).astype(np.uint8)


NORMALIZED_DISPLAY_LUT: np.ndarray = _build_display_lut()

# Bumped when the positive display encode changes incompatibly — old on-disk
# preview PNGs encoded before this version must be regenerated.
POSITIVE_DISPLAY_ENCODE_VERSION = 2


def _build_negative_display_lut() -> np.ndarray:
    """uint16 normalized-density code -> uint8 negative display code.

    The un-inverted view: `decode_normalized`'s value straight to 8-bit
    with no gamma — the published TIFF's own appearance, `val = 0` the
    scene highlight, `val = 1` the scene shadow, the flat negative a
    densitometer would see. The tone adjustment is deliberately not
    composed into it: grading is a positive-view judgement aid, and a
    graded density is not the density.
    """
    codes = np.arange(MAX_CODE + 1, dtype=np.float64)
    normalized = normalization.decode_normalized(codes)
    return np.rint(np.clip(normalized, 0.0, 1.0) * 255).astype(np.uint8)


NEGATIVE_DISPLAY_LUT: np.ndarray = _build_negative_display_lut()

# The display modes `render_region`/`render_preview` understand.
DISPLAY_MODES = ("positive", "negative")


def previews_root() -> Path:
    """Previews sit beside the library database in Application Support."""
    return library_db_path().parent / "previews"


def _preview_path(roll_id: str, negative_id: str) -> Path:
    return previews_root() / roll_id / f"{negative_id}.png"


def _camera_matrix_for_roll(roll_dir: Path, channels: int) -> np.ndarray | None:
    """The roll's export matrix when previewing a colour negative, or `None`
    for mono or when stitch has not yet written `camera_color`."""
    if channels <= 1:
        return None
    from scanny_boy.roll_manifest import load_roll_manifest

    roll = load_roll_manifest(roll_dir)
    return render.camera_matrix_from_roll(roll)


def _write_downscaled(
    image: np.ndarray,
    destination: Path,
    tone_params: dict[str, float] | None = None,
    color_params: dict[str, float] | None = None,
    metering: color.Metering | None = None,
    mode: str = "positive",
    matrix: np.ndarray | None = None,
) -> tuple[int, int]:
    """The downscale (in normalized density — code space, not linear light:
    averaging density is what averaging a photographic image means —
    plus the display encode.
    Returns the written PNG's `(width, height)`."""
    image = _downscale_codes(image)
    _encode_display_png(
        image,
        destination,
        tone_params,
        color_params=color_params,
        metering=metering,
        mode=mode,
        matrix=matrix,
    )
    return image.shape[1], image.shape[0]


def _display_tables(
    tone_params: tone.ToneParams,
    color_params: color.ColorParams,
    metering: color.Metering,
    channels: int,
) -> np.ndarray:
    """Per-channel float display tables, shape `(channels, 65536)`.

    Kept for tests and backward references; preview encode uses
    `render.encode_positive_uint8` instead."""
    return tone.build_channel_tables(tone_params, color_params, metering, channels)


def _encode_display_uint8(
    image: np.ndarray,
    tone_params: dict[str, float] | None = None,
    color_params: dict[str, float] | None = None,
    metering: color.Metering | None = None,
    mode: str = "positive",
    matrix: np.ndarray | None = None,
) -> np.ndarray:
    """16-bit normalized-density (or already-8-bit) RGB -> 8-bit RGB."""
    if mode not in DISPLAY_MODES:
        raise ValueError(f"unknown display mode {mode!r}")
    if image.dtype == np.uint16:
        if mode == "negative":
            image = NEGATIVE_DISPLAY_LUT[image]
        else:
            channels = image.shape[2] if image.ndim == 3 else 1
            encoded = render.encode_positive_uint8(
                image,
                matrix,
                tone_params,
                color_params=color_params,
                metering=metering,
            )
            if channels == 1 and encoded.ndim == 2:
                image = encoded
            else:
                image = encoded
    elif mode == "negative":
        raise ValueError(
            "the negative view must be encoded from the published TIFF's "
            "density codes, not from an already-display-encoded image"
        )
    if image.ndim == 2:
        return np.stack([image] * 3, axis=-1)
    return image


def _encode_display_png(
    image: np.ndarray,
    destination: Path,
    tone_params: dict[str, float] | None = None,
    color_params: dict[str, float] | None = None,
    metering: color.Metering | None = None,
    mode: str = "positive",
    matrix: np.ndarray | None = None,
) -> None:
    """16-bit normalized-density (or already-8-bit) RGB -> 8-bit lossless
    PNG on disk, no downscale. `mode` picks the display encode:
    `"positive"` runs the shared export render (`render.encode_positive_uint8`)
    — camera matrix, tone, and colour ops included when given —
    `"negative"` is the un-inverted density view, which no tone, colour, or
    matrix ever touches. The published TIFF is never touched by any of it."""
    encoded = _encode_display_uint8(
        image,
        tone_params,
        color_params=color_params,
        metering=metering,
        mode=mode,
        matrix=matrix,
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    ok, png = cv2.imencode(".png", cv2.cvtColor(encoded, cv2.COLOR_RGB2BGR))
    if not ok:
        raise ValueError(f"could not encode preview {destination}")
    destination.write_bytes(png.tobytes())


def _encode_display_raw(
    image: np.ndarray,
    destination: Path,
    tone_params: dict[str, float] | None = None,
    color_params: dict[str, float] | None = None,
    metering: color.Metering | None = None,
    mode: str = "positive",
    matrix: np.ndarray | None = None,
) -> None:
    """8-bit display RGB -> raw RGBA region cache (`SB01` header + pixels)."""
    encoded = _encode_display_uint8(
        image,
        tone_params,
        color_params=color_params,
        metering=metering,
        mode=mode,
        matrix=matrix,
    )
    height, width = encoded.shape[:2]
    rgba = np.dstack([encoded, np.full((height, width, 1), 255, dtype=np.uint8)])
    destination.parent.mkdir(parents=True, exist_ok=True)
    header = REGION_RAW_MAGIC + struct.pack("<II", width, height)
    destination.write_bytes(header + rgba.astype(np.uint8).tobytes())


def _encode_display_region(
    image: np.ndarray,
    destination: Path,
    tone_params: dict[str, float] | None = None,
    color_params: dict[str, float] | None = None,
    metering: color.Metering | None = None,
    mode: str = "positive",
    matrix: np.ndarray | None = None,
) -> None:
    """Region caches use raw RGBA; everything else stays PNG."""
    if destination.suffix.lower() == ".rgba":
        _encode_display_raw(
            image,
            destination,
            tone_params,
            color_params=color_params,
            metering=metering,
            mode=mode,
            matrix=matrix,
        )
        return
    _encode_display_png(
        image,
        destination,
        tone_params,
        color_params=color_params,
        metering=metering,
        mode=mode,
        matrix=matrix,
    )


# --- the `crop` op ------------------------------------------------------------


def crop_is_live(crop_params: dict | None, tiff_shape: tuple[int, int]) -> bool:
    """Whether `crop_params` is a live crop against a TIFF of `shape`
    (`(height, width)`). A crop recorded against different canvas
    dimensions — a re-stitch replaced the published TIFF — is stale and
    ignored, the same canvas rule the `spots` op has;
    unlike spots it degrades silently, because a vanished crop changes no
    pixels and `roll info` simply reports no crop."""
    if not crop_params:
        return False
    canvas = crop_params.get("canvas") or (None, None)
    return (canvas[0], canvas[1]) == (tiff_shape[1], tiff_shape[0])


def _crop_matrix(crop_params: dict) -> np.ndarray:
    """The crop warp's matrix: `cv2.getRotationMatrix2D` about the rect's
    centre. Stored `tilt_deg` is the window's counter-clockwise tilt as
    displayed; the image rotates by the negation to straighten the content
    inside the axis-aligned slice — same pattern as `rotate_with_fill`."""
    return cv2.getRotationMatrix2D(
        (
            crop_params["x"] + crop_params["w"] / 2.0,
            crop_params["y"] + crop_params["h"] / 2.0,
        ),
        -float(crop_params["tilt_deg"]),
        1.0,
    )


def apply_crop(image: np.ndarray, crop_params: dict | None) -> np.ndarray:
    """The crop step of the canonical replay: the image warped about the
    stored rect's centre by its tilt (uncovering to the stitching fill
    sentinel, exactly as `auto_rotate.rotate_with_fill` does), then sliced
    to the rect — the output shows the tilted window's content upright,
    with the crop's dimensions. A stale crop (recorded against a different
    canvas — a re-stitch) is ignored, so every caller is safe by default.
    The stored rect is guaranteed inside the canvas (`run_edit_crop`
    clamps it), which the slice below relies on."""
    if not crop_is_live(crop_params, image.shape[:2]):
        return image
    matrix = _crop_matrix(crop_params)
    height, width = image.shape[0], image.shape[1]
    channel_count = 1 if image.ndim == 2 else image.shape[-1]
    warped = cv2.warpAffine(
        image,
        matrix,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=auto_rotate.fill_border_value(channel_count),
    )
    x, y = int(crop_params["x"]), int(crop_params["y"])
    w, h = int(crop_params["w"]), int(crop_params["h"])
    return np.ascontiguousarray(warped[y : y + h, x : x + w])


def display_shape(
    tiff_size: tuple[int, int],  # (height, width)
    *,
    quarter_turns: int,
    crop_params: dict | None,
) -> tuple[int, int]:
    """The display image's `(height, width)`: the published TIFF's own
    dimensions — or a live crop window's output size when cropped — swapped
    when the net quarter turns are odd. The flip and the fine rotation
    never change dimensions."""
    if crop_is_live(crop_params, tiff_size):
        inner_h, inner_w = int(crop_params["h"]), int(crop_params["w"])
    else:
        inner_h, inner_w = tiff_size
    r = (-int(quarter_turns)) % 4
    return (inner_w, inner_h) if r % 2 else (inner_h, inner_w)


def _window_from_tilted_corners(
    corners: list[tuple[float, float]],
) -> tuple[int, int, int, int, float]:
    """Axis-aligned window `(x, y, w, h, tilt_deg)` from four tilted
    pixel-index corners — the centre, edge lengths, and CCW tilt that
    `display_crop_window_to_tiff` and `tiff_crop_window_to_display` share."""
    (x0, y0), (x1, y1), x2y2, (x3, y3) = corners
    centre_x = (x0 + x1 + x2y2[0] + x3) / 4.0
    centre_y = (y0 + y1 + x2y2[1] + y3) / 4.0
    edge_x, edge_y = x1 - x0, y1 - y0
    width = math.hypot(edge_x, edge_y) + 1.0
    height = math.hypot(x3 - x0, y3 - y0) + 1.0
    tilt = -auto_rotate.edge_degrees(edge_y, edge_x)
    while tilt > 45.0:
        tilt -= 90.0
        width, height = height, width
    while tilt <= -45.0:
        tilt += 90.0
        width, height = height, width
    return (
        math.floor(centre_x - width / 2.0 + 0.5),
        math.floor(centre_y - height / 2.0 + 0.5),
        max(round(width), 1),
        max(round(height), 1),
        round(tilt, 2),
    )


def tiff_crop_window_to_display(
    crop_params: dict,
    tiff_size: tuple[int, int],  # (height, width)
    *,
    quarter_turns: int,
    flipped_horizontally: bool,
    fine_angle_deg: float,
) -> tuple[int, int, int, int, float]:
    """The stored TIFF-space crop window as the display-space rect and
    slider tilt the app re-enters crop mode with — the forward map that
    inverts `display_crop_window_to_tiff` on the full uncropped canvas."""
    import cv2

    tiff_h, tiff_w = tiff_size
    stage_h, stage_w = tiff_h, tiff_w
    x, y, w, h = (
        int(crop_params["x"]),
        int(crop_params["y"]),
        int(crop_params["w"]),
        int(crop_params["h"]),
    )
    tilt_deg = float(crop_params["tilt_deg"])
    centre_x, centre_y = x + (w - 1) / 2.0, y + (h - 1) / 2.0
    corners = [
        (float(px), float(py))
        for px, py in (
            (x, y),
            (x + w - 1, y),
            (x + w - 1, y + h - 1),
            (x, y + h - 1),
        )
    ]
    if abs(tilt_deg) >= 1e-9:
        tilt_matrix = cv2.getRotationMatrix2D(
            (centre_x, centre_y), float(tilt_deg), 1.0
        )
        corners = [
            (
                tilt_matrix[0, 0] * px + tilt_matrix[0, 1] * py + tilt_matrix[0, 2],
                tilt_matrix[1, 0] * px + tilt_matrix[1, 1] * py + tilt_matrix[1, 2],
            )
            for px, py in corners
        ]
    if flipped_horizontally:
        corners = [(stage_w - 1 - px, py) for px, py in corners]
    if abs(fine_angle_deg) >= 1e-9:
        matrix = cv2.getRotationMatrix2D(
            (stage_w / 2.0, stage_h / 2.0), -float(fine_angle_deg), 1.0
        )
        corners = [
            (
                matrix[0, 0] * px + matrix[0, 1] * py + matrix[0, 2],
                matrix[1, 0] * px + matrix[1, 1] * py + matrix[1, 2],
            )
            for px, py in corners
        ]
    r = (-int(quarter_turns)) % 4
    if r == 1:
        corners = [(py, stage_w - 1 - px) for px, py in corners]
    elif r == 2:
        corners = [(stage_w - 1 - px, stage_h - 1 - py) for px, py in corners]
    elif r == 3:
        corners = [(stage_h - 1 - py, px) for px, py in corners]
    return _window_from_tilted_corners(corners)


def display_crop_params(
    crop_params: dict | None,
    tiff_size: tuple[int, int],  # (height, width)
    *,
    quarter_turns: int,
    flipped_horizontally: bool,
    fine_angle_deg: float,
) -> dict | None:
    """The stored TIFF-space crop as params for `apply_crop` on the full
    uncropped display image — the last replay step, matching crop mode's
    uncropped display plus slider tilt."""
    if not crop_params or not crop_is_live(crop_params, tiff_size):
        return None
    x, y, w, h, display_tilt = tiff_crop_window_to_display(
        crop_params,
        tiff_size,
        quarter_turns=quarter_turns,
        flipped_horizontally=flipped_horizontally,
        fine_angle_deg=fine_angle_deg,
    )
    canvas_h, canvas_w = display_shape(
        tiff_size, quarter_turns=quarter_turns, crop_params=None
    )
    result: dict = {
        "canvas": [canvas_w, canvas_h],
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "tilt_deg": display_tilt,
    }
    preset = crop_params.get("preset")
    if preset is not None:
        result["preset"] = preset
    return result


def crop_report(
    crop_params: dict | None,
    tiff_size: tuple[int, int],  # (height, width)
    *,
    quarter_turns: int,
    flipped_horizontally: bool = False,
    fine_angle_deg: float = 0.0,
) -> dict | None:
    """The net crop as `roll info` and `edit_recorded` report it, in
    display space — the cropped image's final dimensions (quarter turns
    folded in, the only transform that changes them) plus the display
    slider tilt and preset label for the sidebar. When a live crop exists,
    the report also carries the crop window's origin and the full
    uncropped display canvas so the app can re-enter crop mode on the
    whole frame with the saved rect superimposed."""
    if not crop_params:
        return None
    height, width = display_shape(
        tiff_size, quarter_turns=quarter_turns, crop_params=crop_params
    )
    canvas_h, canvas_w = display_shape(
        tiff_size, quarter_turns=quarter_turns, crop_params=None
    )
    x, y, _w, _h, display_tilt = tiff_crop_window_to_display(
        crop_params,
        tiff_size,
        quarter_turns=quarter_turns,
        flipped_horizontally=flipped_horizontally,
        fine_angle_deg=fine_angle_deg,
    )
    return {
        "width": width,
        "height": height,
        "tilt_deg": display_tilt,
        "preset": crop_params.get("preset"),
        "x": x,
        "y": y,
        "canvas_width": canvas_w,
        "canvas_height": canvas_h,
    }


def display_crop_window_to_tiff(
    rect: tuple[int, int, int, int],  # display space: (x, y, w, h)
    tiff_size: tuple[int, int],  # (height, width)
    *,
    tilt_deg: float,
    quarter_turns: int,
    flipped_horizontally: bool,
    fine_angle_deg: float,
    crop_params: dict | None,
    full_frame: bool = False,
) -> tuple[int, int, int, int, float]:
    """A display-space crop — the axis-aligned rect `rect` the user drew
    over the image as it currently renders (live crop included), tilted
    `tilt_deg` counter-clockwise about its own centre — as the
    fully-composed tilted window in published-TIFF pixels the `crop` op
    stores: `(x, y, w, h, tilt_deg)`.

    The *tilted* rect's four corners are mapped backwards through the
    canonical replay (quarter turns, then the fine rotation, then the
    flip, then the live crop's own warp) into TIFF space. The mapped
    corners *are* the new window: an axis-aligned rect rotated by the
    total tilt about its centre, whose centre, side lengths, and edge
    direction become the stored rect and `tilt_deg` — so a re-crop
    composes into one window and the replay never has to. The returned
    tilt is counter-clockwise as displayed, normalized to (-45, 45].

    When `full_frame` is true, `rect` is on the full uncropped display
    canvas (the same space `crop_report`'s `x`/`y`/`canvas_*` name) and
    the live crop's inverse warp is skipped — the app re-enters crop mode
    on the whole frame with the saved window superimposed.
    """
    import cv2

    tiff_h, tiff_w = tiff_size
    x, y, w, h = rect
    live = crop_is_live(crop_params, (tiff_h, tiff_w)) and not full_frame
    stage_h, stage_w = (
        (int(crop_params["h"]), int(crop_params["w"]))
        if live
        else (tiff_h, tiff_w)
    )
    r = (-int(quarter_turns)) % 4
    centre_x, centre_y = x + (w - 1) / 2.0, y + (h - 1) / 2.0
    corners = [
        (float(px), float(py))
        for px, py in (
            (x, y),
            (x + w - 1, y),
            (x + w - 1, y + h - 1),
            (x, y + h - 1),
        )
    ]
    # 0. The drawn tilt, in display space: the rect's corners rotate
    # counter-clockwise about its centre (cv2's positive angles turn
    # counter-clockwise, matching the tilt convention).
    if abs(tilt_deg) >= 1e-9:
        tilt_matrix = cv2.getRotationMatrix2D(
            (centre_x, centre_y), float(tilt_deg), 1.0
        )
        corners = [
            (
                tilt_matrix[0, 0] * px + tilt_matrix[0, 1] * py + tilt_matrix[0, 2],
                tilt_matrix[1, 0] * px + tilt_matrix[1, 1] * py + tilt_matrix[1, 2],
            )
            for px, py in corners
        ]
    mapped: list[tuple[float, float]] = []
    for px, py in corners:
        # 1. Quarter turns, inverted (the display point's row/col first).
        i, j = _display_point_to_tiff(py, px, stage_h, stage_w, r)
        # 2. The fine rotation, inverted: the forward map is the
        # `-fine_angle_deg` matrix, so the inverse carries `+fine`.
        if abs(fine_angle_deg) >= 1e-9:
            matrix = cv2.getRotationMatrix2D(
                (stage_w / 2.0, stage_h / 2.0), float(fine_angle_deg), 1.0
            )
            j, i = (
                matrix[0, 0] * j + matrix[0, 1] * i + matrix[0, 2],
                matrix[1, 0] * j + matrix[1, 1] * i + matrix[1, 2],
            )
        # 3. The mirror, inverted (its own inverse).
        if flipped_horizontally:
            j = stage_w - 1 - j
        # 4. The live crop's warp, inverted: the crop's output point maps
        # to the warped canvas at `point + origin`, whose TIFF source the
        # crop matrix names.
        if live:
            matrix = _crop_matrix(crop_params)
            wx, wy = j + crop_params["x"], i + crop_params["y"]
            j, i = (
                matrix[0, 0] * wx + matrix[0, 1] * wy + matrix[0, 2],
                matrix[1, 0] * wx + matrix[1, 1] * wy + matrix[1, 2],
            )
        mapped.append((j, i))

    return _window_from_tilted_corners(mapped)


def _display_image(
    tiff_path: Path,
    quarter_turns: int = 0,
    flipped_horizontally: bool = False,
    fine_angle_deg: float = 0.0,
    spots_params: dict | None = None,
    crop_params: dict | None = None,
    scratches_params: dict | None = None,
) -> np.ndarray:
    """The published TIFF's full display image — the net transform replayed
    in canonical order (scratch correction, then spot repair, then the
    mirror, then the fine rotation's warp with the fill sentinel, then the
    quarter turns, then the crop on the uncropped display canvas) — the
    pixels `generate_preview`, `render_preview`, and `render_region`'s
    exact path all work from. uint16 RGB in density codes, like the TIFF.

    Scratch and spot ops run in TIFF space before any geometry. The stored
    crop window is TIFF space in the ops log; replay converts it to
    display space via `display_crop_params` and applies it last, matching
    crop mode's uncropped display plus slider tilt."""
    import tifffile

    image = _promote_to_rgb(tifffile.imread(tiff_path))
    tiff_size = (image.shape[0], image.shape[1])
    image = scratches.apply(image, scratches_params)
    image = spots.apply_repair(image, spots_params)
    if flipped_horizontally:
        image = np.ascontiguousarray(image[:, ::-1])
    if abs(fine_angle_deg) >= 1e-9:
        image = auto_rotate.rotate_with_fill(image, fine_angle_deg)
    if quarter_turns % 4:
        # np.rot90 turns counter-clockwise; the count is net clockwise
        # quarter turns.
        image = np.ascontiguousarray(np.rot90(image, k=(-quarter_turns) % 4))
    return apply_crop(
        image,
        display_crop_params(
            crop_params,
            tiff_size,
            quarter_turns=quarter_turns,
            flipped_horizontally=flipped_horizontally,
            fine_angle_deg=fine_angle_deg,
        ),
    )


def generate_preview(
    roll_dir: Path,
    roll_id: str,
    negative,
    quarter_turns: int = 0,
    flipped_horizontally: bool = False,
    fine_angle_deg: float = 0.0,
    tone_params: dict[str, float] | None = None,
    color_params: dict[str, float] | None = None,
    metering: color.Metering | None = None,
    spots_params: dict | None = None,
    scratches_params: dict | None = None,
    crop_params: dict | None = None,
) -> Path | None:
    """A preview of `negative`'s published TIFF with the negative's net
    transform applied — the published TIFF itself never carries edits, so
    the transform is folded in here. The canonical replay mirrors the
    original horizontally first (when flipped), then applies the ops log's
    fine auto-rotation (a warp with the stitching fill sentinel in the
    uncovered pixels, `auto_rotate.rotate_with_fill`), then rotates; the
    fine angle is negated by a flip exactly as `repo.net_edit_state`'s
    replay says, so the caller passes the canonical angle through
    untouched. `tone_params` is the net `tone` op's full param dict (None =
    the default print curve), composed into the display LUT. The net `crop` op's
    window (`crop_params`, TIFF space in the ops log) is replayed last on
    the uncropped display canvas — which is why a live crop's window is
    what `display_shape` sizes.
    Returns the preview path, or None when the negative has no published
    output to preview."""
    if negative.output is None:
        return None

    tiff_path = Path(roll_dir) / negative.output["name"]
    image = cached_preview_codes(
        tiff_path,
        quarter_turns,
        flipped_horizontally,
        fine_angle_deg,
        spots_params,
        scratches_params,
        crop_params,
    )
    channels = image.shape[2] if image.ndim == 3 else 1
    matrix = _camera_matrix_for_roll(roll_dir, channels)

    destination = _preview_path(roll_id, negative.negative_id)
    _encode_display_png(
        image,
        destination,
        tone_params,
        color_params=color_params,
        metering=metering,
        matrix=matrix,
    )
    return destination


def render_preview(
    tiff_path: Path,
    destination: Path,
    *,
    quarter_turns: int = 0,
    flipped_horizontally: bool = False,
    fine_angle_deg: float = 0.0,
    mode: str = "positive",
    tone_params: dict[str, float] | None = None,
    color_params: dict[str, float] | None = None,
    metering: color.Metering | None = None,
    spots_params: dict | None = None,
    scratches_params: dict | None = None,
    crop_params: dict | None = None,
    matrix: np.ndarray | None = None,
) -> tuple[int, int]:
    """The whole display image in the requested display mode, downscaled to
    `PREVIEW_MAX_EDGE`, written to a caller-named path — the pure-query
    sibling of `generate_preview` backing the app's positive/negative
    toggle (`edit render-preview`): nothing is recorded, the TIFF is
    untouched. The transform replays exactly as `generate_preview`'s does,
    the net crop window included.
    `mode` is `"positive"` (the inverted look, always what the managed
    on-disk preview holds; `tone_params` — the net `tone` op's
    `{"grade_r", "snap_gamma"}` — composes into its LUT exactly as it does
    there) or `"negative"` (the un-inverted density view, which no tone
    ever reaches — `tone_params` is ignored in that mode). Returns the
    written PNG's `(width, height)`."""
    image = cached_preview_codes(
        tiff_path,
        quarter_turns,
        flipped_horizontally,
        fine_angle_deg,
        spots_params,
        scratches_params,
        crop_params,
    )
    _encode_display_png(
        image,
        destination,
        tone_params,
        color_params=color_params,
        metering=metering,
        mode=mode,
        matrix=matrix,
    )
    return image.shape[1], image.shape[0]


def transform_preview(current_path: Path, op: str) -> Path:
    """One lossless transform of the cached preview, in place: `op` is one
    of `"cw"`, `"ccw"`, or `"flip"` — the op just appended to the log,
    applied to pixels that already reflect every earlier one."""
    image = cv2.imread(str(current_path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"could not read preview {current_path}")
    # cv2 is BGR but a transpose or mirror is channel-agnostic.
    # np.rot90 turns counter-clockwise, so cw is k=3.
    if op == "flip":
        transformed = np.ascontiguousarray(image[:, ::-1])
    else:
        transformed = np.rot90(image, k=3 if op == "cw" else 1)
    ok, encoded = cv2.imencode(".png", transformed)
    if not ok:
        raise ValueError(f"could not encode preview {current_path}")
    current_path.write_bytes(encoded.tobytes())
    return current_path


# --- 1:1 region rendering ----------------------------------------------------
# A display-space rectangle: (x, y, width, height) — display space is the
# published TIFF's pixels with the net rotation folded in, i.e. what the
# app shows. This matches the canvas-space rect convention of
# `layout.largest_valid_rect` and `composite(region=...)`.
Region = tuple[int, int, int, int]


def _read_tiff_dimensions(tiff_path: Path) -> tuple[int, int]:
    """`(height, width)` from the TIFF header alone, no pixel decoding."""
    import tifffile

    with tifffile.TiffFile(tiff_path) as tif:
        page = tif.pages[0]
        return int(page.imagelength), int(page.imagewidth)


def _display_point_to_tiff(
    i: int, j: int, tiff_h: int, tiff_w: int, r: int
) -> tuple[int, int]:
    """Display point (row, col) -> TIFF point (row, col), where the display
    image is `np.rot90(tiff, k=r)`.

    From `np.rot90`'s index algebra, for a display of shape
    (DH, DW) over a TIFF of shape (tiff_h, tiff_w):
    r=0 keeps (i, j); r=1 (one CCW turn) reads tiff[j, tiff_w-1-i];
    r=2 reads tiff[tiff_h-1-i, tiff_w-1-j]; r=3 (one CW turn) reads
    tiff[tiff_h-1-j, i].
    """
    if r == 0:
        return i, j
    if r == 1:
        return j, tiff_w - 1 - i
    if r == 2:
        return tiff_h - 1 - i, tiff_w - 1 - j
    return tiff_h - 1 - j, i  # r == 3


def tiff_rect_to_display(
    rect: tuple[int, int, int, int],
    tiff_size: tuple[int, int],  # (height, width)
    *,
    quarter_turns: int,
    flipped_horizontally: bool,
    fine_angle_deg: float,
    crop_params: dict | None = None,
) -> tuple[int, int, int, int]:
    """A TIFF-space `(x, y, width, height)` rect as the display-space rect
    the app draws markers over — the exact forward map of
    `_display_image`'s canonical order on the rect's four corners: the
    live crop's warp (its inverse rotation, then the window offset), the
    mirror, the fine rotation (the same matrix `auto_rotate.rotate_with_fill`
    builds, negated angle and all), then the quarter turns; the
    axis-aligned bounding box of the mapped corners (floor the minimum,
    ceil the maximum), clamped to the display bounds — which are the live
    crop window's dimensions when a crop is present. A box under a fine
    rotation grows slightly — correct behaviour for a review marker, not a
    bug to fix.

    The CLI converts; Swift never does. Every command and query reports
    spots in display space, already transformed."""
    import cv2

    tiff_h, tiff_w = tiff_size
    x, y, w, h = rect
    live_crop = crop_is_live(crop_params, (tiff_h, tiff_w))
    # The crop is the replay's first geometric step, so with a live crop
    # the mirror/fine/turn stages operate on the window's dimensions.
    stage_h, stage_w = (
        (int(crop_params["h"]), int(crop_params["w"]))
        if live_crop
        else (tiff_h, tiff_w)
    )
    corners = [
        (float(px), float(py))
        for px, py in (
            (x, y),
            (x + w - 1, y),
            (x + w - 1, y + h - 1),
            (x, y + h - 1),
        )
    ]
    # 0. The live crop, forward: the crop output point is the inverse
    # rotation of the TIFF point, minus the window's origin.
    if live_crop:
        inverse = cv2.getRotationMatrix2D(
            (
                crop_params["x"] + crop_params["w"] / 2.0,
                crop_params["y"] + crop_params["h"] / 2.0,
            ),
            float(crop_params["tilt_deg"]),
            1.0,
        )
        ox, oy = float(crop_params["x"]), float(crop_params["y"])
        corners = [
            (
                inverse[0, 0] * px + inverse[0, 1] * py + inverse[0, 2] - ox,
                inverse[1, 0] * px + inverse[1, 1] * py + inverse[1, 2] - oy,
            )
            for px, py in corners
        ]
    # 1. Mirror, when flipped.
    if flipped_horizontally:
        corners = [(stage_w - 1 - px, py) for px, py in corners]
    # 2. The fine rotation, about the canvas center, negated angle — the
    # same matrix `rotate_with_fill` builds.
    if abs(fine_angle_deg) >= 1e-9:
        matrix = cv2.getRotationMatrix2D(
            (stage_w / 2.0, stage_h / 2.0), -fine_angle_deg, 1.0
        )
        corners = [
            (
                matrix[0, 0] * px + matrix[0, 1] * py + matrix[0, 2],
                matrix[1, 0] * px + matrix[1, 1] * py + matrix[1, 2],
            )
            for px, py in corners
        ]
    # 3. Quarter turns (the inverse of `_display_point_to_tiff`'s case
    # table). r = the counter-clockwise turn count np.rot90 applies.
    r = (-int(quarter_turns)) % 4
    if r == 1:
        corners = [(py, stage_w - 1 - px) for px, py in corners]
    elif r == 2:
        corners = [(stage_w - 1 - px, stage_h - 1 - py) for px, py in corners]
    elif r == 3:
        corners = [(stage_h - 1 - py, px) for px, py in corners]

    xs = [px for px, _ in corners]
    ys = [py for _, py in corners]
    # Odd net turns swap the display dimensions.
    display_w, display_h = (stage_w, stage_h) if r % 2 == 0 else (stage_h, stage_w)
    # The corners are pixel indices, so the bounding rect's far edges are
    # one past the extreme corner: floor the minimum, ceil the maximum
    # *plus one* — which is what makes an unrotated rect come back exact.
    x0 = min(max(math.floor(min(xs)), 0), display_w)
    y0 = min(max(math.floor(min(ys)), 0), display_h)
    x1 = min(max(math.ceil(max(xs)) + 1, 0), display_w)
    y1 = min(max(math.ceil(max(ys)) + 1, 0), display_h)
    return x0, y0, max(x1 - x0, 0), max(y1 - y0, 0)


def _clamp_display_region(
    x: int, y: int, width: int, height: int, display_h: int, display_w: int
) -> tuple[int, int, int, int]:
    """Intersect the requested display-space rect with the image bounds.

    Raises `ValueError` when the intersection is empty — the app only asks
    for regions it is currently showing, so an empty one is a bug there,
    not a clamping case."""
    if width <= 0 or height <= 0:
        raise ValueError(f"region must have positive size, got {width}x{height}")
    x0 = min(max(x, 0), display_w)
    y0 = min(max(y, 0), display_h)
    x1 = min(max(x + width, 0), display_w)
    y1 = min(max(y + height, 0), display_h)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(
            f"region {x}x{y}+{width}+{height} is empty against a "
            f"{display_w}x{display_h} image"
        )
    return x0, y0, x1 - x0, y1 - y0


def _read_tiff_region(
    tiff_path: Path, tiff_rect: tuple[int, int, int, int]
) -> np.ndarray:
    """The TIFF's pixels for a TIFF-space rect `(x, y, w, h)`, decoded
    through tifffile's own codec pipeline — deflate strips with horizontal
    prediction — for just the strips the rect overlaps, then full-image
    decode as a fallback if a strip-level read ever misbehaves."""
    import tifffile

    x, y, w, h = tiff_rect
    try:
        with tifffile.TiffFile(tiff_path) as tif:
            page = tif.pages[0]
            page_w = int(page.imagewidth)
            rows_per_strip = int(page.rowsperstrip or int(page.imagelength))
            first_strip = y // rows_per_strip
            last_strip = (y + h - 1) // rows_per_strip
            filehandle = tif.filehandle
            parts = []
            for strip in range(first_strip, last_strip + 1):
                filehandle.seek(int(page.dataoffsets[strip]))
                raw = filehandle.read(int(page.databytecounts[strip]))
                decoded, _shape, _dtype = page.decode(raw, strip)
                parts.append(
                    np.asarray(decoded).reshape(-1, page_w, page.samplesperpixel)
                )
            strip_image = parts[0] if len(parts) == 1 else np.concatenate(parts)
            return strip_image[y - first_strip * rows_per_strip :][0:h, x : x + w]
    except Exception:  # noqa: BLE001 — the strip path is an optimization
        image = tifffile.imread(tiff_path)
        return image[y : y + h, x : x + w]


def _promote_to_rgb(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return np.stack([image] * 3, axis=-1)
    if image.shape[2] == 4:
        return image[:, :, :3]
    return image


def render_region(
    tiff_path: Path,
    x: int,
    y: int,
    width: int,
    height: int,
    quarter_turns: int = 0,
    flipped_horizontally: bool = False,
    fine_angle_deg: float = 0.0,
    tone_params: dict[str, float] | None = None,
    color_params: dict[str, float] | None = None,
    metering: color.Metering | None = None,
    destination: Path | None = None,
    mode: str = "positive",
    spots_params: dict | None = None,
    scratches_params: dict | None = None,
    crop_params: dict | None = None,
    matrix: np.ndarray | None = None,
) -> Region:
    """Encode the published TIFF's `(x, y, width, height)` display-space
    region as a lossless 1:1 PNG — display space is the TIFF with the net
    transform folded in, exactly as `generate_preview` shows it: mirrored
    horizontally (when flipped), then fine-rotated (the auto-seeded
    `rotate_fine` angle, a warp about the canvas center with the fill
    sentinel in the uncovered pixels), then rotated, then the live crop's
    window (a tilted warp, `apply_crop`) — and the encode is the
    8-bit display LUT named by `mode` (the inverted positive, with the net
    `tone` op composed in when `tone_params` is given — or the un-inverted
    negative, which no tone reaches) with no downscale. A live crop's
    presence is what sizes the display bounds: the region is asked for in
    cropped display space, the same space `roll info`'s crop report names.

    The region is clamped against the image bounds; the returned `Region`
    is the rect actually rendered, post-clamp. Cropping first and
    transforming the crop is exact for axis-aligned rects, so the same
    pixels come back as a full decode would give —
    `test_render_region_matches_full_decode` holds that equivalence, and
    the strip-level reader (only the strips overlapping the rect are
    decoded, since the published TIFF is strip-compressed, not tiled) is
    held to the full read as well. The fine rotation's warp interpolates
    across the crop boundary, the spot repair's inpainting differs between
    a crop and the whole image, and a live crop's own warp interpolates —
    any of the three takes the exact path instead: decode the whole TIFF,
    replay the full transform on it, and slice the rect out of the result
    — the same pixels the preview shows.
    """
    if mode not in DISPLAY_MODES:
        raise ValueError(f"unknown display mode {mode!r}")
    debug_timing = os.environ.get("SCANNY_BOY_DEBUG_TIMING") == "1"
    if debug_timing:
        _timing_start = time.perf_counter()
    tiff_h, tiff_w = _read_tiff_dimensions(tiff_path)
    display_h, display_w = display_shape(
        (tiff_h, tiff_w), quarter_turns=quarter_turns, crop_params=crop_params
    )
    dx, dy, dw, dh = _clamp_display_region(x, y, width, height, display_h, display_w)

    if (
        abs(fine_angle_deg) >= 1e-9
        or crop_is_live(crop_params, (tiff_h, tiff_w))
        or spots.is_repairing(spots_params, (tiff_h, tiff_w))
    ):
        # The fine warp interpolates across its source's boundaries, the
        # crop warp does the same, and inpainting a crop uses different
        # surroundings than inpainting the whole image — crop-then-transform
        # is no longer exact: replay the transform on the full decode, the
        # way `generate_preview` does, then slice.
        image = cached_display_image(
            tiff_path,
            quarter_turns,
            flipped_horizontally,
            fine_angle_deg,
            spots_params,
            scratches_params,
            crop_params,
        )
        if destination is not None:
            _encode_display_region(
                image[dy : dy + dh, dx : dx + dw],
                destination,
                tone_params,
                color_params=color_params,
                metering=metering,
                mode=mode,
                matrix=matrix,
            )
        if debug_timing:
            print(
                f"render_region slow {dw}x{dh} took "
                f"{time.perf_counter() - _timing_start:.3f}s",
                file=sys.stderr,
            )
        return dx, dy, dw, dh

    # Invert the display transform on the clamped rect: the display image
    # is `rot90(mirror(tiff), k=r)`, so the rect corners map through the
    # inverse rotation into mirrored-tiff space (`_display_point_to_tiff`),
    # and the mirror — its own inverse — flips the column bounds back into
    # tiff space. (No live crop reaches this path: its warp would not be
    # exact, and that case takes the full-decode branch above.)
    r = (-int(quarter_turns)) % 4
    corners = (
        _display_point_to_tiff(dy, dx, tiff_h, tiff_w, r),
        _display_point_to_tiff(dy + dh - 1, dx + dw - 1, tiff_h, tiff_w, r),
    )
    ty0 = min(corners[0][0], corners[1][0])
    ty1 = max(corners[0][0], corners[1][0]) + 1
    tx0 = min(corners[0][1], corners[1][1])
    tx1 = max(corners[0][1], corners[1][1]) + 1
    if flipped_horizontally:
        tx0, tx1 = tiff_w - tx1, tiff_w - tx0

    live_scratches = scratches.is_live(scratches_params, (tiff_h, tiff_w))
    if live_scratches:
        tx0_exp = max(0, tx0 - scratches.REGION_COL_MARGIN)
        ty0_exp = max(0, ty0 - scratches.REGION_ROW_MARGIN)
        tx1_exp = min(tiff_w, tx1 + scratches.REGION_COL_MARGIN)
        ty1_exp = min(tiff_h, ty1 + scratches.REGION_ROW_MARGIN)
        inner_x = tx0 - tx0_exp
        inner_y = ty0 - ty0_exp
        crop = _read_tiff_region(
            tiff_path, (tx0_exp, ty0_exp, tx1_exp - tx0_exp, ty1_exp - ty0_exp)
        )
        crop = scratches.apply(
            crop,
            scratches_params,
            region=(inner_x, inner_y, tx1 - tx0, ty1 - ty0),
            origin=(tx0_exp, ty0_exp),
        )
    else:
        crop = _read_tiff_region(tiff_path, (tx0, ty0, tx1 - tx0, ty1 - ty0))
    if flipped_horizontally:
        crop = np.ascontiguousarray(crop[:, ::-1])
    if r:
        crop = np.ascontiguousarray(np.rot90(crop, k=r))
    if destination is not None:
        _encode_display_region(
            _promote_to_rgb(crop),
            destination,
            tone_params,
            color_params=color_params,
            metering=metering,
            mode=mode,
            matrix=matrix,
        )
    if debug_timing:
        print(
            f"render_region fast {dw}x{dh} took "
            f"{time.perf_counter() - _timing_start:.3f}s",
            file=sys.stderr,
        )
    return dx, dy, dw, dh


# tone and color ops never route through the lossless incremental path —
# an 8-bit PNG cannot be re-curved or re-coloured losslessly. The spots op
# joins them: a repair changes pixels, and the incremental path is
# lossless-geometry only. The crop op joins too — its
# window changes which pixels exist, and a tilted window is a warp.
PREVIEW_OPS = {"cw", "ccw", "flip"}
_STATE_PREVIEW_OPS = {repo.TONE_OP, repo.COLOR_OP, repo.SPOTS_OP, repo.SCRATCHES_OP, repo.CROP_OP}


def ensure_preview(
    roll_dir: Path,
    roll_id: str,
    negative,
    op: str | None = None,
    *,
    highlight_lock: dict | None = None,
) -> Path | None:
    """The preview path `negative` should display after `op`
    (None meaning: make sure one exists).

    - No preview yet: generate one from the published TIFF with the ops
      log's *net* state applied — the incremental `op` is already in that
      net, and the cache may have been lost several edits ago, so
      regenerating with only the latest op would lie.
    - Preview exists and a geometric op came in: transform the cached
      preview, which already reflects every earlier edit.
    - Preview exists and the `tone` op came in: regenerate — the tone
      curve lives in the display encode, which the cached PNG has already
      been through. The `color` op likewise; the `spots` op likewise — a
      repair changes pixels, and the incremental transform path is
      lossless-geometry only.
    - Preview exists, no op: leave it alone.

    `highlight_lock` is the roll's manifest `highlight_lock` block (a plain
    dict, as stored — `None` on a mono roll or a roll with no lock), passed
    through to `color.read_metering` for the ROLL_HIGHLIGHT_LOCK correction.
    """
    if op is not None and op not in PREVIEW_OPS and op not in _STATE_PREVIEW_OPS:
        raise ValueError(f"unknown preview op {op!r}")
    lock = highlight_lock_module.HighlightLock.from_dict(highlight_lock)
    if negative.preview_path is None or not Path(negative.preview_path).exists():
        state = repo.net_edit_state(roll_dir, negative.negative_id)
        meter = color.read_metering(negative.normalization, highlight_lock=lock)
        return generate_preview(
            roll_dir,
            roll_id,
            negative,
            quarter_turns=state.quarter_turns,
            flipped_horizontally=state.flipped,
            fine_angle_deg=state.fine_angle_deg,
            tone_params=state.tone,
            color_params=state.color,
            metering=meter,
            spots_params=state.spots,
            scratches_params=state.scratches,
            crop_params=state.crop,
        )
    state = repo.net_edit_state(roll_dir, negative.negative_id)
    if op is not None:
        if op in _STATE_PREVIEW_OPS:
            meter = color.read_metering(negative.normalization, highlight_lock=lock)
            return generate_preview(
                roll_dir,
                roll_id,
                negative,
                quarter_turns=state.quarter_turns,
                flipped_horizontally=state.flipped,
                fine_angle_deg=state.fine_angle_deg,
                tone_params=state.tone,
                color_params=state.color,
                metering=meter,
                spots_params=state.spots,
                scratches_params=state.scratches,
                crop_params=state.crop,
            )
        return transform_preview(Path(negative.preview_path), op)
    # Untoned negatives may still carry preview PNGs from the old flat
    # encode — regenerate so the default print curve shows on disk too.
    if state.tone is None:
        meter = color.read_metering(negative.normalization, highlight_lock=lock)
        return generate_preview(
            roll_dir,
            roll_id,
            negative,
            quarter_turns=state.quarter_turns,
            flipped_horizontally=state.flipped,
            fine_angle_deg=state.fine_angle_deg,
            tone_params=state.tone,
            color_params=state.color,
            metering=meter,
            spots_params=state.spots,
            scratches_params=state.scratches,
            crop_params=state.crop,
        )
    return Path(negative.preview_path)


def sync_previews(
    roll_dir: Path,
    manifest,
    published_outputs: list[str] | None = None,
    *,
    force: bool = False,
) -> None:
    """Generate previews for completed negatives that lack one, regenerate
    the ones whose pixels this run replaced, and record the paths.

    `published_outputs` names the output files the caller just published.
    A re-stitch adopts an existing negative — same id, same preview path,
    brand-new TIFF — so a cached preview must not survive that: it would
    show the old pixels. Regenerated previews carry the ops log's net
    rotation, since the published TIFF never does.

    `force` regenerates every completed negative's preview regardless of
    `published_outputs` — docs/ROLL_HIGHLIGHT_LOCK.md §5: when the roll's
    highlight-colour lock changes, a negative this run never touched can
    still render differently, and its cached preview PNG (unlike the LUT
    step that builds it) does not pick that up on its own."""
    published = set(published_outputs or [])
    changed = False
    for negative in manifest.negatives:
        if negative.status != "completed" or negative.output is None:
            continue
        has_preview = negative.preview_path and Path(negative.preview_path).exists()
        state = repo.net_edit_state(roll_dir, negative.negative_id)
        needs_default_encode_refresh = has_preview and state.tone is None
        if (
            not force
            and has_preview
            and negative.output["name"] not in published
            and not needs_default_encode_refresh
        ):
            continue
        meter = color.read_metering(
            negative.normalization,
            highlight_lock=highlight_lock_module.HighlightLock.from_dict(
                manifest.highlight_lock
            ),
        )
        preview = generate_preview(
            roll_dir,
            manifest.roll_id,
            negative,
            quarter_turns=state.quarter_turns,
            flipped_horizontally=state.flipped,
            fine_angle_deg=state.fine_angle_deg,
            tone_params=state.tone,
            color_params=state.color,
            metering=meter,
            spots_params=state.spots,
            scratches_params=state.scratches,
            crop_params=state.crop,
        )
        if preview is not None:
            negative.preview_path = str(preview)
            changed = True
    if changed:
        from scanny_boy.roll_manifest import write_roll_manifest

        write_roll_manifest(roll_dir, manifest)


def transforms_for(
    manifest, roll_dir: Path
) -> dict[str, repo.EditState]:
    """Net state per negative id — for `roll info` augmentation."""
    return {
        negative.negative_id: repo.net_edit_state(roll_dir, negative.negative_id)
        for negative in manifest.negatives
    }
