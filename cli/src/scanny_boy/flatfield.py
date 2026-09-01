"""Flat-field correction: measure a copy stand's falloff once and divide it
back out of every frame.

Modelled on NegPy's flat-field feature (`negpy/features/flatfield/
logic.py:compute_gain`), adapted to this program: the correction is
multiplicative gain only — no black-frame subtraction — and it is applied per
frame in the convert stage, immediately after `raw_decode.decode_raw` and
before the intermediate TIFF is written, so the stitch stage's per-frame
photometric gain solve (`layout.solve_gains`) is asked to explain real
exposure mismatch rather than spatial falloff, which a global scalar per
frame per channel cannot represent.

The reference is a shot of the bare light source with no negative in the
holder, decoded with the project's locked `RAW_PARAMS` — reusing the one
decode path the project treats as load-bearing. Dividing each channel by its
own mean cancels any constant per-channel scale, so decoding the reference
with `use_camera_wb=True` (as NegPy's no-white-balance decode would not)
yields an identical gain map; `flatfield_test.py` proves that.

Memory (docs/FLATFIELD_PLAN.md section 2.7): the gain map is computed and
stored at `GAIN_MAP_MAX_EDGE`, materialised at frame resolution once per run
and shared read-only across workers, and applied in horizontal bands of
`FLATFIELD_BAND_ROWS` rows so a worker's peak transient is band-sized —
`concurrency.py`'s 640 MiB per-worker budget was measured without any of
this and must not be re-measured.

Every constant in this module is defined here and nowhere else.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid
from pathlib import Path

import cv2
import numpy as np

from scanny_boy import romm
from scanny_boy.events import Code, FlatFieldProfileSummary
from scanny_boy.hashing import sha256_file
from scanny_boy.library.db import library_db_path
from scanny_boy.raw_decode import decode_raw

# Falloff is low-frequency; a full-resolution gain map buys nothing.
GAIN_MAP_MAX_EDGE = 256
# Gaussian sigma on the downsampled map: max(h, w) / 16, so dust and noise
# in the reference are not baked into the correction.
BLUR_SIGMA_DIVISOR = 16
# A near-black edge in the reference must not become an extreme multiplier.
GAIN_MIN, GAIN_MAX = 0.25, 4.0
GAIN_MAP_FORMAT_VERSION = 1
# Rows of a frame corrected at once, decoded to linear and re-encoded in
# place: peak transient per worker is band-sized, not frame-sized.
FLATFIELD_BAND_ROWS = 512
# A frame is warned, not failed, when more than this fraction of its pixels
# would be pushed past full scale by the correction.
CLIPPED_PIXEL_WARN_FRACTION = 0.001


class FlatFieldError(Exception):
    """A flat-field profile operation failed with a stable CONTRACT.md code.

    A bad reference NEF is not one of these — `decode_raw` propagates
    `UnsupportedRawError` / `UnreadableRawError` unchanged, because a bad
    reference is a bad NEF and already has stable codes."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class FlatFieldProfile:
    profile_id: str
    name: str
    gain_map_path: str
    gain_map_sha256: str
    # Provenance only. Once the profile exists the reference is never read
    # again — the profile is self-contained.
    source_path: str | None
    reference_width: int
    reference_height: int
    # How this map was built (`build_params`).
    params: dict
    scanny_boy_version: str
    created_at: str


def flatfield_root() -> Path:
    """Gain maps sit beside the library database and the previews in
    Application Support — exactly mirroring `previews.previews_root()`, so
    `SCANNY_BOY_LIBRARY_DB` relocates gain maps along with everything else
    and the test suite gets per-test isolation for free."""
    return library_db_path().parent / "flatfield"


def _now_iso() -> str:
    return (
        datetime.datetime.now(datetime.UTC).replace(tzinfo=None).isoformat() + "Z"
    )


def build_params() -> dict:
    """How a gain map is built — the constants of this module that produced
    it, recorded on the profile so a map can be interpreted without knowing
    which build wrote it."""
    return {
        "gain_map_max_edge": GAIN_MAP_MAX_EDGE,
        "blur_sigma_divisor": BLUR_SIGMA_DIVISOR,
        "gain_min": GAIN_MIN,
        "gain_max": GAIN_MAX,
        "format_version": GAIN_MAP_FORMAT_VERSION,
    }


def compute_gain(linear: np.ndarray) -> np.ndarray:
    """NegPy's `compute_gain`, per channel independently, on one linear
    float32 `(h, w, 3)` array.

    Downsample with INTER_AREA so `max(h, w) <= GAIN_MAP_MAX_EDGE`, blur each
    channel with `sigma = max(h, w) / BLUR_SIGMA_DIVISOR`, divide each
    channel by its own mean, and clip to `[GAIN_MIN, GAIN_MAX]`. Any constant
    per-channel scale — which is exactly what a white-balance multiplier is —
    cancels identically in the mean division.
    """
    height, width = linear.shape[:2]
    small = linear
    scale = GAIN_MAP_MAX_EDGE / max(height, width)
    if scale < 1.0:
        small = cv2.resize(
            linear,
            (round(width * scale), round(height * scale)),
            interpolation=cv2.INTER_AREA,
        )

    sigma = max(small.shape[0], small.shape[1]) / BLUR_SIGMA_DIVISOR
    channels = []
    for channel in range(small.shape[2]):
        blurred = cv2.GaussianBlur(
            small[:, :, channel].astype(np.float32), (0, 0), sigma
        )
        mean = blurred.mean()
        # A channel that blurs to all black is a degenerate reference; clip
        # it to the extreme multiplier rather than dividing by zero.
        gain = np.where(blurred > 0.0, mean / blurred, GAIN_MAX)
        channels.append(np.clip(gain, GAIN_MIN, GAIN_MAX))
    return np.stack(channels, axis=-1).astype(np.float32)


def build_gain_map(reference: Path) -> tuple[np.ndarray, int, int]:
    """Decode the reference with the locked `RAW_PARAMS` into linear light
    and measure its falloff. Returns `(gain_map, reference_width,
    reference_height)` — the full-resolution dimensions the profile records
    for the aspect-ratio check."""
    frame = decode_raw(reference)
    gain_map = compute_gain(romm.decode_to_linear(frame.pixels))
    return gain_map, frame.width, frame.height


def save_gain_map(profile_id: str, gain_map: np.ndarray) -> tuple[Path, str]:
    """Write the `.npz` beside the library database. Returns its path and
    the SHA-256 of the file bytes."""
    path = flatfield_root() / f"{profile_id}.npz"
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, format_version=GAIN_MAP_FORMAT_VERSION, gain_map=gain_map)
    return path, sha256_file(path)


def load_gain_map(profile: FlatFieldProfile) -> np.ndarray:
    """Read a profile's gain map back. The profile is self-contained: the
    original reference file is never read again, so a missing or corrupt
    `.npz` is the one thing that can fail here."""
    path = Path(profile.gain_map_path)
    if not path.exists():
        raise FlatFieldError(
            Code.FLATFIELD_GAIN_MAP_MISSING,
            f"the gain map {path} no longer exists",
        )
    try:
        with np.load(path, allow_pickle=False) as data:
            if int(data["format_version"]) != GAIN_MAP_FORMAT_VERSION:
                raise FlatFieldError(
                    Code.FLATFIELD_GAIN_MAP_MISSING,
                    f"the gain map {path} is format version "
                    f"{int(data['format_version'])}, not "
                    f"{GAIN_MAP_FORMAT_VERSION}",
                )
            gain_map = data["gain_map"]
            if gain_map.dtype != np.float32 or gain_map.ndim != 3:
                raise FlatFieldError(
                    Code.FLATFIELD_GAIN_MAP_MISSING,
                    f"the gain map {path} is corrupt: expected a float32 "
                    "rank-3 array",
                )
    except (OSError, ValueError, KeyError, EOFError) as exc:
        raise FlatFieldError(
            Code.FLATFIELD_GAIN_MAP_MISSING, f"the gain map {path} is corrupt: {exc}"
        ) from exc
    if sha256_file(path) != profile.gain_map_sha256:
        raise FlatFieldError(
            Code.FLATFIELD_GAIN_MAP_MISSING,
            f"the gain map {path} no longer matches its recorded SHA-256",
        )
    return gain_map


def resize_gain_map(gain_map: np.ndarray, width: int, height: int) -> np.ndarray:
    """The map is measured at `GAIN_MAP_MAX_EDGE`; a run's frames are not.
    Falloff is low-frequency, so plain linear interpolation is plenty."""
    return cv2.resize(gain_map, (width, height), interpolation=cv2.INTER_LINEAR)


def apply_in_place(pixels: np.ndarray, full_res_gain: np.ndarray) -> int:
    """Multiply one decoded `uint16` frame by the full-resolution gain map,
    band by band, in place.

    The correction is multiplicative and therefore only valid in linear
    light, while the decoded frame is gamma-encoded — so each band makes the
    `decode_to_linear -> multiply -> encode_from_linear` round trip. The two
    transfer curves are exact inverses to within one code, which
    `flatfield_test.py` proves rather than assumes.

    Returns the number of pixels the correction clipped — where the
    corrected linear value exceeds full scale and `encode_from_linear`'s
    clip at 1.0 loses the highlight — so the caller can decide whether to
    warn. A pixel counts once if any channel clipped.
    """
    height = pixels.shape[0]
    clipped = 0
    for start in range(0, height, FLATFIELD_BAND_ROWS):
        stop = min(start + FLATFIELD_BAND_ROWS, height)
        band = pixels[start:stop]
        linear = romm.decode_to_linear(band)
        corrected = linear * full_res_gain[start:stop]
        clipped += int(np.count_nonzero(np.any(corrected > 1.0, axis=-1)))
        band[:] = romm.encode_from_linear(corrected)
    return clipped


def profile_token(profile: FlatFieldProfile) -> dict:
    """The roll-invariant identity of a profile. `name` is deliberately not
    in the token: renaming a profile must not invalidate a roll."""
    return {
        "profile_id": profile.profile_id,
        "gain_map_sha256": profile.gain_map_sha256,
        "params": profile.params,
    }


def flatfield_profile_summary(profile: FlatFieldProfile) -> FlatFieldProfileSummary:
    """The fields a `flatfield` event carries: what the app's profile list
    needs and nothing it does not."""
    return FlatFieldProfileSummary(
        profile_id=profile.profile_id,
        name=profile.name,
        reference_width=profile.reference_width,
        reference_height=profile.reference_height,
        source_path=profile.source_path,
        created_at=profile.created_at,
    )


def create_profile(reference: Path, name: str) -> FlatFieldProfile:
    """Decode, build, save, and insert — the one path `flatfield create` and
    the app's New Profile sheet both use. Raises the reference decode's own
    errors for a bad NEF and `FlatFieldError` (`FLATFIELD_PROFILE_EXISTS`)
    when the name is already taken."""
    from scanny_boy.library import repo

    existing = repo.list_flatfield_profiles()
    if any(profile.name == name for profile in existing):
        raise FlatFieldError(
            Code.FLATFIELD_PROFILE_EXISTS, f"a profile named {name!r} already exists"
        )

    gain_map, width, height = build_gain_map(reference)
    profile_id = str(uuid.uuid4())
    path, sha256 = save_gain_map(profile_id, gain_map)
    profile = FlatFieldProfile(
        profile_id=profile_id,
        name=name,
        gain_map_path=str(path),
        gain_map_sha256=sha256,
        source_path=str(reference),
        reference_width=width,
        reference_height=height,
        params=build_params(),
        scanny_boy_version=_current_scanny_boy_version(),
        created_at=_now_iso(),
    )
    repo.save_flatfield_profile(profile)
    return profile


def _current_scanny_boy_version() -> str:
    from scanny_boy.manifest import current_scanny_boy_version

    return current_scanny_boy_version()