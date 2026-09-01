"""Small per-negative previews, generated and rotated by the CLI.

The app's Edit tab shows a preview of each negative; with edits in the
picture, "what the negative looks like" is derived state — the CLI is its
only legitimate source (Python owns every decision), so the CLI generates a
small lossless PNG preview of each published TIFF and rewrites it whenever
an edit changes the rendering. The path is recorded on the negative row and
reported through `roll info`; Swift only displays the file it is told to.

For a 90-degree rotation the regeneration is a lossless pixel transpose of
the cached preview, not a re-decode of a multi-megapixel TIFF. PNG, not
JPEG: repeated edits would otherwise compound generational loss.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from scanny_boy.library import repo
from scanny_boy.library.db import library_db_path

# Longest edge of a generated preview, in pixels.
PREVIEW_MAX_EDGE = 512


def previews_root() -> Path:
    """Previews sit beside the library database in Application Support."""
    return library_db_path().parent / "previews"


def _preview_path(roll_id: str, negative_id: str) -> Path:
    return previews_root() / roll_id / f"{negative_id}.png"


def _write_downscaled(image: np.ndarray, destination: Path) -> None:
    edge = max(image.shape[0], image.shape[1])
    if edge > PREVIEW_MAX_EDGE:
        scale = PREVIEW_MAX_EDGE / edge
        image = cv2.resize(
            image,
            (round(image.shape[1] * scale), round(image.shape[0] * scale)),
            interpolation=cv2.INTER_AREA,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    # cv2 is BGR; the TIFF is RGB, so flip the channels for storage.
    ok, encoded = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    if not ok:
        raise ValueError(f"could not encode preview {destination}")
    destination.write_bytes(encoded.tobytes())


def generate_preview(roll_dir: Path, roll_id: str, negative) -> Path | None:
    """A preview of `negative`'s published TIFF as it currently stands —
    no edits applied. Returns the preview path, or None when the negative
    has no published output to preview."""
    if negative.output is None:
        return None
    import tifffile

    tiff_path = Path(roll_dir) / negative.output["name"]
    image = tifffile.imread(tiff_path)
    if image.ndim == 2:
        image = np.stack([image] * 3, axis=-1)
    elif image.shape[2] == 4:
        image = image[:, :, :3]
    # 16-bit scans downscale cleanly to 8-bit for a preview.
    if image.dtype == np.uint16:
        image = (image >> 8).astype(np.uint8)

    destination = _preview_path(roll_id, negative.negative_id)
    _write_downscaled(image, destination)
    return destination


def rotate_preview(current_path: Path, direction: str) -> Path:
    """One lossless quarter turn of the cached preview, in place."""
    image = cv2.imread(str(current_path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"could not read preview {current_path}")
    # cv2 is BGR but a transpose is channel-agnostic.
    rotated = np.rot90(image, k=1 if direction == "cw" else 3)
    ok, encoded = cv2.imencode(".png", rotated)
    if not ok:
        raise ValueError(f"could not encode preview {current_path}")
    current_path.write_bytes(encoded.tobytes())
    return current_path


def ensure_preview(
    roll_dir: Path, roll_id: str, negative, direction: str | None = None
) -> Path | None:
    """The preview path `negative` should display after `direction`
    (None meaning: make sure an unrotated one exists).

    - No preview yet: generate one from the published TIFF, then apply the
      quarter turn if asked.
    - Preview exists and a direction came in: rotate the cached preview.
    - Preview exists, no direction: leave it alone.
    """
    if negative.preview_path is None or not Path(negative.preview_path).exists():
        fresh = generate_preview(roll_dir, roll_id, negative)
        if fresh is None:
            return None
        if direction is not None:
            rotate_preview(fresh, direction)
        return fresh
    if direction is not None:
        return rotate_preview(Path(negative.preview_path), direction)
    return Path(negative.preview_path)


def sync_previews(roll_dir: Path, manifest) -> None:
    """Generate previews for any completed negatives that lack one, and
    record the paths. Called after a stitch publishes; cheap when
    everything already has one."""
    changed = False
    for negative in manifest.negatives:
        if negative.status != "completed" or negative.output is None:
            continue
        if negative.preview_path and Path(negative.preview_path).exists():
            continue
        preview = generate_preview(roll_dir, manifest.roll_id, negative)
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
