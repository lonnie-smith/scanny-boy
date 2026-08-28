"""Loads and verifies the bundled ROMM-compatible ICC colour profile.

See `docs/IMPLEMENTATION_PLAN.md` section 3.4 ("The ICC colour profile").
Every TIFF this program writes must carry this exact profile — never
untagged ROMM data — so its SHA-256 is checked before every use, not just
once at import time.
"""

from __future__ import annotations

import hashlib
import importlib.resources

from scanny_boy.events import Code

PROFILE_FILENAME = "ProPhoto-v4.icc"

# Verified against the upstream file per section 3.4.
PROFILE_SHA256 = "090daf740c136b4a63bf979d64f034b4a65aa5abbb04a0917729222afe2bb5c2"


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
