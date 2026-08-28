"""Worker-count policy and the per-worker memory budget of
`docs/IMPLEMENTATION_PLAN.md` section 3.8.

Every machine shape here is passed in explicitly rather than read from the
host, so the same assertions hold on the developer's 64 GiB Mac and on a
CI runner.
"""

from __future__ import annotations

import pytest

from scanny_boy.concurrency import (
    MAX_DEFAULT_WORKERS,
    WORKER_MEMORY_BUDGET_BYTES,
    MemoryBudgetError,
    default_worker_count,
    physical_memory_bytes,
    resolve_worker_count,
    workers_permitted_by_memory,
)

MIB = 1024 * 1024
GIB = 1024 * MIB

# Section 2.5: "The standard macOS runner has 3 CPUs and about 7 GB of
# RAM. Any memory guard must not reject a default run on that machine."
CI_RUNNER_MEMORY = 7 * 1000**3
CI_RUNNER_CPUS = 3

PLENTY_OF_MEMORY = 64 * GIB


def test_budget_is_640_mib_per_worker():
    # Raised from the plan's starting figure of 512 MiB by section 3.8's
    # own instruction: the measured serial peak plus 25% is 594 MiB. See
    # the comment on the constant and scripts/measure-concurrency.py.
    assert WORKER_MEMORY_BUDGET_BYTES == 640 * MIB


def test_workers_permitted_is_half_of_physical_ram_over_the_budget():
    two_workers = 2 * 2 * WORKER_MEMORY_BUDGET_BYTES  # twice a two-worker budget
    assert workers_permitted_by_memory(two_workers) == 2
    assert workers_permitted_by_memory(2 * 5 * WORKER_MEMORY_BUDGET_BYTES) == 5


def test_workers_permitted_never_drops_below_one():
    # A serial run holds one frame at a time and is the floor of what the
    # program can do at all, so even an absurdly small machine permits 1.
    assert workers_permitted_by_memory(128 * MIB) == 1
    assert workers_permitted_by_memory(0) == 1


def test_physical_memory_is_a_plausible_positive_number_on_this_machine():
    assert physical_memory_bytes() >= 512 * MIB


# --- the default: min(shots_per_negative, cpus, 4), memory-clamped -------


@pytest.mark.parametrize(
    ("shots", "cpus", "expected"),
    [
        (3, 10, 3),  # shots_per_negative binds
        (12, 10, MAX_DEFAULT_WORKERS),  # the hard cap of 4 binds
        (12, 2, 2),  # cpu count binds
        (1, 10, 1),  # a one-shot negative is serial
    ],
)
def test_default_worker_count_takes_the_minimum_of_shots_cpus_and_four(shots, cpus, expected):
    assert (
        default_worker_count(shots, cpu_count=cpus, total_memory=PLENTY_OF_MEMORY) == expected
    )


def test_default_worker_count_is_silently_reduced_to_fit_the_memory_budget():
    # Section 3.8: "If the computed default worker count exceeds the
    # budget for this machine, silently reduce it. Never fail a run
    # because of the default." The CPU rule alone would give 4 here.
    three_worker_machine = 2 * 3 * WORKER_MEMORY_BUDGET_BYTES
    assert default_worker_count(12, cpu_count=10, total_memory=PLENTY_OF_MEMORY) == 4
    assert default_worker_count(12, cpu_count=10, total_memory=three_worker_machine) == 3


def test_default_worker_count_never_reaches_zero_on_a_tiny_machine():
    assert default_worker_count(3, cpu_count=1, total_memory=128 * MIB) == 1


# --- the 7 GB CI runner: reduced, never rejected -------------------------


def test_on_a_simulated_seven_gb_machine_the_default_is_reduced_not_rejected():
    # Section 2.5's "any memory guard must not reject a default run on
    # that machine", checked over every legal --per-negative value: the
    # default always resolves, is at least 1, and never exceeds what the
    # budget permits.
    permitted = workers_permitted_by_memory(CI_RUNNER_MEMORY)
    for shots in range(1, 13):
        workers = resolve_worker_count(
            shots, None, cpu_count=CI_RUNNER_CPUS, total_memory=CI_RUNNER_MEMORY
        )
        assert 1 <= workers <= permitted
        assert workers == min(shots, CI_RUNNER_CPUS, MAX_DEFAULT_WORKERS, permitted)


def test_on_a_simulated_seven_gb_machine_an_explicit_jobs_12_is_rejected():
    with pytest.raises(MemoryBudgetError) as excinfo:
        resolve_worker_count(
            3, 12, cpu_count=CI_RUNNER_CPUS, total_memory=CI_RUNNER_MEMORY
        )

    error = excinfo.value
    assert error.code.value == "INSUFFICIENT_MEMORY"
    # "reject it ... and report both numbers".
    assert error.requested_workers == 12
    assert error.permitted_workers == workers_permitted_by_memory(CI_RUNNER_MEMORY)
    assert "12" in error.message
    assert str(error.permitted_workers) in error.message


def test_an_explicit_jobs_within_the_budget_is_honoured_exactly():
    # Explicit values are never quietly lowered, even past the default's
    # cap of four or the CPU count: the user asked for a number.
    permitted = workers_permitted_by_memory(CI_RUNNER_MEMORY)
    assert permitted > MAX_DEFAULT_WORKERS
    assert (
        resolve_worker_count(
            3, permitted, cpu_count=CI_RUNNER_CPUS, total_memory=CI_RUNNER_MEMORY
        )
        == permitted
    )


def test_an_explicit_jobs_1_is_always_allowed_however_small_the_machine():
    assert resolve_worker_count(3, 1, cpu_count=1, total_memory=128 * MIB) == 1


def test_an_explicit_jobs_2_is_rejected_on_a_one_worker_machine():
    with pytest.raises(MemoryBudgetError):
        resolve_worker_count(3, 2, cpu_count=1, total_memory=WORKER_MEMORY_BUDGET_BYTES)
