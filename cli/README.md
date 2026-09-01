# Scanny Boy CLI

The Python command-line program, packaged as `scanny-boy`. It contains all
file discovery, validation, sorting, grouping, RAW conversion, registration,
compositing, TIFF and manifest writing, and progress reporting; the macOS app
has none of this logic and only runs this program and reads its JSON event
stream. The interface is defined in
[`../shared/contract/`](../shared/contract/).

Four commands: `probe` (validate without writing), `convert` (RAW to
per-frame TIFFs), `stitch` (register and composite an existing conversion's
intermediates into one TIFF per negative — the re-stitch path, since it pays
for no RAW decoding), and `run` (convert and stitch in one invocation — the
app's normal path). See
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

See [`../docs/IMPLEMENTATION_PLAN.md`](../docs/IMPLEMENTATION_PLAN.md)
section 4 and
[`../docs/PHASE2_IMPLEMENTATION_PLAN.md`](../docs/PHASE2_IMPLEMENTATION_PLAN.md)
section 3.6 for the authoritative contract behind `CONTRACT.md`.

## Test

```bash
uv run ruff check .
uv run pytest
```

Some tests need the real sample NEFs at `../tests/fixtures/nef/`, which are
not committed (see the root README): Phase 1's original six frames for
conversion tests, and Phase 2's gate-B stitching scans (appendix C of the
Phase 2 plan) for registration tests. Those tests skip clearly and say what
they didn't test when the files are absent.

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
