"""docs/OPTIMIZATION.md §1.2: the import-graph assertion.

A timing assertion would be flaky on CI and worthless; this asserts the
graph instead. The two module names are the two large leaves — `scipy`
(pulled by `calibration`, and by `film_base`'s chain) and `alembic`
(pulled by `library.db`'s migration path) — and they are what regresses
when someone adds a convenience import at the top of `cli.py`.

The check runs in a subprocess: an assertion against the test process's
own `sys.modules` would only describe whichever module some earlier test
happened to import.
"""

from __future__ import annotations

import subprocess
import sys

_PROBE = (
    "import sys\n"
    "import scanny_boy.cli\n"
    "leaves = sorted(m for m in ('scipy', 'alembic') if m in sys.modules)\n"
    "assert not leaves, f'imported eagerly by scanny_boy.cli: {leaves}'\n"
)


def test_cli_import_keeps_scipy_and_alembic_out_of_sys_modules() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
