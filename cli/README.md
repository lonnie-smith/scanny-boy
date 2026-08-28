# Scanny Boy CLI

Python CLI, packaged with the `src/` layout. Tests live next to the code
they test (`*_test.py`), run via pytest.

## Setup

```bash
uv sync
```

## Run

```bash
uv run scanny-boy --help
```

## Test

```bash
uv run ruff check .
uv run pytest
```

## Freeze for the macOS app

See [`build/scanny_boy.spec`](build/scanny_boy.spec) and
[`../scripts/build-cli.sh`](../scripts/build-cli.sh).
