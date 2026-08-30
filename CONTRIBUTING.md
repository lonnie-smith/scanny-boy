# Contributing

Scanny Boy is a one-person project, built and maintained by one developer for
one user on one machine. This file documents how that one person works on it
— it is not an invitation for outside contributions.

The repository is public so it can be linked to and referenced, not because
it is open source. As [`LICENSE`](LICENSE) says, this code is all rights
reserved: being able to read it does not grant permission to use, copy,
modify, or distribute it, and pull requests or issues from anyone other than
the maintainer are not expected and are not a way to obtain a licence to the
project. If you've found this repository and have a question, that's fine —
but there is no contribution process here to plug into.

## How this project is actually worked on

- Work is planned in [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md)
  (Phase 1: RAW conversion) and
  [`docs/PHASE2_IMPLEMENTATION_PLAN.md`](docs/PHASE2_IMPLEMENTATION_PLAN.md)
  (Phase 2: registration and stitching), each broken into numbered chunks.
  Each chunk is one topic, one branch, and one pull request, merged in order.
- `docs/CHUNK_PROMPT.md` and `docs/PHASE2_CHUNK_PROMPT.md` are the standing
  prompts used to hand one chunk of the respective phase to an implementation
  agent at a time.
- Locked decisions live in each plan's own section 3 and are mirrored in
  [`docs/DECISIONS.md`](docs/DECISIONS.md). Changing one of them requires
  deliberately updating the plan and DECISIONS.md together, not just editing
  code until it works differently.
- `main` is protected: every change goes through a pull request with passing
  status checks, and a branch must be up to date with `main` before merging.
  There is no required-approval count, because there is only one person to
  approve.
- Human approval points (Phase 1 plan section 8, Phase 2 plan section 7) are
  real stops, not formalities — visual RAW/TIFF review, the measured
  registration constants, visual approval of a real stitched negative,
  failure/cancellation/cleanup behaviour in the finished app, and final
  sign-off before each release tag all wait for the maintainer to actually
  look before proceeding.

## Running the checks locally

```bash
cd cli && uv run ruff check . && uv run pytest
```

```bash
cd mac && xcodebuild test -scheme ScannyBoy -destination 'platform=macOS'
```

These are the same checks CI runs (`.github/workflows/ci.yml`) and are
required on every pull request before it can merge.
