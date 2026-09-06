"""Small per-negative previews, generated and rotated by the CLI.

The app's Edit tab shows a preview of each negative; with edits in the
picture, "what the negative looks like" is derived state — the CLI is its
only legitimate source (Python owns every decision), so the CLI generates a
small lossless PNG preview of each published TIFF and rewrites it whenever
an edit changes the rendering. The path is recorded on the negative row and
reported through `roll info`; Swift only displays the file it is told to.

The published TIFF holds **normalized log density** (section 3.11), a
negative in appearance — `val = 0` is the scene highlight, `val = 1` the
scene shadow. Displayed raw it is a flat, un-inverted negative: honest,
useless for judging a rotation. So the preview decodes through
`normalization.decode_normalized`, takes `1 - val`, and encodes 8-bit —
**no gamma**: log density is already roughly perceptually uniform, and
pushing it through an sRGB OETF would double-encode. The result is a
positive-looking, flat-contrast image. On top of that flat baseline the
user's nondestructive tone adjustment (`tone.py`, recorded as a `tone` op)
composes a paper-grade contrast curve into the same display LUT — a
preview-only judgement aid, not the Phase 4 print curve, which will own
the pixels at export time. The downscale happens in normalized density
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

from pathlib import Path

import cv2
import numpy as np

from scanny_boy import auto_rotate, color, normalization, tone
from scanny_boy.library import repo
from scanny_boy.library.db import library_db_path

# Longest edge of a generated preview, in pixels.
PREVIEW_MAX_EDGE = 1024

MAX_CODE = 65535


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


def _write_downscaled(
    image: np.ndarray,
    destination: Path,
    tone_params: dict[str, float] | None = None,
    color_params: dict[str, float] | None = None,
    metering: color.Metering | None = None,
    mode: str = "positive",
) -> tuple[int, int]:
    """The downscale (in normalized density — code space, not linear light:
    averaging density is what averaging a photographic image means —
    docs/DECISIONS.md, "Normalization decisions") plus the display encode.
    Returns the written PNG's `(width, height)`."""
    edge = max(image.shape[0], image.shape[1])
    if edge > PREVIEW_MAX_EDGE:
        scale = PREVIEW_MAX_EDGE / edge
        image = cv2.resize(
            image,
            (round(image.shape[1] * scale), round(image.shape[0] * scale)),
            interpolation=cv2.INTER_AREA,
        )
    _encode_display_png(
        image,
        destination,
        tone_params,
        color_params=color_params,
        metering=metering,
        mode=mode,
    )
    return image.shape[1], image.shape[0]


def _display_tables(
    tone_params: tone.ToneParams,
    color_params: color.ColorParams,
    metering: color.Metering,
    channels: int,
) -> np.ndarray:
    """Per-channel float display tables, shape `(channels, 65536)`."""
    return tone.build_channel_tables(tone_params, color_params, metering, channels)


def _encode_display_png(
    image: np.ndarray,
    destination: Path,
    tone_params: dict[str, float] | None = None,
    color_params: dict[str, float] | None = None,
    metering: color.Metering | None = None,
    mode: str = "positive",
) -> None:
    """16-bit normalized-density (or already-8-bit) RGB -> 8-bit lossless
    PNG on disk, no downscale, no gamma. `mode` picks the display encode:
    `"positive"` is the inverted look the filmstrip reads as one (tone and
    colour ops compose into the display LUT), `"negative"` is the
    un-inverted density view, which no tone or colour ever touches. The
    published TIFF is never touched by any of it."""
    if mode not in DISPLAY_MODES:
        raise ValueError(f"unknown display mode {mode!r}")
    if image.dtype == np.uint16:
        if mode == "negative":
            image = NEGATIVE_DISPLAY_LUT[image]
        else:
            channels = image.shape[2] if image.ndim == 3 else 1
            if tone_params is None and color_params is None:
                image = NORMALIZED_DISPLAY_LUT[image]
            else:
                tone_obj = tone.ToneParams(**tone_params) if tone_params else tone.NEUTRAL
                color_obj = (
                    color.ColorParams(**color_params) if color_params else color.NEUTRAL_COLOR
                )
                meter = metering or color.Metering(
                    ranges=(1.0,) * channels, shadow_refs_norm=None
                )
                tables = _display_tables(tone_obj, color_obj, meter, channels)
                use_separation = (
                    channels > 1
                    and color_params is not None
                    and color_obj.dye_separation != 1.0
                )
                if use_separation:
                    gathered = np.empty(image.shape, dtype=np.float32)
                    for ch in range(channels):
                        gathered[..., ch] = tables[ch][image[..., ch]]
                    separated = color.apply_separation(gathered, color_obj)
                    encoded = np.clip(np.rint(separated * 255), 0, 255).astype(np.uint8)
                    image = encoded
                elif channels == 1:
                    image = np.rint(tables[0][image] * 255).astype(np.uint8)
                else:
                    out = np.empty(image.shape, dtype=np.uint8)
                    for ch in range(channels):
                        out[..., ch] = np.rint(tables[ch][image[..., ch]] * 255).astype(np.uint8)
                    image = out
    elif mode == "negative":
        raise ValueError(
            "the negative view must be encoded from the published TIFF's "
            "density codes, not from an already-display-encoded image"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    if not ok:
        raise ValueError(f"could not encode preview {destination}")
    destination.write_bytes(encoded.tobytes())


def _display_image(
    tiff_path: Path,
    quarter_turns: int = 0,
    flipped_horizontally: bool = False,
    fine_angle_deg: float = 0.0,
) -> np.ndarray:
    """The published TIFF's full display image — the net transform replayed
    in canonical order (mirror horizontally, then the fine rotation's warp
    with the fill sentinel, then the quarter turns) — the pixels
    `generate_preview`, `render_preview`, and `render_region`'s exact path
    all work from. uint16 RGB in density codes, like the TIFF."""
    import tifffile

    image = _promote_to_rgb(tifffile.imread(tiff_path))
    if flipped_horizontally:
        image = np.ascontiguousarray(image[:, ::-1])
    if abs(fine_angle_deg) >= 1e-9:
        image = auto_rotate.rotate_with_fill(image, fine_angle_deg)
    if quarter_turns % 4:
        # np.rot90 turns counter-clockwise; the count is net clockwise
        # quarter turns.
        image = np.ascontiguousarray(np.rot90(image, k=(-quarter_turns) % 4))
    return image


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
    the flat look), composed into the display LUT.
    Returns the preview path, or None when the negative has no published
    output to preview."""
    if negative.output is None:
        return None

    tiff_path = Path(roll_dir) / negative.output["name"]
    image = _display_image(tiff_path, quarter_turns, flipped_horizontally, fine_angle_deg)

    destination = _preview_path(roll_id, negative.negative_id)
    _write_downscaled(
        image,
        destination,
        tone_params,
        color_params=color_params,
        metering=metering,
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
) -> tuple[int, int]:
    """The whole display image in the requested display mode, downscaled to
    `PREVIEW_MAX_EDGE`, written to a caller-named path — the pure-query
    sibling of `generate_preview` backing the app's positive/negative
    toggle (`edit render-preview`): nothing is recorded, the TIFF is
    untouched. The transform replays exactly as `generate_preview`'s does.
    `mode` is `"positive"` (the inverted look, always what the managed
    on-disk preview holds; `tone_params` — the net `tone` op's
    `{"grade_r", "snap_gamma"}` — composes into its LUT exactly as it does
    there) or `"negative"` (the un-inverted density view, which no tone
    ever reaches — `tone_params` is ignored in that mode). Returns the
    written PNG's `(width, height)`."""
    image = _display_image(tiff_path, quarter_turns, flipped_horizontally, fine_angle_deg)
    width, height = _write_downscaled(image, destination, tone_params, mode=mode)
    return width, height


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
) -> Region:
    """Encode the published TIFF's `(x, y, width, height)` display-space
    region as a lossless 1:1 PNG — display space is the TIFF with the net
    transform folded in, exactly as `generate_preview` shows it: mirrored
    horizontally first (when flipped), then fine-rotated (the auto-seeded
    `rotate_fine` angle, a warp about the canvas center with the fill
    sentinel in the uncovered pixels), then rotated — and the encode is the
    8-bit display LUT named by `mode` (the inverted positive, with the net
    `tone` op composed in when `tone_params` is given — or the un-inverted
    negative, which no tone reaches) with no downscale.

    The region is clamped against the image bounds; the returned `Region`
    is the rect actually rendered, post-clamp. Cropping first and
    transforming the crop is exact for axis-aligned rects, so the same
    pixels come back as a full decode would give —
    `test_render_region_matches_full_decode` holds that equivalence, and
    the strip-level reader (only the strips overlapping the rect are
    decoded, since the published TIFF is strip-compressed, not tiled) is
    held to the full read as well. The fine rotation's warp interpolates
    across the crop boundary, so a nonzero angle takes the exact path
    instead: decode the whole TIFF, replay the full transform on it, and
    slice the rect out of the result — the same pixels the preview shows.
    """
    if mode not in DISPLAY_MODES:
        raise ValueError(f"unknown display mode {mode!r}")
    tiff_h, tiff_w = _read_tiff_dimensions(tiff_path)
    r = (-int(quarter_turns)) % 4
    # Odd net turns swap the display dimensions.
    display_h, display_w = (tiff_w, tiff_h) if r % 2 else (tiff_h, tiff_w)
    dx, dy, dw, dh = _clamp_display_region(x, y, width, height, display_h, display_w)

    if abs(fine_angle_deg) >= 1e-9:
        # The fine warp interpolates across its source's boundaries, so
        # crop-then-transform is no longer exact: replay the transform on
        # the full decode, the way `generate_preview` does, then slice.
        image = _display_image(tiff_path, quarter_turns, flipped_horizontally, fine_angle_deg)
        if destination is not None:
            _encode_display_png(
                image[dy : dy + dh, dx : dx + dw],
                destination,
                tone_params,
                color_params=color_params,
                metering=metering,
                mode=mode,
            )
        return dx, dy, dw, dh

    # Invert the display transform on the clamped rect: the display image
    # is `rot90(mirror(tiff), k=r)`, so the rect corners map through the
    # inverse rotation into mirrored-tiff space (`_display_point_to_tiff`),
    # and the mirror — its own inverse — flips the column bounds back into
    # tiff space.
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

    crop = _read_tiff_region(tiff_path, (tx0, ty0, tx1 - tx0, ty1 - ty0))
    if flipped_horizontally:
        crop = np.ascontiguousarray(crop[:, ::-1])
    if r:
        crop = np.ascontiguousarray(np.rot90(crop, k=r))
    if destination is not None:
        _encode_display_png(
            _promote_to_rgb(crop),
            destination,
            tone_params,
            color_params=color_params,
            metering=metering,
            mode=mode,
        )
    return dx, dy, dw, dh


# tone and color ops never route through the lossless incremental path —
# an 8-bit PNG cannot be re-curved or re-coloured losslessly.
PREVIEW_OPS = {"cw", "ccw", "flip"}
_STATE_PREVIEW_OPS = {repo.TONE_OP, repo.COLOR_OP}


def ensure_preview(
    roll_dir: Path, roll_id: str, negative, op: str | None = None
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
      been through.
    - Preview exists, no op: leave it alone.
    """
    if op is not None and op not in PREVIEW_OPS and op not in _STATE_PREVIEW_OPS:
        raise ValueError(f"unknown preview op {op!r}")
    if negative.preview_path is None or not Path(negative.preview_path).exists():
        state = repo.net_edit_state(roll_dir, negative.negative_id)
        meter = color.read_metering(negative.normalization)
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
        )
    if op is not None:
        if op in _STATE_PREVIEW_OPS:
            state = repo.net_edit_state(roll_dir, negative.negative_id)
            meter = color.read_metering(negative.normalization)
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
            )
        return transform_preview(Path(negative.preview_path), op)
    return Path(negative.preview_path)


def sync_previews(
    roll_dir: Path, manifest, published_outputs: list[str] | None = None
) -> None:
    """Generate previews for completed negatives that lack one, regenerate
    the ones whose pixels this run replaced, and record the paths.

    `published_outputs` names the output files the caller just published.
    A re-stitch adopts an existing negative — same id, same preview path,
    brand-new TIFF — so a cached preview must not survive that: it would
    show the old pixels. Regenerated previews carry the ops log's net
    rotation, since the published TIFF never does."""
    published = set(published_outputs or [])
    changed = False
    for negative in manifest.negatives:
        if negative.status != "completed" or negative.output is None:
            continue
        has_preview = negative.preview_path and Path(negative.preview_path).exists()
        if has_preview and negative.output["name"] not in published:
            continue
        state = repo.net_edit_state(roll_dir, negative.negative_id)
        meter = color.read_metering(negative.normalization)
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
