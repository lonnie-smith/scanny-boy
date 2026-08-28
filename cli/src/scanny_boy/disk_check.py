"""Free-space estimate and check from `docs/IMPLEMENTATION_PLAN.md` section
3.9.

```text
P = height x width x 3 channels x 2 bytes
B = ceil(P x 1.05)                 # one TIFF plus metadata overhead
M = number of expected outputs that do not already exist
G = largest group size
D = max(1 MiB, estimated manifest size)
required free bytes = ceil((M x B + 2 x G x B + D) x 1.20)
```

`B` deliberately assumes compression saves nothing, leaving real headroom
over the ~74% Deflate actually achieves. `2 x G x B` covers the base and
rewritten TIFFs the two-pass write holds at once for one staged group.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

from scanny_boy.events import Code

MIB = 1024 * 1024
_HEADROOM = 1.05
_SAFETY_MARGIN = 1.20


class DiskCheckError(Exception):
    """Maps to `INSUFFICIENT_DISK`: free space on the output volume is
    below the section 3.9 estimate."""

    def __init__(self, required_bytes: int, available_bytes: int) -> None:
        message = (
            f"required {required_bytes} bytes free, but only "
            f"{available_bytes} bytes are available on the output volume"
        )
        super().__init__(message)
        self.code = Code.INSUFFICIENT_DISK
        self.message = message
        self.required_bytes = required_bytes
        self.available_bytes = available_bytes


def one_frame_bytes(width: int, height: int) -> int:
    """`P`: uncompressed bytes for one `(height, width, 3)` `uint16` frame."""
    return width * height * 3 * 2


def required_free_bytes(
    *,
    width: int,
    height: int,
    missing_output_count: int,
    largest_group_size: int,
    manifest_size_estimate: int,
) -> int:
    p = one_frame_bytes(width, height)
    b = math.ceil(p * _HEADROOM)
    d = max(MIB, manifest_size_estimate)
    return math.ceil(
        (missing_output_count * b + 2 * largest_group_size * b + d) * _SAFETY_MARGIN
    )


def check_disk_space(output_dir: Path, required_bytes: int) -> None:
    available_bytes = shutil.disk_usage(output_dir).free
    if available_bytes < required_bytes:
        raise DiskCheckError(required_bytes, available_bytes)
