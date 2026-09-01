"""Loads and verifies the bundled Linear-ProPhoto ICC colour profile.

See `docs/DECISIONS.md` ("Linear decode for NegPy compatibility"). Every
TIFF this program writes must carry this exact profile — never untagged
data — so its SHA-256 is checked before every use, not just once at import
time. The profile declares ProPhoto (ROMM) primaries with a **linear**
TRC, the truth about the pixels since `raw_decode.RAW_PARAMS` decodes
linear (`output_color=raw`, `gamma=(1, 1)`, unity white balance).
"""

from __future__ import annotations

import hashlib
import importlib.resources

from scanny_boy.events import Code

PROFILE_FILENAME = "ScannyBoy-Linear-ProPhoto-v1.icc"

# Verified against the generated file.
PROFILE_SHA256 = "a739982a10dc1b9de27dd262c4d7a8269c2a48ec42c4eb3743e1a108c6a8d744"

# ICC parametricCurveType function type 0 (pure gamma), g = 1.0 in
# s15Fixed16 — the identity curve, because the decode is linear.
TRC_FUNCTION_TYPE = 0
TRC_G = 65536


class IccProfileError(Exception):
    """Maps to `ICC_PROFILE_INVALID`: the bundled profile is missing or its
    SHA-256 does not match the vetted value."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = Code.ICC_PROFILE_INVALID
        self.message = message


def verify_icc_profile(data: bytes) -> None:
    """Raise `IccProfileError` unless `data`'s SHA-256 matches
    `PROFILE_SHA256`."""
    digest = hashlib.sha256(data).hexdigest()
    if digest != PROFILE_SHA256:
        raise IccProfileError(
            f"ICC profile has SHA-256 {digest}, expected {PROFILE_SHA256}"
        )


def load_icc_profile() -> bytes:
    """Read the bundled ICC profile through `importlib.resources` (works
    identically in a development checkout and inside the packaged program;
    see section 3.4) and verify its SHA-256."""
    resource = importlib.resources.files("scanny_boy.resources") / PROFILE_FILENAME
    try:
        data = resource.read_bytes()
    except (FileNotFoundError, OSError) as exc:
        raise IccProfileError(
            f"bundled ICC profile {PROFILE_FILENAME} is missing"
        ) from exc

    verify_icc_profile(data)
    return data
