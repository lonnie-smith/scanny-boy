"""Output-folder validation, rerun detection, and overwrite-conflict
planning. See `docs/IMPLEMENTATION_PLAN.md` section 3.6.

`plan_rerun` decides whether an output folder is usable at all (empty, or
related to a valid, matching prior manifest) and, if it is a rerun, which
outputs need consent to overwrite (`conflicting_outputs`, from groups the
prior manifest marked `completed`) versus which can be cleaned up
automatically (`stale_outputs`/`stale_staging_dirs`, from groups that never
reached a final state — recovery per section 3.6's crash-safety rules, not
an overwrite decision).

Phase 3 section 3.4 makes rolls additive: a nonempty roll folder holding
published outputs from earlier runs is normal, not `OUTPUT_NOT_EMPTY`, and
under `ROLL_RULES` a completed negative's published output is neither a
conflict needing consent nor a stale output — section 5.4 decision 3, no
roll content is ever re-rendered or replaced in place. Only the conflict
classification changes: an unrelated nonempty folder is still rejected, and
never-finished negatives still get recovery cleanup.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scanny_boy.events import Code
from scanny_boy.library import repo
from scanny_boy.manifest import (
    MANIFEST_FILENAME,
    check_rerun_compatible,
    check_rerun_matches,
    load_manifest,
)
from scanny_boy.roll_manifest import (
    check_roll_invariants,
    load_roll_manifest,
)

STAGING_SUFFIX = ".scanny-staging"


@dataclasses.dataclass(frozen=True)
class UnitView:
    """One publishable unit of a manifest, seen through `FolderRules`: a
    Phase 1 *group* or a Phase 2 *negative*. `_plan_rerun` cares only about
    the three things below, which is why one function serves both."""

    unit_id: str
    expected_outputs: list[str]
    is_completed: bool


@dataclasses.dataclass(frozen=True)
class FolderRules:
    """Everything `_plan_rerun` needs to know about *which* manifest kind an
    output folder holds. Section 3.7: generalise this module over the
    manifest it is reading rather than copying it.

    A roll folder's record lives in the library database, not in a file, so
    "does this folder hold one?" is `registered` rather than a filename
    existence check; `manifest_filename` survives only to name the Phase 1
    work manifest in errors and the known-artifacts list."""

    manifest_filename: str
    registered: Callable[[Path], bool]
    unregistered_reason: Callable[[Path], str]
    load: Callable[[Path], Any]
    run_id_of: Callable[[Any], str]
    units_of: Callable[[Any], list[UnitView]]
    all_expected_outputs_of: Callable[[Any], list[str]]


PREPARE_RULES = FolderRules(
    manifest_filename=MANIFEST_FILENAME,
    registered=lambda output_dir: (output_dir / MANIFEST_FILENAME).exists(),
    unregistered_reason=lambda output_dir: (
        f"{output_dir} is not empty and has no {MANIFEST_FILENAME}"
    ),
    load=load_manifest,
    run_id_of=lambda manifest: manifest.run_id,
    units_of=lambda manifest: [
        UnitView(
            unit_id=group.group_id,
            expected_outputs=group.expected_outputs,
            is_completed=group.status == "completed",
        )
        for group in manifest.groups
    ],
    all_expected_outputs_of=lambda manifest: manifest.all_expected_outputs(),
)

ROLL_RULES = FolderRules(
    manifest_filename="",
    registered=repo.roll_registered,
    unregistered_reason=lambda output_dir: (
        f"{output_dir} is not empty and is not a registered roll; create the roll first"
    ),
    load=load_roll_manifest,
    # Phase 3 section 5.4 decision 2: the v2 roll manifest has no top-level
    # `run_id`. Staging directories belong to the run in flight, which is
    # always the last appended one; a roll with no runs yet has none.
    run_id_of=lambda manifest: manifest.runs[-1].run_id if manifest.runs else "",
    units_of=lambda manifest: [
        UnitView(
            unit_id=negative.negative_id,
            expected_outputs=[negative.expected_output],
            is_completed=negative.status == "completed",
        )
        for negative in manifest.negatives
    ],
    all_expected_outputs_of=lambda manifest: manifest.all_expected_outputs(),
)


class OutputFolderError(Exception):
    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def list_non_dot_entries(output_dir: Path) -> list[Path]:
    """Every direct child of `output_dir` except dot-files (section 3.6:
    `.DS_Store`, `._*` AppleDouble files, `.Spotlight-V100`, and anything
    else macOS or Scanny Boy itself never creates without a leading dot)."""
    return [p for p in output_dir.iterdir() if not p.name.startswith(".")]


def validate_not_same_as_input(input_dir: Path, output_dir: Path) -> None:
    if input_dir.resolve() == output_dir.resolve():
        raise OutputFolderError(
            Code.OUTPUT_SAME_AS_INPUT,
            "the output folder resolves to the same folder as the input folder",
        )


def validate_writable(output_dir: Path) -> None:
    if not output_dir.is_dir():
        raise OutputFolderError(
            Code.OUTPUT_NOT_WRITABLE, f"{output_dir} is not an existing directory"
        )
    # No probe file: section 3.6 says Scanny Boy never creates dot-files
    # itself, and a non-dot probe would itself trip the folder-relatedness
    # check above. os.access() answers the writability question without
    # creating anything.
    if not os.access(output_dir, os.W_OK):
        raise OutputFolderError(
            Code.OUTPUT_NOT_WRITABLE, f"cannot write to {output_dir}"
        )


def staging_dir_path(output_dir: Path, run_id: str, group_id: str) -> Path:
    return output_dir / f"{run_id}-{group_id}{STAGING_SUFFIX}"


def _is_staging_dir_for_run(entry: Path, run_id: str) -> bool:
    return (
        entry.is_dir()
        and entry.name.startswith(f"{run_id}-")
        and entry.name.endswith(STAGING_SUFFIX)
    )


@dataclasses.dataclass(frozen=True)
class RerunPlan:
    existing_manifest: Any | None
    conflicting_outputs: list[str]
    stale_outputs: list[str]
    stale_staging_dirs: list[Path]


def _plan_rerun(
    output_dir: Path,
    check: Callable[[Any], None],
    *,
    rules: FolderRules = PREPARE_RULES,
) -> RerunPlan:
    """Shared by `plan_rerun` and `plan_rerun_preview`: folder-relatedness
    and conflict-listing rules are the same either way, so only the
    rerun-comparison itself is a parameter. Raises
    `OutputFolderError(OUTPUT_NOT_EMPTY)` for an unrelated nonempty folder,
    `manifest.BadManifestError` for an unreadable or schema-invalid
    manifest, and whatever `check` raises for a mismatched run.

    `rules` supplies the manifest filename, loader, run id, and units, so
    the identical logic serves Phase 1's per-frame manifest and Phase 2's
    roll manifest."""
    registered = rules.registered(output_dir)
    entries = list_non_dot_entries(output_dir)
    # A registered roll folder may be genuinely empty — its record lives in
    # the library database, not in a file — so registration, not entries,
    # decides whether there is a prior manifest to plan against.
    if not entries and not registered:
        return RerunPlan(None, [], [], [])

    if not registered:
        raise OutputFolderError(
            Code.OUTPUT_NOT_EMPTY, rules.unregistered_reason(output_dir)
        )

    existing = rules.load(output_dir)

    run_id = rules.run_id_of(existing)
    known_names = {name for name in (rules.manifest_filename,) if name}
    known_names.update(rules.all_expected_outputs_of(existing))
    for entry in entries:
        if entry.name in known_names:
            continue
        if _is_staging_dir_for_run(entry, run_id):
            continue
        raise OutputFolderError(
            Code.OUTPUT_NOT_EMPTY,
            f"{output_dir} contains content unrelated to its manifest: {entry.name}",
        )

    check(existing)

    conflicting: list[str] = []
    stale: list[str] = []
    stale_staging: list[Path] = []
    for unit in rules.units_of(existing):
        unit_dir = staging_dir_path(output_dir, run_id, unit.unit_id)
        if unit.is_completed:
            # Section 3.4: rolls are additive. A completed negative's
            # published output belongs to an earlier run and is never
            # re-rendered or replaced, so under ROLL_RULES it is neither a
            # conflict needing consent nor a stale output (section 5.4
            # decision 3: nothing in a roll is ever overwritten in place).
            if rules is not ROLL_RULES:
                conflicting.extend(
                    name
                    for name in unit.expected_outputs
                    if (output_dir / name).exists()
                )
        else:
            stale.extend(
                name for name in unit.expected_outputs if (output_dir / name).exists()
            )
            if unit_dir.exists():
                stale_staging.append(unit_dir)

    return RerunPlan(existing, conflicting, stale, stale_staging)


def plan_rerun(
    output_dir: Path, candidate: Any, *, rules: FolderRules = PREPARE_RULES
) -> RerunPlan:
    """Raises `OutputFolderError(OUTPUT_NOT_EMPTY)` for an unrelated
    nonempty folder, `manifest.BadManifestError` for an unreadable or
    schema-invalid manifest, and `manifest.ManifestMismatchError` when a
    valid manifest describes a different run.

    The comparison itself is manifest-kind-specific — Phase 1's
    `check_rerun_matches` compares a `Manifest`, Phase 3's
    `check_roll_invariants` a `RollInvariants` (section 3.4) — so it is
    selected from `rules` here rather than carried as a sixth `FolderRules`
    field. Under `ROLL_RULES`, `candidate` is therefore a `RollInvariants`,
    not a manifest, and the folder is additive: completed negatives'
    published outputs are never reported as conflicts."""
    if rules is ROLL_RULES:
        check: Callable[[Any], None] = lambda existing: check_roll_invariants(
            existing, candidate
        )
    else:
        check = lambda existing: check_rerun_matches(existing, candidate)
    return _plan_rerun(output_dir, check, rules=rules)


def plan_rerun_preview(
    output_dir: Path,
    *,
    rules: FolderRules = PREPARE_RULES,
    source_order: list[str],
    source_hashes: dict[str, str],
    shots_per_negative: int,
    groups: list[tuple[str, list[str]]],
    icc_sha256: str | None,
) -> RerunPlan:
    """The `probe --out` counterpart to `plan_rerun` (section 4.1:
    "output-folder validation and the overwrite-conflict preview"). Compares
    only the fields known before a film date has been entered; `convert`
    still calls `plan_rerun` with a complete candidate manifest and repeats
    the full comparison before it writes anything."""
    return _plan_rerun(
        output_dir,
        lambda existing: check_rerun_compatible(
            existing,
            source_order=source_order,
            source_hashes=source_hashes,
            shots_per_negative=shots_per_negative,
            groups=groups,
            icc_sha256=icc_sha256,
        ),
        rules=rules,
    )


def apply_recovery_cleanup(output_dir: Path, plan: RerunPlan) -> None:
    """Delete stray outputs and staging directories left by groups that
    never reached a final state in a prior run (section 3.6: "the manifest
    records progress so a rerun can safely replace an incomplete group").
    Unconditional: these files were never a protected, completed result."""
    for name in plan.stale_outputs:
        (output_dir / name).unlink(missing_ok=True)
    for staging_dir in plan.stale_staging_dirs:
        shutil.rmtree(staging_dir, ignore_errors=True)
