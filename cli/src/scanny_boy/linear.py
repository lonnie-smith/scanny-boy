"""The linear transfer: decode and encode between uint16 codes and [0, 1].

The decode is linear (`gamma=(1, 1)`, `output_color=raw` — see
`raw_decode.RAW_PARAMS`), so these helpers are plain fixed-point scaling:
a code maps to its fraction of full scale and back. The round trip is
exact for every code. `encode_from_linear` clips to [0, 1] first, because
a correction that boosts a value past full scale cannot be represented.
"""

from __future__ import annotations

import numpy as np

MAX_CODE = 65535


def decode_to_linear(image: np.ndarray) -> np.ndarray:
    """uint16 array of any shape -> float32 linear in [0, 1]."""
    return image.astype(np.float32) / MAX_CODE


def encode_from_linear(image: np.ndarray) -> np.ndarray:
    """float32 linear -> uint16. Clamps to [0, 1] first, then rounds
    with numpy.rint."""
    linear = np.clip(image, 0.0, 1.0)
    return np.rint(linear * MAX_CODE).astype(np.uint16)
