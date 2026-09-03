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

For a 90-degree rotation the regeneration is a lossless pixel transpose of
the cached preview, not a re-decode of a multi-megapixel TIFF. PNG, not
JPEG: repeated edits would otherwise compound generational loss.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from scanny_boy import normalization
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
    roll_dir: Path, roll_id: str, negative, quarter_turns: int = 0
) -> Path | None:
    """A preview of `negative`'s published TIFF with the negative's net
    `quarter_turns` applied — the published TIFF itself never carries edits,
    so the rotation is folded in here. Returns the preview path, or None
    when the negative has no published output to preview."""
    if negative.output is None:
        return None
    import tifffile

    tiff_path = Path(roll_dir) / negative.output["name"]
    image = tifffile.imread(tiff_path)
    if image.ndim == 2:
        image = np.stack([image] * 3, axis=-1)
    elif image.shape[2] == 4:
        image = image[:, :, :3]
    if quarter_turns % 4:
        # np.rot90 turns counter-clockwise; the count is net clockwise
        # quarter turns.
        image = np.ascontiguousarray(np.rot90(image, k=(-quarter_turns) % 4))

    destination = _preview_path(roll_id, negative.negative_id)
    _write_downscaled(image, destination)
    return destination


def rotate_preview(current_path: Path, direction: str) -> Path:
    """One lossless quarter turn of the cached preview, in place."""
    image = cv2.imread(str(current_path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"could not read preview {current_path}")
    # cv2 is BGR but a transpose is channel-agnostic.
    # np.rot90 turns counter-clockwise, so cw is k=3.
    rotated = np.rot90(image, k=3 if direction == "cw" else 1)
    ok, encoded = cv2.imencode(".png", rotated)
    if not ok:
        raise ValueError(f"could not encode preview {current_path}")
    current_path.write_bytes(encoded.tobytes())
    return current_path


def ensure_preview(
    roll_dir: Path, roll_id: str, negative, direction: str | None = None
) -> Path | None:
    """The preview path `negative` should display after `direction`
    (None meaning: make sure one exists).

    - No preview yet: generate one from the published TIFF with the ops
      log's *net* rotation applied — the incremental `direction` is already
      in that net, and the cache may have been lost several edits ago, so
      regenerating with only the latest turn would lie.
    - Preview exists and a direction came in: rotate the cached preview,
      which already reflects every earlier turn.
    - Preview exists, no direction: leave it alone.
    """
    if negative.preview_path is None or not Path(negative.preview_path).exists():
        net = repo.net_rotation_quarter_turns(roll_dir, negative.negative_id)
        return generate_preview(roll_dir, roll_id, negative, quarter_turns=net)
    if direction is not None:
        return rotate_preview(Path(negative.preview_path), direction)
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
        turns = repo.net_rotation_quarter_turns(roll_dir, negative.negative_id)
        preview = generate_preview(
            roll_dir, manifest.roll_id, negative, quarter_turns=turns
        )
        if preview is not None:
            negative.preview_path = str(preview)
            changed = True
    if changed:
        from scanny_boy.roll_manifest import write_roll_manifest

        write_roll_manifest(roll_dir, manifest)


def rotations_for(manifest, roll_dir: Path) -> dict[str, int]:
    """Net quarter turns per negative id, for `roll info` augmentation."""
    return {
        negative.negative_id: repo.net_rotation_quarter_turns(
            roll_dir, negative.negative_id
        )
        for negative in manifest.negatives
    }
