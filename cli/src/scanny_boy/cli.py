from __future__ import annotations

import argparse
import datetime
import importlib.metadata
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from scanny_boy.cancellation import sigterm_cancellation
from scanny_boy.events import (
    Code,
    ErrorEvent,
    EventWriter,
    Finished,
    ProbeResult,
    RollCreated,
    RollInfo,
    RollList,
    RollListingEntry,
    RollListingReason,
    Started,
    WarningEvent,
)
from scanny_boy.manifest import BadManifestError
from scanny_boy.pipeline import ConvertFailure, run_convert
from scanny_boy.probe import ProbeFailure, run_probe
from scanny_boy.registration import StitchError
from scanny_boy.roll_folder import RollFolderError, create_roll, scan_library
from scanny_boy.roll_manifest import (
    ROLL_MANIFEST_FILENAME,
    RollManifestUnsupportedError,
    load_roll_manifest,
)
from scanny_boy.run_pipeline import RunFailure, run_full
from scanny_boy.stitch_pipeline import run_stitch

MAX_SELECTION_FILES = 5000
MIN_PER_NEGATIVE = 1
MAX_PER_NEGATIVE = 12
MIN_JOBS = 1
MAX_JOBS = 12

# 128 + SIGTERM, per CONTRACT.md's exit-status table.
CANCELLED_EXIT_STATUS = 143


def _film_date(value: str) -> str:
    try:
        datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--film-date must be YYYY-MM-DD, got {value!r}"
        ) from exc
    return value


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
    roll_init.add_argument(
        "--per-negative", type=int, required=True, metavar="N", dest="per_negative"
    )

    roll_list = roll_subparsers.add_parser(
        "list", help="Scan the library and list its rolls."
    )
    roll_list.add_argument("--library", required=True, metavar="DIR")

    roll_info = roll_subparsers.add_parser(
        "info", help="Load and validate one roll's manifest."
    )
    roll_info.add_argument("--roll", required=True, metavar="DIR")

    probe = subparsers.add_parser(
        "probe", help="Validate a folder or selection without writing anything."
    )
    probe.add_argument("--input", required=True, metavar="DIR")
    probe.add_argument("--files", nargs="+", metavar="FILE")
    probe.add_argument("--out", metavar="DIR")
    probe.add_argument("--roll", metavar="DIR")
    probe.add_argument(
        "--per-negative", type=int, default=3, metavar="N", dest="per_negative"
    )

    convert = subparsers.add_parser(
        "convert", help="Convert a selection of NEFs to TIFFs."
    )
    convert.add_argument("--input", required=True, metavar="DIR")
    convert.add_argument("--files", nargs="+", required=True, metavar="FILE")
    convert.add_argument("--out", required=True, metavar="DIR")
    convert.add_argument(
        "--film-date", required=True, type=_film_date, metavar="YYYY-MM-DD"
    )
    convert.add_argument(
        "--per-negative", type=int, default=3, metavar="N", dest="per_negative"
    )
    convert.add_argument("--jobs", type=int, metavar="N")
    convert.add_argument("--overwrite", action="store_true")

    stitch = subparsers.add_parser(
        "stitch",
        help="Stitch a work directory's intermediates into one TIFF per negative.",
    )
    stitch.add_argument("--work", required=True, metavar="DIR")
    stitch.add_argument("--out", required=True, metavar="DIR")
    stitch.add_argument("--jobs", type=int, metavar="N")
    stitch.add_argument("--overwrite", action="store_true")
    stitch.add_argument("--allow-partial", action="store_true", dest="allow_partial")

    run = subparsers.add_parser(
        "run", help="Convert and stitch a selection of NEFs in one run."
    )
    run.add_argument("--input", required=True, metavar="DIR")
    run.add_argument("--files", nargs="+", required=True, metavar="FILE")
    run.add_argument("--out", required=True, metavar="DIR")
    run.add_argument(
        "--film-date", required=True, type=_film_date, metavar="YYYY-MM-DD"
    )
    run.add_argument(
        "--per-negative", type=int, default=3, metavar="N", dest="per_negative"
    )
    run.add_argument("--jobs", type=int, metavar="N")
    run.add_argument("--overwrite", action="store_true")
    run.add_argument("--work", metavar="DIR")
    run.add_argument(
        "--keep-intermediates", action="store_true", dest="keep_intermediates"
    )

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
                Path(args.out),
                run_id=run_id,
                overwrite=args.overwrite,
                allow_partial=args.allow_partial,
                jobs=jobs,
                cancel=cancel,
                emit=writer.write,
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
    """The `roll init` / `roll list` / `roll info` subcommands (section
    3.5). Each mirrors the other commands' started/finished bracketing;
    none carries a `run_id`, since none is a pipeline run."""
    if args.roll_command == "init":
        writer.write(Started(command="roll init"))
        try:
            roll_dir = create_roll(Path(args.library), args.name, args.per_negative)
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

    # info
    writer.write(Started(command="roll info"))
    roll_dir = Path(args.roll)
    if not (roll_dir / ROLL_MANIFEST_FILENAME).exists():
        writer.write(
            ErrorEvent(
                code=Code.ROLL_NOT_FOUND,
                message=f"{roll_dir} has no {ROLL_MANIFEST_FILENAME}",
            )
        )
        writer.write(Finished(status="failed", exit_status=1))
        return 1
    try:
        manifest = load_roll_manifest(roll_dir)
    except (BadManifestError, RollManifestUnsupportedError) as exc:
        writer.write(ErrorEvent(code=exc.code, message=exc.message))
        writer.write(Finished(status="failed", exit_status=1))
        return 1
    writer.write(RollInfo(manifest=manifest.to_dict()))
    writer.write(Finished(status="success", exit_status=0))
    return 0


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
                Path(args.out),
                datetime.date.fromisoformat(args.film_date),
                args.per_negative,
                run_id=run_id,
                work_dir=Path(args.work) if args.work else None,
                keep_intermediates=args.keep_intermediates,
                overwrite=args.overwrite,
                jobs=jobs,
                cancel=cancel,
                emit=writer.write,
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
    # manifest, which already recorded it.
    per_negative = getattr(args, "per_negative", None)
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

    if args.command == "roll":
        return _run_roll_command(args, writer)

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
                datetime.date.fromisoformat(args.film_date),
                args.per_negative,
                run_id=run_id,
                overwrite=args.overwrite,
                jobs=jobs,
                cancel=cancel,
                emit=writer.write,
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
