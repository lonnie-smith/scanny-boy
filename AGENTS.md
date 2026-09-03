# Notes for coding agents

## Tests

The Python test suite has two tiers. Tests that really decode sample RAW
frames, stitch real scans, or run the packaged app carry the `slow` pytest
marker and **skip by default**, so an ordinary run takes ~4 minutes, not ~9:

```bash
cd cli
uv run pytest            # fast tier only; slow tests report as skipped
uv run pytest --slow     # everything, including the slow tier
```

Default to the fast run while iterating. Run `--slow` when your change
touches RAW decoding, registration/stitching, TIFF writing, or the PyInstaller
packaging — that is the only way those paths are exercised.

The Swift test target behaves the same way: the multi-minute integration
scenarios (real conversions and runs through the bundled helper) skip unless
the environment sets `SCANNY_BOY_SLOW_TESTS=1`. Probe-level and model-level
tests always run.

```bash
cd mac && xcodegen generate && xcodebuild test -scheme ScannyBoy \
  -destination 'platform=macOS'   # add SCANNY_BOY_SLOW_TESTS=1 for integration
```

CI runs the same defaults (fast tiers only); the integration tiers skip
there anyway because the sample NEFs and the built app are absent.

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
