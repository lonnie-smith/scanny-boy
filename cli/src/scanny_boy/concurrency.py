"""Worker-count policy and the per-worker memory budget of
`docs/IMPLEMENTATION_PLAN.md` section 3.8.

The rules, verbatim from the plan:

- Default workers: `min(shots_per_negative, os.process_cpu_count() or 1, 4)`.
- "Budget **512 MiB of memory per worker**. One output frame is about
  140 MiB, and LibRaw needs working space on top of that, so this is
  roughly a three-times margin."
- "If the computed **default** worker count exceeds the budget for this
  machine, silently reduce it. Never fail a run because of the default."
- "If an **explicit** `--jobs` value exceeds the budget, reject it with
  `INSUFFICIENT_MEMORY` and report both numbers."
- "The budget is workers x 512 MiB, and it must not exceed half of
  physical RAM."

Physical RAM comes from `os.sysconf`, which reports it identically on
macOS and on the Linux CI runner, and needs no third-party dependency.
Section 2.5 warns that neither `os.process_cpu_count()` nor
`os.cpu_count()` distinguishes performance cores from efficiency cores on
Apple silicon, which is why the default is capped at four regardless.
"""

from __future__ import annotations

import os

from scanny_boy.events import Code

MIB = 1024 * 1024

# Section 3.8 sets this at 512 MiB and then says: "Chunk 6 measures peak
# resident memory for jobs 1 and 4. Raise the per-worker budget if the
# measured peak plus 25% is larger, and record the measurement in the pull
# request." It is larger, so this is 640 MiB. Measured on the six real
# sample NEFs (`scripts/measure-concurrency.py`, macOS 14.6.1, Apple
# silicon), peak resident set size and the budget each row demands:
#
#     jobs  peak MiB  peak+25%  per worker
#        1     463.8     579.8       579.8   <- binding
#        2     846.8    1058.5       529.3
#        3    1217.4    1521.7       507.2
#        4    1608.8    2011.0       502.8
#
# The per-worker figure falls as workers rise because a large part of the
# serial peak is the fixed interpreter, numpy, rawpy, and imagecodecs
# baseline (~80-100 MiB by the difference between rows), not per-frame
# cost. The budget must cover the worst row, which is the serial one.
# Figures move by a few MiB between runs; re-measure before changing this.
WORKER_MEMORY_BUDGET_BYTES = 640 * MIB
MAX_DEFAULT_WORKERS = 4

# Section 3.8: the budget "must not exceed half of physical RAM".
_USABLE_MEMORY_FRACTION = 0.5


class MemoryBudgetError(Exception):
    """Maps to `INSUFFICIENT_MEMORY`: an *explicit* `--jobs` value needs
    more memory than the section 3.8 budget allows on this machine. Never
    raised for the computed default, which is silently reduced instead."""

    def __init__(self, requested_workers: int, permitted_workers: int, total_memory: int) -> None:
        message = (
            f"--jobs {requested_workers} needs "
            f"{requested_workers * WORKER_MEMORY_BUDGET_BYTES} bytes at "
            f"{WORKER_MEMORY_BUDGET_BYTES} bytes per worker, which is more than "
            f"half of this machine's {total_memory} bytes of physical memory; "
            f"at most {permitted_workers} workers fit in that budget"
        )
        super().__init__(message)
        self.code = Code.INSUFFICIENT_MEMORY
        self.message = message
        self.requested_workers = requested_workers
        self.permitted_workers = permitted_workers
        self.total_memory = total_memory


def physical_memory_bytes() -> int:
    """Total physical RAM. `SC_PAGE_SIZE * SC_PHYS_PAGES` is available on
    both macOS and Linux; if the platform refuses to answer, report a
    single worker's budget so the policy degrades to one worker rather
    than crashing."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return WORKER_MEMORY_BUDGET_BYTES


def workers_permitted_by_memory(total_memory: int) -> int:
    """How many workers fit in half of `total_memory` at 512 MiB each.

    Never returns less than one: a serial run holds one frame at a time
    and is the floor of what the program can do at all.
    """
    usable = int(total_memory * _USABLE_MEMORY_FRACTION)
    return max(1, usable // WORKER_MEMORY_BUDGET_BYTES)


def default_worker_count(
    shots_per_negative: int, *, cpu_count: int | None = None, total_memory: int | None = None
) -> int:
    """`min(shots_per_negative, cpus, 4)`, silently reduced to fit the
    memory budget."""
    cpus = cpu_count if cpu_count is not None else (os.process_cpu_count() or 1)
    memory = total_memory if total_memory is not None else physical_memory_bytes()
    computed = min(shots_per_negative, max(1, cpus), MAX_DEFAULT_WORKERS)
    return max(1, min(computed, workers_permitted_by_memory(memory)))


def resolve_worker_count(
    shots_per_negative: int,
    requested: int | None,
    *,
    cpu_count: int | None = None,
    total_memory: int | None = None,
) -> int:
    """The worker count for this run.

    `requested is None` means no `--jobs` was given: use the default,
    reduced silently if memory demands it. An explicit value is honoured
    as given, or rejected with `MemoryBudgetError` — never quietly
    lowered, because the user asked for a specific number.
    """
    if requested is None:
        return default_worker_count(
            shots_per_negative, cpu_count=cpu_count, total_memory=total_memory
        )

    memory = total_memory if total_memory is not None else physical_memory_bytes()
    permitted = workers_permitted_by_memory(memory)
    if requested > permitted:
        raise MemoryBudgetError(requested, permitted, memory)
    return requested
