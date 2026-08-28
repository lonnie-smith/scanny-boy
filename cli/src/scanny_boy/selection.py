"""Selection range-checking and grouping.

Pure functions over an already-computed canonical order plus a user-picked
list of filenames — see `docs/IMPLEMENTATION_PLAN.md` sections 1.1, 3.2, and
3.3.
"""

from __future__ import annotations

import dataclasses


class SelectionUsageError(Exception):
    """The `--files` argument doesn't correspond to the catalogue: an
    unknown filename or a duplicate. Neither has a dedicated CONTRACT.md
    code; `probe.py` reports both as `NO_FILES`.
    """


@dataclasses.dataclass(frozen=True)
class OrderedSelection:
    names: list[str]  # in canonical order
    start_index: int
    end_index: int


def order_selection(catalogue: list[str], files: list[str]) -> OrderedSelection:
    """Re-order `files` (as given, in any order) into canonical order,
    validating that every entry is a real, distinct catalogue member."""
    index_of = {name: i for i, name in enumerate(catalogue)}
    seen: set[str] = set()
    indices: list[int] = []
    for name in files:
        if name in seen:
            raise SelectionUsageError(f"duplicate file in --files: {name!r}")
        seen.add(name)
        index = index_of.get(name)
        if index is None:
            raise SelectionUsageError(
                f"{name!r} is not part of the catalogue for --input"
            )
        indices.append(index)
    indices.sort()
    ordered_names = [catalogue[i] for i in indices]
    return OrderedSelection(
        names=ordered_names, start_index=indices[0], end_index=indices[-1]
    )


def is_contiguous(selection: OrderedSelection) -> bool:
    span = selection.end_index - selection.start_index + 1
    return span == len(selection.names)


def group(names: list[str], per_negative: int) -> list[list[str]]:
    return [names[i : i + per_negative] for i in range(0, len(names), per_negative)]


def nearest_valid_counts(count: int, per_negative: int) -> tuple[int, int]:
    """The nearest selection counts below and above `count` that are evenly
    divisible by `per_negative`, for the `NOT_DIVISIBLE` error message."""
    lower = (count // per_negative) * per_negative
    upper = lower + per_negative
    return lower, upper
