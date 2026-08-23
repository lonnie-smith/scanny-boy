"""Setup-consistency validation over already-read per-file settings.

Kept independent of file I/O (operates on `metadata.SourceSettings`) so it
can be tested without real NEFs or rawpy. See
`docs/IMPLEMENTATION_PLAN.md` section 3.2.

Exposure time is required per section 3.5's tag table: missing anywhere
stops with `CAPTURE_METADATA_MISSING`, but its value is deliberately not
compared across the selection — exposure properties may differ across a
roll (see `docs/ARCHITECTURE.md`'s blending discussion).

Aperture, ISO, focal length, and source orientation are required per
section 3.5's tag table (or, for orientation, per section 3.2's general
comparison list, since orientation is not itself a section 3.5 output
tag): missing anywhere stops with `CAPTURE_METADATA_MISSING`, and a
present-but-differing value stops with `CAPTURE_SETTINGS_DIFFER`.

Lens model is `optional` per section 3.5: missing is a warning, not a stop.
But section 3.2 still lists lens among the fields to compare, so among the
files that *do* report a lens, a differing value still stops with
`CAPTURE_SETTINGS_DIFFER`.

Camera white balance is required per section 3.2 ("Require four finite,
positive multipliers"): missing or invalid stops with
`CAPTURE_METADATA_MISSING`; a differing normalised vector (beyond the
documented 1e-6 tolerance) stops with `CAPTURE_SETTINGS_DIFFER`.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Callable

from scanny_boy.events import Code
from scanny_boy.metadata import SourceSettings

WB_REL_TOL = 1e-6
WB_ABS_TOL = 1e-6


class ConsistencyError(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclasses.dataclass(frozen=True)
class ConsistencyWarning:
    code: Code
    message: str


@dataclasses.dataclass(frozen=True)
class ConsistencyResult:
    warnings: list[ConsistencyWarning]


def _require_present(
    settings_list: list[SourceSettings],
    getter: Callable[[SourceSettings], object],
    field_label: str,
) -> None:
    missing = [s.filename for s in settings_list if getter(s) is None]
    if missing:
        raise ConsistencyError(
            Code.CAPTURE_METADATA_MISSING,
            f"{field_label} is missing from: {', '.join(missing)}",
        )


def _require_equal(
    settings_list: list[SourceSettings],
    getter: Callable[[SourceSettings], object],
    field_label: str,
) -> None:
    _require_present(settings_list, getter, field_label)
    values = {getter(s) for s in settings_list}
    if len(values) > 1:
        detail = ", ".join(f"{s.filename}={getter(s)}" for s in settings_list)
        raise ConsistencyError(
            Code.CAPTURE_SETTINGS_DIFFER,
            f"{field_label} differs across the selection: {detail}",
        )


def _normalise_white_balance(
    wb: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    if wb is None:
        return None
    if any(not math.isfinite(v) or v <= 0 for v in wb):
        return None
    _, g1, _, _ = wb
    return tuple(v / g1 for v in wb)  # type: ignore[return-value]


def _white_balance_close(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    return all(
        math.isclose(x, y, rel_tol=WB_REL_TOL, abs_tol=WB_ABS_TOL)
        for x, y in zip(a, b, strict=True)
    )


def _check_white_balance(settings_list: list[SourceSettings]) -> None:
    normalised: list[tuple[str, tuple[float, float, float, float]]] = []
    missing: list[str] = []
    for s in settings_list:
        n = _normalise_white_balance(s.camera_whitebalance)
        if n is None:
            missing.append(s.filename)
        else:
            normalised.append((s.filename, n))
    if missing:
        raise ConsistencyError(
            Code.CAPTURE_METADATA_MISSING,
            "camera white balance is missing, non-finite, or non-positive for: "
            + ", ".join(missing),
        )
    base_name, base_wb = normalised[0]
    differing = [name for name, wb in normalised if not _white_balance_close(wb, base_wb)]
    if differing:
        raise ConsistencyError(
            Code.CAPTURE_SETTINGS_DIFFER,
            "camera white balance differs across the selection: "
            + ", ".join([base_name, *differing]),
        )


def _check_lens_model(settings_list: list[SourceSettings]) -> list[ConsistencyWarning]:
    warnings = [
        ConsistencyWarning(
            Code.CAPTURE_METADATA_MISSING,
            f"lens model is not available for {s.filename}",
        )
        for s in settings_list
        if s.lens_model is None
    ]
    present = [(s.filename, s.lens_model) for s in settings_list if s.lens_model is not None]
    if present:
        distinct = {value for _, value in present}
        if len(distinct) > 1:
            detail = ", ".join(f"{name}={value}" for name, value in present)
            raise ConsistencyError(
                Code.CAPTURE_SETTINGS_DIFFER,
                f"lens model differs across the selection: {detail}",
            )
    return warnings


def check_consistency(settings_list: list[SourceSettings]) -> ConsistencyResult:
    """Validate `settings_list` (one entry per selected file, any order).
    Raises `ConsistencyError` on the first problem found; otherwise returns
    warnings for optional tags missing from individual files."""
    _require_present(settings_list, lambda s: s.exposure_time, "exposure time")
    _require_equal(settings_list, lambda s: s.f_number, "aperture")
    _require_equal(settings_list, lambda s: s.iso, "ISO")
    _require_equal(settings_list, lambda s: s.focal_length, "focal length")
    _require_equal(settings_list, lambda s: s.orientation, "source orientation")
    _check_white_balance(settings_list)
    warnings = _check_lens_model(settings_list)
    return ConsistencyResult(warnings=warnings)
