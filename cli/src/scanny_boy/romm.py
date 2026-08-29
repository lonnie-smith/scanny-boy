"""LibRaw's transfer curve, decode and encode.

Phase 1's section 3.4 documents the ROMM RGB transfer curve and its two
breakpoints, one in each domain. Section 2.3.1 of the Phase 2 plan found that
rawpy's `gamma=(1.8, 16)` output does **not** follow that curve: it applies
LibRaw's own generalised `gamma_curve`, which shares ROMM's gamma and toe
slope but has different breakpoints and an offset term ROMM lacks entirely.
This is the **user-approved amendment** — the constants and formulas below
are LibRaw's, not the ROMM registry's. Do not substitute ROMM's
`0.03125` / `0.001953125` breakpoints; section 2.3.1 measured the resulting
error at 3.149% of linear light on average.
"""

from __future__ import annotations

import numpy as np

ROMM_GAMMA = 1.8
ROMM_SLOPE = 16.0
ENCODED_BREAKPOINT = 0.008454220179  # in the ENCODED domain
LINEAR_BREAKPOINT = 0.000528388761  # in the LINEAR domain
CURVE_OFFSET = 0.006763376143  # LibRaw's g[4]; ROMM has no such term
MAX_CODE = 65535


def _build_decode_lut() -> np.ndarray:
    codes = np.arange(MAX_CODE + 1, dtype=np.float64)
    encoded = codes / MAX_CODE
    linear = np.where(
        encoded < ENCODED_BREAKPOINT,
        encoded / ROMM_SLOPE,
        ((encoded + CURVE_OFFSET) / (1.0 + CURVE_OFFSET)) ** ROMM_GAMMA,
    )
    return linear.astype(np.float32)


DECODE_LUT: np.ndarray = _build_decode_lut()


def decode_to_linear(image: np.ndarray) -> np.ndarray:
    """uint16 array of any shape -> float32 linear, via DECODE_LUT indexing.
    Never calls numpy.power per pixel."""
    return DECODE_LUT[image]


def encode_from_linear(image: np.ndarray) -> np.ndarray:
    """float32 linear -> uint16. Clamps to [0, 1] first, then rounds
    with numpy.rint."""
    linear = np.clip(image, 0.0, 1.0).astype(np.float64)
    encoded = np.where(
        linear < LINEAR_BREAKPOINT,
        linear * ROMM_SLOPE,
        linear ** (1.0 / ROMM_GAMMA) * (1.0 + CURVE_OFFSET) - CURVE_OFFSET,
    )
    encoded = np.clip(encoded, 0.0, 1.0)
    return np.rint(encoded * MAX_CODE).astype(np.uint16)
