from __future__ import annotations

import argparse
import datetime
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
    Started,
    WarningEvent,
)
from scanny_boy.pipeline import ConvertFailure, run_convert
from scanny_boy.probe import ProbeFailure, run_probe

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
    subparsers = parser.add_subparsers(dest="command", required=True)

    probe = subparsers.add_parser(
        "probe", help="Validate a folder or selection without writing anything."
    )
    probe.add_argument("--input", required=True, metavar="DIR")
    probe.add_argument("--files", nargs="+", metavar="FILE")
    probe.add_argument("--out", metavar="DIR")
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

    return parser


def _exit_code(exc: SystemExit) -> int:
    return exc.code if isinstance(exc.code, int) else 2


def _usage_error(parser: argparse.ArgumentParser, message: str) -> int:
    try:
        parser.error(message)
    except SystemExit as exc:
        return _exit_code(exc)
    raise AssertionError("argparse.ArgumentParser.error() always raises SystemExit")


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

    if not (MIN_PER_NEGATIVE <= args.per_negative <= MAX_PER_NEGATIVE):
        writer.write(
            ErrorEvent(
                code=Code.INVALID_PER_NEGATIVE,
                message=(
                    f"--per-negative must be between {MIN_PER_NEGATIVE} and "
                    f"{MAX_PER_NEGATIVE}, got {args.per_negative}"
                ),
            )
        )
        return 2

    jobs = getattr(args, "jobs", None)
    if jobs is not None and not (MIN_JOBS <= jobs <= MAX_JOBS):
        return _usage_error(
            parser, f"--jobs must be between {MIN_JOBS} and {MAX_JOBS}, got {jobs}"
        )

    if args.command == "probe":
        writer.write(Started(command="probe"))
        emitted_warnings: list[str] = []

        def on_warning(code: Code, message: str) -> None:
            writer.write(WarningEvent(code=code, message=message))
            emitted_warnings.append(code.value)

        try:
            outcome = run_probe(
                Path(args.input), files, args.per_negative, on_warning=on_warning
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
            )
        )
        writer.write(Finished(status="success", exit_status=0))
        return 0

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
