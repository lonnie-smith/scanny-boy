"""Module entry point, also used as PyInstaller's analysis script.

`python -m scanny_boy` and the packaged `ScannyBoyCLI.app` executable both
run this file, so the packaged program starts through exactly the same code
path as a development run (see `docs/IMPLEMENTATION_PLAN.md` section 5.2).
"""

from __future__ import annotations

import sys

from scanny_boy.cli import main

if __name__ == "__main__":
    sys.exit(main())
