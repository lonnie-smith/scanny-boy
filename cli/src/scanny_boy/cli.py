import argparse
import dataclasses
import json
import sys

from scanny_boy.core.scanner import scan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scanny-boy")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Scan a path")
    scan_parser.add_argument("path")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "scan":
        result = scan(args.path)
        json.dump(dataclasses.asdict(result), sys.stdout)
        sys.stdout.write("\n")
        return 0 if result.ok else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
