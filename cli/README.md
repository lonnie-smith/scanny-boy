# Scanny Boy CLI

Python CLI, packaged with the `src/` layout. Tests live next to the code
they test (`*_test.py`), run via pytest.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Run

```bash
scanny-boy scan /some/path
```

## Test

```bash
pytest
```

## Freeze for the macOS app

See [`build/scanny_boy.spec`](build/scanny_boy.spec) and
[`../scripts/build-cli.sh`](../scripts/build-cli.sh).
