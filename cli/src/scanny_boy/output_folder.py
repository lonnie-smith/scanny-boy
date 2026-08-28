"""Output-folder validation, rerun detection, and overwrite-conflict
planning. See `docs/IMPLEMENTATION_PLAN.md` section 3.6.

`plan_rerun` decides whether an output folder is usable at all (empty, or
related to a valid, matching prior manifest) and, if it is a rerun, which
outputs need consent to overwrite (`conflicting_outputs`, from groups the
prior manifest marked `completed`) versus which can be cleaned up
automatically (`stale_outputs`/`stale_staging_dirs`, from groups that never
reached a final state — recovery per section 3.6's crash-safety rules, not
an overwrite decision).
"""

from __future__ import annotations

import dataclasses
import os
import shutil
from pathlib import Path

from scanny_boy.events import Code
from scanny_boy.manifest import (
    MANIFEST_FILENAME,
    Manifest,
    check_rerun_matches,
    load_manifest,
)

STAGING_SUFFIX = ".scanny-staging"


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
        raise OutputFolderError(Code.OUTPUT_NOT_WRITABLE, f"cannot write to {output_dir}")


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
    existing_manifest: Manifest | None
    conflicting_outputs: list[str]
    stale_outputs: list[str]
    stale_staging_dirs: list[Path]


def plan_rerun(output_dir: Path, candidate: Manifest) -> RerunPlan:
    """Raises `OutputFolderError(OUTPUT_NOT_EMPTY)` for an unrelated
    nonempty folder, `manifest.BadManifestError` for an unreadable or
    schema-invalid manifest, and `manifest.ManifestMismatchError` when a
    valid manifest describes a different run."""
    entries = list_non_dot_entries(output_dir)
    if not entries:
        return RerunPlan(None, [], [], [])

    if not (output_dir / MANIFEST_FILENAME).exists():
        raise OutputFolderError(
            Code.OUTPUT_NOT_EMPTY,
            f"{output_dir} is not empty and has no {MANIFEST_FILENAME}",
        )

    existing = load_manifest(output_dir)

    known_names = {MANIFEST_FILENAME, *existing.all_expected_outputs()}
    for entry in entries:
        if entry.name in known_names:
            continue
        if _is_staging_dir_for_run(entry, existing.run_id):
            continue
        raise OutputFolderError(
            Code.OUTPUT_NOT_EMPTY,
            f"{output_dir} contains content unrelated to its manifest: {entry.name}",
        )

    check_rerun_matches(existing, candidate)

    conflicting: list[str] = []
    stale: list[str] = []
    stale_staging: list[Path] = []
    for group in existing.groups:
        group_dir = staging_dir_path(output_dir, existing.run_id, group.group_id)
        if group.status == "completed":
            conflicting.extend(
                name for name in group.expected_outputs if (output_dir / name).exists()
            )
        else:
            stale.extend(
                name for name in group.expected_outputs if (output_dir / name).exists()
            )
            if group_dir.exists():
                stale_staging.append(group_dir)

    return RerunPlan(existing, conflicting, stale, stale_staging)


def apply_recovery_cleanup(output_dir: Path, plan: RerunPlan) -> None:
    """Delete stray outputs and staging directories left by groups that
    never reached a final state in a prior run (section 3.6: "the manifest
    records progress so a rerun can safely replace an incomplete group").
    Unconditional: these files were never a protected, completed result."""
    for name in plan.stale_outputs:
        (output_dir / name).unlink(missing_ok=True)
    for staging_dir in plan.stale_staging_dirs:
        shutil.rmtree(staging_dir, ignore_errors=True)
