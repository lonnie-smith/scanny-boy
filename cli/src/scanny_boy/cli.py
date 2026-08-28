import argparse


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog="scanny-boy")


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
