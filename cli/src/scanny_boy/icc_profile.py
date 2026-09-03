"""Loads and verifies the bundled ICC colour profiles.

Two profiles, one claim each (docs/DECISIONS.md, "Normalization decisions"):

- `ScannyBoy-Linear-ProPhoto-v1.icc` tags the **prepare stage's
  intermediates**, and its linear TRC is the truth about those pixels
  (`raw_decode.RAW_PARAMS` decodes linear — `output_color=raw`,
  `gamma=(1, 1)`, unity white balance).
- `ScannyBoy-Density-ProPhoto-v1.icc` tags the **published, normalized
  TIFF**. Its g = 2.2 TRC is a *viewing convention*, not a colorimetric
  claim: the pixels are normalized log density, which no ICC TRC expresses.
  It exists so external viewers show approximately the code values; every
  internal consumer — previews, the edit stage, export, the future print
  stage — decodes through `normalization.decode_normalized`, never through
  an ICC transform. That rule is the load-bearing one, and
  `icc_profile_test.py`'s guard test keeps the profile from creeping into
  the render path where a wrong TRC could corrupt pixels instead of merely
  looking odd.

Every TIFF this program writes must carry the profile its stage dictates —
never untagged data — so the SHA-256 is checked before every use, not just
once at import time.
"""

from __future__ import annotations

import enum
import hashlib
import importlib.resources

from scanny_boy.events import Code

LINEAR_PROFILE_FILENAME = "ScannyBoy-Linear-ProPhoto-v1.icc"
DENSITY_PROFILE_FILENAME = "ScannyBoy-Density-ProPhoto-v1.icc"

# Verified against the generated files (`cli/tools/generate_icc_profile.py`).
LINEAR_PROFILE_SHA256 = (
    "a739982a10dc1b9de27dd262c4d7a8269c2a48ec42c4eb3743e1a108c6a8d744"
)
DENSITY_PROFILE_SHA256 = (
    "26d966d7dcc748eecd618f082cf6e4294a95a4e4a38d9ec2693c741b70f1ee0c"
)


class ProfileKind(enum.StrEnum):
    LINEAR = "linear"  # prepare-stage intermediates
    DENSITY = "density"  # published, normalized TIFFs


# kind -> (filename, sha256)
PROFILES: dict[ProfileKind, tuple[str, str]] = {
    ProfileKind.LINEAR: (LINEAR_PROFILE_FILENAME, LINEAR_PROFILE_SHA256),
    ProfileKind.DENSITY: (DENSITY_PROFILE_FILENAME, DENSITY_PROFILE_SHA256),
}

# ICC parametricCurveType function type 0 (pure gamma), in s15Fixed16. The
# linear profile's g = 1.0 is the identity curve, because the decode is
# linear; the density profile's g = 2.2 (`round(2.2 * 65536)`) is the
# viewing convention described in the module docstring.
TRC_FUNCTION_TYPE = 0
TRC_G_LINEAR = 65536
TRC_G_DENSITY = 144179


class IccProfileError(Exception):
    """Maps to `ICC_PROFILE_INVALID`: the bundled profile is missing or its
    SHA-256 does not match the vetted value."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = Code.ICC_PROFILE_INVALID
        self.message = message


def verify_icc_profile(data: bytes, kind: ProfileKind) -> None:
    """Raise `IccProfileError` unless `data`'s SHA-256 matches the vetted
    value for `kind`. The two profiles are never silently swappable: a
    DENSITY byte string fails a LINEAR verification, and vice versa."""
    filename, expected = PROFILES[kind]
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        raise IccProfileError(
            f"ICC profile has SHA-256 {digest}, expected {expected} ({filename})"
        )


def profile_record(kind: ProfileKind) -> dict[str, str]:
    """`{"name": ..., "sha256": ...}` for `kind` — the shape the work
    manifest's and roll manifest's `icc_profile` fields carry."""
    name, sha256 = PROFILES[kind]
    return {"name": name, "sha256": sha256}


def load_icc_profile(kind: ProfileKind = ProfileKind.LINEAR) -> bytes:
    """Read the bundled ICC profile for `kind` through
    `importlib.resources` (works identically in a development checkout and
    inside the packaged program; see section 3.4) and verify its SHA-256."""
    filename, _ = PROFILES[kind]
    resource = importlib.resources.files("scanny_boy.resources") / filename
    try:
        data = resource.read_bytes()
    except (FileNotFoundError, OSError) as exc:
        raise IccProfileError(f"bundled ICC profile {filename} is missing") from exc

    verify_icc_profile(data, kind)
    return data
