# Notes for coding agents

## Tests

The Python test suite has two tiers. Tests that really decode sample RAW
frames, stitch real scans, or run the packaged app carry the `slow` pytest
marker and **skip by default**, so an ordinary run takes about a minute:

```bash
cd cli
uv run pytest            # fast tier only; slow tests report as skipped
uv run pytest --slow     # everything, including the slow tier
uv run pytest -n 0       # serial, for when you are debugging one test
```

Default to the fast run while iterating. Run `--slow` when your change
touches RAW decoding, registration/stitching, TIFF writing, the tone curve,
or the PyInstaller packaging — that is the only way those paths are fully
exercised.

`pyproject.toml` puts `-n 8` in `addopts`, so every run is parallel by
default (pytest-xdist, eight workers for this machine's eight performance
cores). Pass `-n 0` when a failure is confusing: xdist swallows `-s` and live
output, and a crashed worker reports less usefully than a serial failure.

Two fixtures carry most of the suite's cost and are cached; know about them
before you add tests:

- **`work_dir`** (in `src/scanny_boy/conftest.py`) is a Phase 1 work
  directory with one negative. Take the fixture — it is a ~3 ms `copytree`
  of a per-session template. Call `work_dir_support.make_work_dir` directly
  only for a shape the default does not cover (more negatives, a
  non-complete status, injected gains, a frame hook), because each call
  costs about 2 seconds. If your test monkeypatches anything that changes
  what the work directory *contains*, you must call `make_work_dir` yourself,
  after the patch — the template was built long before it.
- **`synthetic_scene`** memoises on `(height, width, seed)` and returns a
  **read-only** array. Copy it if you need to modify one.

Shared test helpers live in `*_support.py` modules. Do not import one test
module from another: it drags the whole module's collection cost into every
importer.

The Swift test target has the same two tiers: the multi-minute integration
scenarios (real conversions and runs through the bundled helper) skip unless
the environment sets `SCANNY_BOY_SLOW_TESTS=1`. Probe-level and model-level
tests always run. Use the script — never a bare `xcodebuild`:

```bash
./scripts/test-mac.sh                       # ~35s, about 4 lines of output
./scripts/test-mac.sh -only-testing:ScannyBoyTests/EditModelTests
SCANNY_BOY_SLOW_TESTS=1 ./scripts/test-mac.sh
```

The script runs `xcodegen generate` first, because `mac/ScannyBoy.xcodeproj`
is generated and gitignored — a Swift file added since the last generation is
simply missing from the project, and the build then fails with a
"Cannot find type 'X' in scope" that points at correct source. It also passes
`-quiet`: a bare `xcodebuild test` prints ~1700 lines per run and buries the
one line naming the failure. `-quiet` keeps warnings, errors (with file, line
and column) and the failure summary, and drops everything else.

CI runs the same defaults (fast tiers only) through the same script; the
integration tiers skip there anyway because the sample NEFs and the built app
are absent.

## Sample fixtures

`tests/fixtures/nef/` holds the real sample NEFs (ignored by Git) plus the
gate-B stitching scans and later sessions, so it keeps growing. The six
appendix A sample files (`_DSC4638`-`_DSC4640`, `_DSC4644`-`_DSC4646`) are
therefore **not contiguous in the shared folder's catalogue**. Tests that
select them must probe/convert a staged directory holding only those files
(`stage_samples` in `cli/src/scanny_boy/sample_nef_support.py`,
`SampleFixtures.stagedDirectory()` in `mac/ScannyBoyTests/TestSupport.swift`)
rather than passing `tests/fixtures/nef/` itself.

`tests/fixtures/flatfield/bare-light.dng` is a committed synthetic bare-light
reference (regenerate with `cli/tools/generate_bare_light_dng.py`); the Swift
integration scenarios build their flat-field profile from it. Never stand in
a real film frame for it — the profile's gain map would carry the scene's
content and break registration.
