from __future__ import annotations

import argparse
import importlib.metadata
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from scanny_boy.apply_metadata import ApplyMetadataFailure, run_apply_metadata
from scanny_boy.cancellation import sigterm_cancellation
from scanny_boy.edits import EditFailure, run_edit_delete, run_edit_rotate
from scanny_boy.events import (
    Code,
    EditRecorded,
    ErrorEvent,
    EventWriter,
    Finished,
    FlatFieldCreated,
    FlatFieldDeleted,
    FlatFieldList,
    NegativeDeleted,
    ProbeResult,
    RollCreated,
    RollInfo,
    RollList,
    RollListingEntry,
    RollListingReason,
    RollRenamed,
    Started,
    WarningEvent,
)
from scanny_boy.exporter import ExportFailure, run_export
from scanny_boy.flatfield import (
    FlatFieldError,
    create_profile,
    flatfield_profile_summary,
)
from scanny_boy.library import repo
from scanny_boy.library.db import LibraryDBError
from scanny_boy.manifest import BadManifestError
from scanny_boy.metadata import UnreadableRawError, UnsupportedRawError
from scanny_boy.pipeline import ConvertFailure, run_convert
from scanny_boy.probe import ProbeFailure, run_probe
from scanny_boy.registration import StitchError
from scanny_boy.roll_folder import (
    RollFolderError,
    create_roll,
    rename_roll,
    scan_library,
)
from scanny_boy.roll_manifest import load_roll_manifest
from scanny_boy.run_pipeline import RunFailure, run_full
from scanny_boy.stitch_pipeline import run_stitch

MAX_SELECTION_FILES = 5000
MIN_PER_NEGATIVE = 1
MAX_PER_NEGATIVE = 12
MIN_JOBS = 1
MAX_JOBS = 12

# 128 + SIGTERM, per CONTRACT.md's exit-status table.
CANCELLED_EXIT_STATUS = 143


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scanny-boy")
    # A diagnostic, not part of the event stream: it prints one plain-text
    # line and exits 0. The app never calls it; the packaged checks of
    # section 5.2 do, as the cheapest proof that the frozen bundle starts
    # and can read its own package metadata.
    parser.add_argument(
        "--version",
        action="version",
        version=f"scanny-boy {importlib.metadata.version('scanny-boy')}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    roll = subparsers.add_parser("roll", help="Manage rolls in the library.")
    roll_subparsers = roll.add_subparsers(dest="roll_command", required=True)

    roll_init = roll_subparsers.add_parser("init", help="Create a new roll.")
    roll_init.add_argument("--library", required=True, metavar="DIR")
    roll_init.add_argument("--name", required=True, metavar="NAME")

    roll_list = roll_subparsers.add_parser(
        "list", help="Scan the library and list its rolls."
    )
    roll_list.add_argument("--library", required=True, metavar="DIR")

    roll_info = roll_subparsers.add_parser(
        "info", help="Load and validate one roll's manifest."
    )
    roll_info.add_argument("--roll", required=True, metavar="DIR")

    roll_rename = roll_subparsers.add_parser("rename", help="Rename a roll.")
    roll_rename.add_argument("--roll", required=True, metavar="DIR")
    roll_rename.add_argument("--name", required=True, metavar="NAME")

    probe = subparsers.add_parser(
        "probe", help="Validate a folder or selection without writing anything."
    )
    probe.add_argument("--input", required=True, metavar="DIR")
    probe.add_argument("--files", nargs="+", metavar="FILE")
    probe.add_argument("--out", metavar="DIR")
    probe.add_argument("--roll", metavar="DIR")
    probe.add_argument(
        "--per-negative",
        type=int,
        default=None,
        metavar="N",
        dest="per_negative",
        help="required with --files: the scans stitched into each negative",
    )
    probe.add_argument("--flatfield", metavar="PROFILE_ID")

    convert = subparsers.add_parser(
        "convert", help="Convert a selection of NEFs to TIFFs."
    )
    convert.add_argument("--input", required=True, metavar="DIR")
    convert.add_argument("--files", nargs="+", required=True, metavar="FILE")
    convert.add_argument("--out", required=True, metavar="DIR")
    convert.add_argument(
        "--per-negative", type=int, required=True, metavar="N", dest="per_negative"
    )
    convert.add_argument("--jobs", type=int, metavar="N")
    convert.add_argument("--overwrite", action="store_true")
    convert.add_argument("--flatfield", metavar="PROFILE_ID")

    stitch = subparsers.add_parser(
        "stitch",
        help="Stitch a work directory's intermediates into one TIFF per negative.",
    )
    stitch.add_argument("--work", required=True, metavar="DIR")
    stitch.add_argument("--roll", required=True, metavar="DIR")
    stitch.add_argument("--jobs", type=int, metavar="N")
    stitch.add_argument("--overwrite", action="store_true")
    stitch.add_argument("--allow-partial", action="store_true", dest="allow_partial")
    stitch.add_argument("--negatives", nargs="+", metavar="ID")

    run = subparsers.add_parser(
        "run", help="Convert and stitch a selection of NEFs in one run."
    )
    run.add_argument("--input", required=True, metavar="DIR")
    run.add_argument("--files", nargs="+", required=True, metavar="FILE")
    run.add_argument("--roll", required=True, metavar="DIR")
    run.add_argument(
        "--per-negative", type=int, required=True, metavar="N", dest="per_negative"
    )
    run.add_argument("--jobs", type=int, metavar="N")
    run.add_argument("--work", metavar="DIR")
    run.add_argument(
        "--skip-sources", nargs="+", metavar="FILE", dest="skip_sources", default=[]
    )
    run.add_argument("--flatfield", metavar="PROFILE_ID")

    flatfield = subparsers.add_parser("flatfield", help="Manage flat-field profiles.")
    flatfield_subparsers = flatfield.add_subparsers(
        dest="flatfield_command", required=True
    )

    flatfield_create = flatfield_subparsers.add_parser(
        "create", help="Build a gain map from a bare light source reference NEF."
    )
    flatfield_create.add_argument("--reference", required=True, metavar="FILE")
    flatfield_create.add_argument("--name", required=True, metavar="NAME")

    flatfield_subparsers.add_parser("list", help="List the flat-field profiles.")

    flatfield_delete = flatfield_subparsers.add_parser(
        "delete", help="Delete one flat-field profile."
    )
    flatfield_delete.add_argument("--profile", required=True, metavar="ID")

    apply_metadata = subparsers.add_parser(
        "apply-metadata",
        help="Write dirty negatives' intended capture times into their TIFFs.",
    )
    apply_metadata.add_argument("--roll", required=True, metavar="DIR")

    edit = subparsers.add_parser(
        "edit", help="Record nondestructive edits against a roll's negatives."
    )
    edit_subparsers = edit.add_subparsers(dest="edit_command", required=True)

    edit_rotate = edit_subparsers.add_parser(
        "rotate", help="Record a 90-degree rotation of one negative."
    )
    edit_rotate.add_argument("--roll", required=True, metavar="DIR")
    edit_rotate.add_argument("--negative", required=True, metavar="ID")
    edit_rotate.add_argument(
        "--direction",
        required=True,
        choices=["cw", "ccw"],
        help="cw rotates the image 90 degrees clockwise, ccw counter-clockwise",
    )

    edit_delete = edit_subparsers.add_parser(
        "delete", help="Delete one negative's record, TIFF, and preview."
    )
    edit_delete.add_argument("--roll", required=True, metavar="DIR")
    edit_delete.add_argument("--negative", required=True, metavar="ID")

    export = subparsers.add_parser(
        "export",
        help="Write TIFFs with each negative's edits applied into an output folder.",
    )
    export.add_argument("--roll", required=True, metavar="DIR")
    export.add_argument("--output", required=True, metavar="DIR")
    export.add_argument("--negatives", nargs="+", metavar="ID", default=[])

    return parser


def _exit_code(exc: SystemExit) -> int:
    return exc.code if isinstance(exc.code, int) else 2


def _usage_error(parser: argparse.ArgumentParser, message: str) -> int:
    try:
        parser.error(message)
    except SystemExit as exc:
        return _exit_code(exc)
    raise AssertionError("argparse.ArgumentParser.error() always raises SystemExit")


def _run_stitch_command(args, writer: EventWriter, jobs: int | None) -> int:
    """The `stitch` subcommand: mirrors `convert`'s event and exit-status
    shape exactly, over `run_stitch` instead of `run_convert`."""
    run_id = str(uuid.uuid4())
    writer.write(Started(command="stitch", run_id=run_id))

    try:
        with sigterm_cancellation() as cancel:
            outcome = run_stitch(
                Path(args.work),
                Path(args.roll),
                run_id=run_id,
                overwrite=args.overwrite,
                allow_partial=args.allow_partial,
                jobs=jobs,
                cancel=cancel,
                emit=writer.write,
                negatives=args.negatives,
            )
    except StitchError as exc:
        writer.write(ErrorEvent(run_id=run_id, code=exc.code, message=exc.message))
        writer.write(Finished(run_id=run_id, status="failed", exit_status=1))
        return 1

    if outcome.status == "cancelled":
        writer.write(
            ErrorEvent(
                run_id=run_id,
                code=Code.CANCELLED,
                message=(
                    "cancelled at the user's request; completed negatives were "
                    "kept and the negative in progress was discarded"
                ),
            )
        )
        writer.write(
            Finished(
                run_id=run_id, status="cancelled", exit_status=CANCELLED_EXIT_STATUS
            )
        )
        return CANCELLED_EXIT_STATUS

    exit_status = 0 if outcome.status == "complete" else 1
    writer.write(
        Finished(
            run_id=run_id,
            status="success" if exit_status == 0 else "failed",
            exit_status=exit_status,
        )
    )
    return exit_status


def _roll_listing_entry(listing) -> RollListingEntry:
    reason = (
        RollListingReason(code=listing.reason[0], message=listing.reason[1])
        if listing.reason is not None
        else None
    )
    return RollListingEntry(
        path=str(listing.path),
        status=listing.status,
        reason=reason,
        roll_id=listing.roll_id,
        roll_name=listing.roll_name,
        negative_count=listing.negative_count,
    )


def _run_roll_command(args, writer: EventWriter) -> int:
    """The `roll init` / `roll list` / `roll info` / `roll rename`
    subcommands (section 3.5; `rename` added at section 5.5). Each mirrors
    the other commands' started/finished bracketing; none carries a
    `run_id`, since none is a pipeline run."""
    if args.roll_command == "init":
        writer.write(Started(command="roll init"))
        try:
            roll_dir = create_roll(Path(args.library), args.name)
        except RollFolderError as exc:
            writer.write(ErrorEvent(code=exc.code, message=exc.message))
            writer.write(Finished(status="failed", exit_status=1))
            return 1
        manifest = load_roll_manifest(roll_dir)
        writer.write(
            RollCreated(
                roll_id=manifest.roll_id,
                roll_name=manifest.roll_name,
                path=str(roll_dir),
            )
        )
        writer.write(Finished(status="success", exit_status=0))
        return 0

    if args.roll_command == "list":
        writer.write(Started(command="roll list"))
        listings = scan_library(Path(args.library))
        writer.write(
            RollList(rolls=[_roll_listing_entry(listing) for listing in listings])
        )
        writer.write(Finished(status="success", exit_status=0))
        return 0

    if args.roll_command == "rename":
        writer.write(Started(command="roll rename"))
        roll_dir = Path(args.roll)
        if not repo.roll_registered(roll_dir):
            writer.write(
                ErrorEvent(
                    code=Code.ROLL_NOT_FOUND,
                    message=f"{roll_dir} is not a registered roll",
                )
            )
            writer.write(Finished(status="failed", exit_status=1))
            return 1
        try:
            new_dir = rename_roll(roll_dir, args.name)
        except RollFolderError as exc:
            writer.write(ErrorEvent(code=exc.code, message=exc.message))
            writer.write(Finished(status="failed", exit_status=1))
            return 1
        manifest = load_roll_manifest(new_dir)
        writer.write(
            RollRenamed(
                roll_id=manifest.roll_id,
                roll_name=manifest.roll_name,
                path=str(new_dir),
            )
        )
        writer.write(Finished(status="success", exit_status=0))
        return 0

    # info
    writer.write(Started(command="roll info"))
    roll_dir = Path(args.roll)
    if not repo.roll_registered(roll_dir):
        writer.write(
            ErrorEvent(
                code=Code.ROLL_NOT_FOUND,
                message=f"{roll_dir} is not a registered roll",
            )
        )
        writer.write(Finished(status="failed", exit_status=1))
        return 1
    try:
        manifest = load_roll_manifest(roll_dir)
    except (BadManifestError, repo.RollNotRegisteredError) as exc:
        writer.write(ErrorEvent(code=exc.code, message=exc.message))
        writer.write(Finished(status="failed", exit_status=1))
        return 1
    info = manifest.to_dict()
    # Net rotation is derived state — the ops log's replay — so it is
    # augmented here rather than stored in the negatives' own shape.
    for negative in info["negatives"]:
        negative["rotation_quarter_turns"] = repo.net_rotation_quarter_turns(
            roll_dir, negative["negative_id"]
        )
    writer.write(RollInfo(manifest=info))
    writer.write(Finished(status="success", exit_status=0))
    return 0


def _run_edit_command(args, writer: EventWriter) -> int:
    """The `edit rotate` subcommand: records the op and refreshes the
    preview; brackets like every other subcommand."""
    writer.write(Started(command=f"edit {args.edit_command}"))
    if args.edit_command == "rotate":
        try:
            fields = run_edit_rotate(
                Path(args.roll),
                args.negative,
                args.direction,
                emit=writer.write,
            )
        except EditFailure as exc:
            writer.write(ErrorEvent(code=exc.code, message=exc.message))
            writer.write(Finished(status="failed", exit_status=1))
            return 1
        writer.write(EditRecorded(**fields))
        writer.write(Finished(status="success", exit_status=0))
        return 0

    if args.edit_command == "delete":
        try:
            fields = run_edit_delete(
                Path(args.roll),
                args.negative,
                emit=writer.write,
            )
        except EditFailure as exc:
            writer.write(ErrorEvent(code=exc.code, message=exc.message))
            writer.write(Finished(status="failed", exit_status=1))
            return 1
        writer.write(NegativeDeleted(**fields))
        writer.write(Finished(status="success", exit_status=0))
        return 0
    raise AssertionError(f"unhandled edit command {args.edit_command!r}")


def _run_flatfield_command(args, writer: EventWriter) -> int:
    """The `flatfield create` / `flatfield list` / `flatfield delete`
    subcommands: each mirrors `roll init`/`roll list`'s started/finished
    bracketing and carries no `run_id`, since none is a pipeline run."""
    if args.flatfield_command == "create":
        writer.write(Started(command="flatfield create"))
        try:
            profile = create_profile(Path(args.reference), args.name)
        except FlatFieldError as exc:
            writer.write(ErrorEvent(code=exc.code, message=exc.message))
            writer.write(Finished(status="failed", exit_status=1))
            return 1
        except UnsupportedRawError:
            writer.write(
                ErrorEvent(
                    code=Code.UNSUPPORTED_RAW,
                    message=f"{args.reference} cannot be read by LibRaw; a flat-field "
                    "reference must be a NEF",
                )
            )
            writer.write(Finished(status="failed", exit_status=1))
            return 1
        except UnreadableRawError:
            writer.write(
                ErrorEvent(
                    code=Code.UNREADABLE_RAW,
                    message=f"{args.reference} could not be decoded",
                )
            )
            writer.write(Finished(status="failed", exit_status=1))
            return 1
        writer.write(
            FlatFieldCreated(profile=flatfield_profile_summary(profile))
        )
        writer.write(Finished(status="success", exit_status=0))
        return 0

    if args.flatfield_command == "list":
        writer.write(Started(command="flatfield list"))
        profiles = repo.list_flatfield_profiles()
        writer.write(
            FlatFieldList(
                profiles=[flatfield_profile_summary(p) for p in profiles]
            )
        )
        writer.write(Finished(status="success", exit_status=0))
        return 0

    # delete
    writer.write(Started(command="flatfield delete"))
    try:
        profile = repo.load_flatfield_profile(args.profile)
    except FlatFieldError as exc:
        writer.write(ErrorEvent(code=exc.code, message=exc.message))
        writer.write(Finished(status="failed", exit_status=1))
        return 1

    users = repo.rolls_using_flatfield(args.profile)
    if users:
        # The gain map is the only thing that could reproduce those rolls.
        writer.write(
            ErrorEvent(
                code=Code.FLATFIELD_PROFILE_IN_USE,
                message=(
                    f"profile {profile.name!r} is locked into "
                    f"{len(users)} roll(s) by their processing invariants "
                    "and cannot be deleted"
                ),
            )
        )
        writer.write(Finished(status="failed", exit_status=1))
        return 1

    repo.delete_flatfield_profile(args.profile)
    gain_map_path = Path(profile.gain_map_path)
    if gain_map_path.exists():
        gain_map_path.unlink()
    writer.write(FlatFieldDeleted(profile_id=args.profile))
    writer.write(Finished(status="success", exit_status=0))
    return 0


def _run_export_command(args, writer: EventWriter) -> int:
    writer.write(Started(command="export"))
    try:
        outcome = run_export(
            Path(args.roll),
            Path(args.output),
            args.negatives,
            emit=writer.write,
        )
    except ExportFailure as exc:
        writer.write(ErrorEvent(code=exc.code, message=exc.message))
        writer.write(Finished(status="failed", exit_status=1))
        return 1
    exit_status = 0 if not outcome.failed else 1
    writer.write(
        Finished(
            status="success" if exit_status == 0 else "failed",
            exit_status=exit_status,
        )
    )
    return exit_status


def _run_run_command(
    args, writer: EventWriter, files: list[str] | None, jobs: int | None
) -> int:
    """The `run` subcommand: mirrors `convert`'s and `stitch`'s event and
    exit-status shape exactly, over `run_full` instead of `run_convert` or
    `run_stitch`."""
    run_id = str(uuid.uuid4())
    writer.write(Started(command="run", run_id=run_id))

    try:
        with sigterm_cancellation() as cancel:
            outcome = run_full(
                Path(args.input),
                files,
                Path(args.roll),
                args.per_negative,
                run_id=run_id,
                work_dir=Path(args.work) if args.work else None,
                skip_sources=args.skip_sources,
                jobs=jobs,
                cancel=cancel,
                emit=writer.write,
                flatfield_profile_id=args.flatfield,
            )
    except RunFailure as exc:
        writer.write(ErrorEvent(run_id=run_id, code=exc.code, message=exc.message))
        writer.write(Finished(run_id=run_id, status="failed", exit_status=1))
        return 1

    if outcome.status == "cancelled":
        writer.write(
            ErrorEvent(
                run_id=run_id,
                code=Code.CANCELLED,
                message=(
                    "cancelled at the user's request; completed negatives were "
                    "kept and the negative in progress was discarded"
                ),
            )
        )
        writer.write(
            Finished(
                run_id=run_id, status="cancelled", exit_status=CANCELLED_EXIT_STATUS
            )
        )
        return CANCELLED_EXIT_STATUS

    exit_status = 0 if outcome.status == "complete" else 1
    writer.write(
        Finished(
            run_id=run_id,
            status="success" if exit_status == 0 else "failed",
            exit_status=exit_status,
        )
    )
    return exit_status


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return _exit_code(exc)

    writer = EventWriter(sys.stdout)

    files = getattr(args, "files", None)
    if files is not None and len(files) > MAX_SELECTION_FILES:
        return _usage_error(
            parser,
            f"selection of {len(files)} files exceeds the maximum of "
            f"{MAX_SELECTION_FILES}",
        )

    # `stitch` takes no --per-negative: it reads the grouping from the work
    # manifest, which already recorded it. `probe` needs one only when it
    # has a selection to group — a catalogue-only probe has no negatives.
    per_negative = getattr(args, "per_negative", None)
    if args.command == "probe" and files is not None and per_negative is None:
        return _usage_error(parser, "probe --files requires --per-negative")
    if per_negative is not None and not (
        MIN_PER_NEGATIVE <= per_negative <= MAX_PER_NEGATIVE
    ):
        writer.write(
            ErrorEvent(
                code=Code.INVALID_PER_NEGATIVE,
                message=(
                    f"--per-negative must be between {MIN_PER_NEGATIVE} and "
                    f"{MAX_PER_NEGATIVE}, got {per_negative}"
                ),
            )
        )
        return 2

    jobs = getattr(args, "jobs", None)
    if jobs is not None and not (MIN_JOBS <= jobs <= MAX_JOBS):
        return _usage_error(
            parser, f"--jobs must be between {MIN_JOBS} and {MAX_JOBS}, got {jobs}"
        )

    try:
        return _dispatch_command(args, writer, files, jobs)
    except LibraryDBError as exc:
        # A database this helper cannot open is the one failure that can
        # strike every command alike, so it gets its own sentence rather
        # than Alembic's.
        writer.write(ErrorEvent(code=exc.code, message=exc.message))
        writer.write(Finished(status="failed", exit_status=1))
        return 1
    except Exception as exc:  # noqa: BLE001 — a crash must still be legible
        # Last resort: an unexpected exception reached the top of the
        # command. Without this, stdout stops after `started` and the app
        # can only say "produced no result"; with it, the user sees the
        # exception itself and the exit is an ordinary failed one.
        writer.write(
            ErrorEvent(
                code=Code.INTERNAL_ERROR,
                message=f"unexpected {type(exc).__name__}: {exc}",
            )
        )
        writer.write(Finished(status="failed", exit_status=1))
        return 1


def _dispatch_command(
    args, writer: EventWriter, files: list[str] | None, jobs: int | None
) -> int:
    if args.command == "roll":
        return _run_roll_command(args, writer)

    if args.command == "edit":
        return _run_edit_command(args, writer)

    if args.command == "flatfield":
        return _run_flatfield_command(args, writer)

    if args.command == "export":
        return _run_export_command(args, writer)

    if args.command == "apply-metadata":
        writer.write(Started(command="apply-metadata"))
        try:
            outcome = run_apply_metadata(Path(args.roll), emit=writer.write)
        except ApplyMetadataFailure as exc:
            writer.write(ErrorEvent(code=exc.code, message=exc.message))
            writer.write(Finished(status="failed", exit_status=1))
            return 1
        exit_status = 0 if not outcome.skipped else 1
        writer.write(
            Finished(
                status="success" if exit_status == 0 else "failed",
                exit_status=exit_status,
            )
        )
        return exit_status

    if args.command == "probe":
        writer.write(Started(command="probe"))
        emitted_warnings: list[str] = []

        def on_warning(code: Code, message: str) -> None:
            writer.write(WarningEvent(code=code, message=message))
            emitted_warnings.append(code.value)

        try:
            outcome = run_probe(
                Path(args.input),
                files,
                args.per_negative,
                out_dir=Path(args.out) if args.out else None,
                roll_dir=Path(args.roll) if args.roll else None,
                flatfield_profile_id=args.flatfield,
                on_warning=on_warning,
            )
        except ProbeFailure as exc:
            writer.write(ErrorEvent(code=exc.code, message=exc.message))
            writer.write(Finished(status="failed", exit_status=1))
            return 1
        writer.write(
            ProbeResult(
                catalogue=outcome.catalogue,
                warnings=emitted_warnings,
                groups=outcome.groups,
                output_conflicts=outcome.output_conflicts,
                estimated_required_bytes=outcome.estimated_required_bytes,
                available_bytes=outcome.available_bytes,
                roll_overlap=outcome.roll_overlap,
            )
        )
        writer.write(Finished(status="success", exit_status=0))
        return 0

    if args.command == "stitch":
        return _run_stitch_command(args, writer, jobs)

    if args.command == "run":
        return _run_run_command(args, writer, files, jobs)

    # convert
    run_id = str(uuid.uuid4())
    writer.write(Started(command="convert", run_id=run_id))

    # The SIGTERM handler is installed for the whole conversion and
    # removed afterwards; it only sets the token's flag, and every
    # deletion, manifest update, and final event below happens on this
    # thread through ordinary control flow (section 3.8).
    try:
        with sigterm_cancellation() as cancel:
            outcome = run_convert(
                Path(args.input),
                files,
                Path(args.out),
                args.per_negative,
                run_id=run_id,
                overwrite=args.overwrite,
                jobs=jobs,
                cancel=cancel,
                emit=writer.write,
                flatfield_profile_id=args.flatfield,
            )
    except ConvertFailure as exc:
        writer.write(ErrorEvent(run_id=run_id, code=exc.code, message=exc.message))
        writer.write(Finished(run_id=run_id, status="failed", exit_status=1))
        return 1

    if outcome.status == "cancelled":
        writer.write(
            ErrorEvent(
                run_id=run_id,
                code=Code.CANCELLED,
                message=(
                    "cancelled at the user's request; completed negatives were "
                    "kept and the negative in progress was discarded"
                ),
            )
        )
        writer.write(
            Finished(
                run_id=run_id, status="cancelled", exit_status=CANCELLED_EXIT_STATUS
            )
        )
        return CANCELLED_EXIT_STATUS

    exit_status = 0 if outcome.status == "complete" else 1
    writer.write(
        Finished(
            run_id=run_id,
            status="success" if exit_status == 0 else "failed",
            exit_status=exit_status,
        )
    )
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
