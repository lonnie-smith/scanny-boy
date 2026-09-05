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
positive-looking, flat-contrast image — no print curve, because the print
curve is Phase 4 and faking one here would be a look nobody chose. The
downscale happens in normalized density (code space), not linear light,
which is correct: averaging density is what averaging a photographic image
means. Uncovered canvas renders black here, without special-casing: the
fill sits at the thin end, so `1 - val` takes it to zero (section 3.14).

The published TIFF itself is never touched — display encoding lives here
and only here, and this path decodes through
`normalization.decode_normalized`, never through the file's ICC profile
(section 3.12's rule).

For a 90-degree rotation or a horizontal flip the incremental update is a
lossless pixel transpose or mirror of the cached preview, not a re-decode of
a multi-megapixel TIFF. PNG, not
JPEG: repeated edits would otherwise compound generational loss.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from scanny_boy import auto_rotate, normalization
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


def previews_root() -> Path:
    """Previews sit beside the library database in Application Support."""
    return library_db_path().parent / "previews"


def _preview_path(roll_id: str, negative_id: str) -> Path:
    return previews_root() / roll_id / f"{negative_id}.png"


def _write_downscaled(image: np.ndarray, destination: Path) -> None:
    edge = max(image.shape[0], image.shape[1])
    if edge > PREVIEW_MAX_EDGE:
        # The downscale happens in normalized density (code space), not
        # linear light: averaging density is what averaging a photographic
        # image means (docs/DECISIONS.md, "Normalization decisions").
        scale = PREVIEW_MAX_EDGE / edge
        image = cv2.resize(
            image,
            (round(image.shape[1] * scale), round(image.shape[0] * scale)),
            interpolation=cv2.INTER_AREA,
        )
    _encode_display_png(image, destination)


def _encode_display_png(image: np.ndarray, destination: Path) -> None:
    """16-bit normalized-density (or already-8-bit) RGB -> inverted 8-bit
    lossless PNG on disk, no downscale, no gamma."""
    if image.dtype == np.uint16:
        # The TIFF holds normalized log density; decode, invert, encode
        # 8-bit with no gamma.
        image = NORMALIZED_DISPLAY_LUT[image]
    destination.parent.mkdir(parents=True, exist_ok=True)
    # cv2 is BGR; the TIFF is RGB, so flip the channels for storage.
    ok, encoded = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    if not ok:
        raise ValueError(f"could not encode preview {destination}")
    destination.write_bytes(encoded.tobytes())


def generate_preview(
    roll_dir: Path,
    roll_id: str,
    negative,
    quarter_turns: int = 0,
    flipped_horizontally: bool = False,
    fine_angle_deg: float = 0.0,
) -> Path | None:
    """A preview of `negative`'s published TIFF with the negative's net
    transform applied — the published TIFF itself never carries edits, so
    the transform is folded in here. The canonical replay mirrors the
    original horizontally first (when flipped), then applies the ops log's
    fine auto-rotation (a warp with the stitching fill sentinel in the
    uncovered pixels, `auto_rotate.rotate_with_fill`), then rotates; the
    fine angle is negated by a flip exactly as `repo.net_edit_state`'s
    replay says, so the caller passes the canonical angle through
    untouched. Returns the preview path, or None when the negative has no
    published output to preview."""
    if negative.output is None:
        return None
    import tifffile

    tiff_path = Path(roll_dir) / negative.output["name"]
    image = tifffile.imread(tiff_path)
    if image.ndim == 2:
        image = np.stack([image] * 3, axis=-1)
    elif image.shape[2] == 4:
        image = image[:, :, :3]
    if flipped_horizontally:
        image = np.ascontiguousarray(image[:, ::-1])
    if abs(fine_angle_deg) >= 1e-9:
        image = auto_rotate.rotate_with_fill(image, fine_angle_deg)
    if quarter_turns % 4:
        # np.rot90 turns counter-clockwise; the count is net clockwise
        # quarter turns.
        image = np.ascontiguousarray(np.rot90(image, k=(-quarter_turns) % 4))

    destination = _preview_path(roll_id, negative.negative_id)
    _write_downscaled(image, destination)
    return destination


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
    destination: Path | None = None,
) -> Region:
    """Encode the published TIFF's `(x, y, width, height)` display-space
    region as a lossless 1:1 PNG — display space is the TIFF with the net
    transform folded in, exactly as `generate_preview` shows it: mirrored
    horizontally first (when flipped), then rotated — and the encode is the
    same inverted 8-bit display LUT with no downscale.

    The region is clamped against the image bounds; the returned `Region`
    is the rect actually rendered, post-clamp. Cropping first and
    transforming the crop is exact for axis-aligned rects, so the same
    pixels come back as a full decode would give —
    `test_render_region_matches_full_decode` holds that equivalence, and
    the strip-level reader (only the strips overlapping the rect are
    decoded, since the published TIFF is strip-compressed, not tiled) is
    held to the full read as well.
    """
    tiff_h, tiff_w = _read_tiff_dimensions(tiff_path)
    r = (-int(quarter_turns)) % 4
    # Odd net turns swap the display dimensions.
    display_h, display_w = (tiff_w, tiff_h) if r % 2 else (tiff_h, tiff_w)
    dx, dy, dw, dh = _clamp_display_region(x, y, width, height, display_h, display_w)

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
        _encode_display_png(_promote_to_rgb(crop), destination)
    return dx, dy, dw, dh


# The preview ops `ensure_preview`/`transform_preview` understand, kept in
# step with the repo's ops log: rotations by direction, plus the flip.
PREVIEW_OPS = {"cw", "ccw", "flip"}


def ensure_preview(
    roll_dir: Path, roll_id: str, negative, op: str | None = None
) -> Path | None:
    """The preview path `negative` should display after `op`
    (None meaning: make sure one exists).

    - No preview yet: generate one from the published TIFF with the ops
      log's *net* transform applied — the incremental `op` is already in
      that net, and the cache may have been lost several edits ago, so
      regenerating with only the latest op would lie.
    - Preview exists and an op came in: transform the cached preview, which
      already reflects every earlier edit.
    - Preview exists, no op: leave it alone.
    """
    if op is not None and op not in PREVIEW_OPS:
        raise ValueError(f"unknown preview op {op!r}")
    if negative.preview_path is None or not Path(negative.preview_path).exists():
        quarter_turns, flipped, fine_angle = repo.net_edit_state(
            roll_dir, negative.negative_id
        )
        return generate_preview(
            roll_dir,
            roll_id,
            negative,
            quarter_turns=quarter_turns,
            flipped_horizontally=flipped,
            fine_angle_deg=fine_angle,
        )
    if op is not None:
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
        quarter_turns, flipped, fine_angle = repo.net_edit_state(
            roll_dir, negative.negative_id
        )
        preview = generate_preview(
            roll_dir,
            manifest.roll_id,
            negative,
            quarter_turns=quarter_turns,
            flipped_horizontally=flipped,
            fine_angle_deg=fine_angle,
        )
        if preview is not None:
            negative.preview_path = str(preview)
            changed = True
    if changed:
        from scanny_boy.roll_manifest import write_roll_manifest

        write_roll_manifest(roll_dir, manifest)


def transforms_for(manifest, roll_dir: Path) -> dict[str, tuple[int, bool, float]]:
    """Net transform per negative id — `(quarter_turns, flipped,
    fine_angle_deg)` — for `roll info` augmentation."""
    return {
        negative.negative_id: repo.net_edit_state(roll_dir, negative.negative_id)
        for negative in manifest.negatives
    }
