#!/usr/bin/env python3
"""Measure wall time and peak resident memory of a real `convert` run at
several `--jobs` values.

`docs/IMPLEMENTATION_PLAN.md` section 3.8 requires Chunk 6 to "measure peak
resident memory for jobs 1 and 4" and to "raise the per-worker budget if
the measured peak plus 25% is larger", recording the result in the pull
request. It also says to "record benchmark results, but do not require a
fixed speedup" — hence a script rather than a timing assertion in the test
suite, which would be flaky on shared CI hardware.

Peak RSS comes from `os.wait4`, which reports rusage for one specific
child, so each `--jobs` value is measured independently rather than as a
running maximum over every child ever reaped.

Usage, from the repository root:

    uv run --project cli scripts/measure-concurrency.py
    uv run --project cli scripts/measure-concurrency.py --jobs 1 2 4
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli" / "src"))

from scanny_boy.concurrency import WORKER_MEMORY_BUDGET_BYTES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT / "tests" / "fixtures" / "nef"
SAMPLE_FILES = [
    "_DSC4638.NEF",
    "_DSC4639.NEF",
    "_DSC4640.NEF",
    "_DSC4644.NEF",
    "_DSC4645.NEF",
    "_DSC4646.NEF",
]
MIB = 1024 * 1024

# `ru_maxrss` is bytes on macOS and kilobytes on Linux.
_RSS_SCALE = 1 if sys.platform == "darwin" else 1024


def _run_once(jobs: int, per_negative: int) -> tuple[float, int, int]:
    """Return (wall seconds, peak RSS bytes, exit status) for one run."""
    out_dir = Path(tempfile.mkdtemp(prefix="scanny-measure-"))
    argv = [
        sys.executable,
        "-m",
        "scanny_boy.cli",
        "convert",
        "--input",
        str(FIXTURES_DIR),
        "--files",
        *SAMPLE_FILES,
        "--out",
        str(out_dir),
        "--per-negative",
        str(per_negative),
        "--jobs",
        str(jobs),
    ]
    try:
        started = time.monotonic()
        # Popen for the output redirection, os.wait4 for the per-child
        # rusage Popen.wait() does not expose. Reaping here means Popen
        # must be told the child is already gone, or its own wait would
        # raise ChildProcessError.
        proc = subprocess.Popen(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        _, status, usage = os.wait4(proc.pid, 0)
        proc.returncode = os.waitstatus_to_exitcode(status)
        elapsed = time.monotonic() - started
        return elapsed, usage.ru_maxrss * _RSS_SCALE, proc.returncode
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, nargs="+", default=[1, 4])
    parser.add_argument("--per-negative", type=int, default=3, dest="per_negative")
    args = parser.parse_args()

    missing = [n for n in SAMPLE_FILES if not (FIXTURES_DIR / n).exists()]
    if missing:
        print(f"sample NEFs missing from {FIXTURES_DIR}: {missing}", file=sys.stderr)
        return 1

    budget_mib = WORKER_MEMORY_BUDGET_BYTES // MIB
    print(
        f"{len(SAMPLE_FILES)} frames, --per-negative {args.per_negative}, "
        f"budget {budget_mib} MiB/worker"
    )
    print(f"{'jobs':>5}  {'wall (s)':>9}  {'peak RSS (MiB)':>15}  {'peak+25% (MiB)':>15}  exit")
    for jobs in args.jobs:
        elapsed, peak, status = _run_once(jobs, args.per_negative)
        peak_mib = peak / MIB
        print(
            f"{jobs:>5}  {elapsed:>9.1f}  {peak_mib:>15.1f}  "
            f"{peak_mib * 1.25:>15.1f}  {status}"
        )
    print(
        f"\nRaise WORKER_MEMORY_BUDGET_BYTES only if a peak+25% figure above "
        f"exceeds {budget_mib} MiB x that run's worker count."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
