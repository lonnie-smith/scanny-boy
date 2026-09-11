# Scanny Boy CLI

The Python command-line program, packaged as `scanny-boy`. It contains all
file discovery, validation, sorting, grouping, RAW conversion, registration,
compositing, TIFF and manifest writing, and progress reporting; the macOS app
has none of this logic and only runs this program and reads its JSON event
stream. The interface is defined in
[`../shared/contract/`](../shared/contract/).

Core commands: `probe` (validate without writing), `prepare` (RAW to
per-frame TIFFs), `stitch` (register and composite an existing conversion's
intermediates into one TIFF per negative — the re-stitch path, since it pays
for no RAW decoding), and `run` (prepare and stitch in one invocation — the
app's normal path), plus the `roll`, `edit`, `metadata`, `export`,
`flatfield`, and `grid` command families. See
[`../shared/contract/CONTRACT.md`](../shared/contract/CONTRACT.md) for the
full command reference.

Uses the `src/` layout. Tests live next to the code they test (`*_test.py`)
and run via pytest.

## Setup

```bash
uv sync
```

Requires Python `>=3.13,<3.14` (see `.python-version` and `pyproject.toml`).
`uv sync` creates `.venv` and installs the locked dependencies from
`uv.lock`.

## Run

```bash
uv run scanny-boy probe --input /path/to/nef/folder
```

```bash
uv run scanny-boy --help
```

## Test

```bash
uv run ruff check .
uv run pytest
```

Some tests need the real sample NEFs at `../tests/fixtures/nef/`, which are
not committed — see the root README's "Sample RAW files". Those tests skip
clearly and say what they didn't test when the files are absent.

## Freeze for the macOS app

The macOS app doesn't run this Python source directly — it runs a frozen,
self-contained `ScannyBoyCLI.app` built with PyInstaller and staged into
`mac/ScannyBoy/Helpers/`. See
[`packaging/scanny_boy.spec`](packaging/scanny_boy.spec) and
[`../scripts/build-cli.sh`](../scripts/build-cli.sh), which builds and stages
it in one step:

```bash
../scripts/build-cli.sh
```

Rerun this after any change to the Python program; the staged copy is build
output and is not kept in sync automatically.
