"""Selection range-checking and grouping.

Pure functions over an already-computed canonical order plus a user-picked
list of filenames — see `docs/IMPLEMENTATION_PLAN.md` sections 1.1, 3.2, and
3.3 — plus the batch's grid spec (docs/GRID_STITCH_PLAN.md section 2.1).
"""

from __future__ import annotations

import dataclasses

# The flat count bounds, moved here from `cli.py` (which now imports them):
# `selection.py` must not import from `cli.py` — the dependency runs the
# other way — and `validate_grid` needs the cap.
MIN_PER_NEGATIVE = 1
MAX_PER_NEGATIVE = 12


class SelectionUsageError(Exception):
    """The `--files` argument doesn't correspond to the catalogue: an
    unknown filename or a duplicate. Neither has a dedicated CONTRACT.md
    code; `probe.py` reports both as `NO_FILES`.
    """


class InvalidGridError(ValueError):
    """A well-formed `--grid AxD` whose shape `validate_grid` refuses.
    Distinct from `SelectionUsageError` (which maps to `NO_FILES`): `cli.py`
    maps this to `Code.INVALID_GRID`, and no other handler touches it."""


@dataclasses.dataclass(frozen=True)
class GridSpec:
    """The 2D arrangement of one negative's scans.

    `across` runs left-to-right in capture space, `down` runs
    top-to-bottom; `across * down` is the batch's scans-per-negative. A
    strip is `GridSpec(across=N, down=1)`, which is what a batch with no
    grid declares, so the strip path is the R=1 case of the grid path and
    not a separate code path.

    `min(across, down) <= 2` by feature constraint: every cell must show
    rebate, which only holds when every cell touches the grid boundary.
    """

    across: int
    down: int

    @property
    def count(self) -> int:
        return self.across * self.down

    @property
    def is_strip(self) -> bool:
        return self.down == 1 or self.across == 1

    def to_dict(self) -> dict[str, int]:
        return {"across": self.across, "down": self.down}

    @classmethod
    def from_dict(cls, data: dict[str, int]) -> GridSpec:
        return cls(across=int(data["across"]), down=int(data["down"]))


def validate_grid(spec: GridSpec) -> None:
    """Reject grid shapes the feature does not support.

    `min(across, down) <= 2` is the feature's own constraint — every cell
    must show film rebate, which only holds when every cell touches the
    grid's outer boundary. `across * down <= MAX_PER_NEGATIVE` keeps the
    memory gate reachable (docs/GRID_STITCH_PLAN.md section 7.1: 12 frames
    of 24MP sits at the gate even after the memory-estimate fix).
    """
    if spec.across < 1 or spec.down < 1:
        raise InvalidGridError(
            f"grid dimensions must each be at least 1, got {spec.across}x{spec.down}"
        )
    if min(spec.across, spec.down) > 2:
        raise InvalidGridError(
            f"grid {spec.across}x{spec.down} is not supported: every cell of "
            "the grid must show film rebate, which only holds when "
            "min(across, down) <= 2"
        )
    if spec.count > MAX_PER_NEGATIVE:
        raise InvalidGridError(
            f"grid {spec.across}x{spec.down} is {spec.count} scans, above the "
            f"maximum of {MAX_PER_NEGATIVE} per negative"
        )


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
