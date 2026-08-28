"""SHA-256 file hashing for the manifest's source and output records.

See `docs/IMPLEMENTATION_PLAN.md` section 3.7: every recorded source and
completed output carries a SHA-256, computed here by streaming the file
rather than loading it whole (source NEFs run to tens of megabytes; outputs
are full 140 MiB frames).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK_SIZE = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()
