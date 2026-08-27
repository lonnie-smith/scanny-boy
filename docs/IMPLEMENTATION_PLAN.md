# Scanny Boy — Phase 1 implementation plan

**Last reviewed:** 2026-08-27

## 1. Goal

Build a macOS app that:

1. Opens one folder of Nikon Z f `.NEF` files.
2. Lets the user select one uninterrupted range in capture order.
3. Groups the range into negatives using 1–12 shots per negative (default: 3).
4. Requires the date on which the film was exposed.
5. Writes one 16-bit RGB TIFF for every selected NEF.
6. Writes a versioned JSON manifest that records grouping, source hashes,
   conversion settings, outputs, and failures.

The TIFFs are high-quality intermediate files for a later stitching phase.
Phase 1 does not stitch or invert negatives.

The first release is for one user, on this Apple-silicon Mac, built locally.
App Store distribution, Developer ID distribution, sandboxing, notarisation,
and Intel support are not Phase 1 requirements.

## 2. Facts checked before implementation

These facts were checked against current documentation and the local machine:

- Local `main` and `lonnie-smith/scanny-boy` both point to commit
  `dddfbf6d3985d6a524b23d817d7e1e1ec54e715d`. The local checkout has no
  `origin` remote.
- GitHub CLI 2.98.0 is installed and authenticated.
- XcodeGen 2.46.0 is installed.
- macOS is 14.6.1 on Apple silicon; Xcode is 16.2; Swift is 6.0.3; Python is 3.13.3;
  uv is 0.11.7.
- The local sample-NEF directory exists but is empty.
- rawpy 0.27.0 uses LibRaw 0.22.1 and supplies a Python 3.13 Apple-silicon package.
- LibRaw 0.22.1 supports Nikon Z f standard/lossless-compressed NEFs. It does
  not support Z f High Efficiency or High Efficiency* NEFs. This project
  requires 14-bit lossless-compressed NEFs.
- Since rawpy 0.21, separate Python threads can decode RAW files at the same
  time safely. Thread workers are therefore preferred over process workers.
- `tifffile` can write RGB16, Deflate compression, horizontal prediction,
  ordinary TIFF tags, and an embedded colour profile.
- `tifffile` cannot create the nested EXIF directory needed for
  `DateTimeOriginal` and related EXIF fields. `tifftools` can read and write
  nested TIFF directories and is pure Python.
- Python's `os.cpu_count()` does not report physical cores.
  `os.process_cpu_count()` reports logical CPUs available to the process.
- Standard GitHub-hosted runners are free for public repositories. Larger
  runners and storage are not covered by that statement.
- GitHub's `macos-14` runner is deprecated and will retire on 2026-11-02.
  CI must use `macos-15`.
- A bundled macOS executable belongs in an app's helper area, not in
  `Contents/Resources`.

## 3. Decisions implementation agents must preserve

Change these only after asking the user.

### 3.1 Product and repository

- The Python command-line program contains all file discovery, validation,
  sorting, grouping, conversion, manifest, and progress logic.
- Swift provides the macOS interface and starts the Python program.
- The repository is public on GitHub, but the project code is **all rights
  reserved**. Do not use an MIT, Apache, BSD, or other open-source licence.
- The root `LICENSE` must contain exactly:

  ```text
  Copyright (c) 2026 Lonnie Smith

  All rights reserved.

  No part of this repository or its contents may be used, copied, modified,
  merged, published, distributed, sublicensed, or sold without the prior
  written permission of the copyright holder.
  ```

  Public visibility does not grant reuse rights. Do not add an SPDX licence
  identifier or open-source badge.
- Bundled third-party assets keep their own licences. Record them in
  `THIRD_PARTY_NOTICES.md` (the CC0 ICC profile is added in Chunk 3). Python
  dependencies are used under their upstream licences but are not relicensed
  here.
- `project.yml` is the source for the Xcode project. Generated
  `.xcodeproj` files are not committed.
- `main` requires a pull request and current CI checks. No approval count is
  required because this is a one-person project. Force-pushes and branch
  deletion are blocked.
- Each implementation chunk is one branch and one pull request. Merge chunks
  in order.

### 3.2 Input rules

- Accept `.nef` case-insensitively from one folder, without recursion.
- Resolve paths and reject duplicates or files outside the chosen input
  folder.
- The selected files must be one uninterrupted range in the folder's official
  order.
- Shots per negative accepts 1–12 and defaults to 3.
- The selected count must be divisible by shots per negative.
- The camera workflow requires:
  - 14-bit lossless-compressed NEF;
  - fixed manual exposure;
  - fixed manual white balance;
  - one lens and focal length;
  - one camera orientation.
- Open each selected RAW with rawpy before conversion. Map LibRaw's unsupported
  file error to `UNSUPPORTED_RAW` and explain that Z f HE/HE* files must be
  recaptured as 14-bit lossless-compressed NEFs.
- Read `raw.camera_whitebalance` from every selected file. Require four finite,
  positive multipliers. Normalise each vector by its first green multiplier
  and compare corresponding values with relative and absolute tolerance
  `1e-6`; do not rely only on an EXIF Manual/Auto flag.
- Compare the scaled white-balance values, shutter speed, aperture, ISO,
  lens, focal length, and source orientation across the selection. Stop and
  identify missing or differing values.

### 3.3 Sorting

- Normal rule: sort by NEF `DateTimeOriginal`, including
  `SubSecTimeOriginal` when present. Use natural filename order to break an
  exact tie.
- Determine one order for the complete folder catalogue. If any NEF in that
  catalogue lacks a usable capture timestamp, sort the complete catalogue by
  natural filename and show a warning, even when the missing timestamp is
  outside the selected range.
- Never use a mixed timestamp/filename comparison.
- After sorting, verify that the selection is an uninterrupted range within
  the complete folder catalogue.
- Do not implement the previously proposed "uneven time gap" warning. There is
  no measured threshold for it.

### 3.4 Pixel output

- Write one TIFF per source frame, named from that frame:
  `DSC_0042.NEF` becomes `DSC_0042.tif`.
- Always write three-channel, unsigned 16-bit RGB.
- Honour the NEF orientation while decoding so pixels display upright. Write
  TIFF Orientation as `1`.
- Use source Orientation only for setup-consistency validation and rawpy
  rotation. Record output dimensions from the final postprocess array shape.
- Encode in ROMM RGB, commonly called ProPhoto RGB, using the standard
  transfer curve.
- Embed a vetted ROMM-compatible ICC colour profile in every TIFF. Never
  silently write untagged ROMM data.
- Use lossless Deflate compression with horizontal prediction. File-size
  savings are measured, not assumed.
- Set `tifffile`'s compression `maxworkers=1`; outer RAW threads own
  concurrency.
- Preserve fixed exposure across the run by disabling both histogram-based
  brightening and content-dependent maximum adjustment.

Required rawpy settings:

```python
RAW_PARAMS = {
    "output_bps": 16,
    "gamma": (1.8, 16),
    "no_auto_bright": True,
    "adjust_maximum_thr": 0.0,
    "use_camera_wb": True,
    "use_auto_wb": False,
    "output_color": rawpy.ColorSpace.ProPhoto,
    "demosaic_algorithm": rawpy.DemosaicAlgorithm.AHD,
    "four_color_rgb": False,
    "median_filter_passes": 0,
    "highlight_mode": rawpy.HighlightMode.Clip,
}
```

Do not set `user_flip=0`; rawpy should apply the source orientation.

`gamma=(1.8, 16)` is the ROMM encoding curve. It is quantised, not perfectly
reversible. Phase 2 must convert ROMM values to linear floating-point values
before interpolation or blending.

Use the CC0 `ProPhoto-v4.icc` profile from Compact ICC Profiles unless a test
finds an interoperability problem:

- Source:
  `https://github.com/saucecontrol/Compact-ICC-Profiles/blob/master/profiles/ProPhoto-v4.icc`
- Expected SHA-256:
  `090daf740c136b4a63bf979d64f034b4a65aa5abbb04a0917729222afe2bb5c2`
- Its inverse transfer curve uses an encoded-domain breakpoint of `0.03125`,
  which matches the ROMM curve. Do not copy the earlier plan's
  `0.001953125` ICC breakpoint; that is the linear-domain breakpoint.

Commit the profile, record its CC0 licence in `THIRD_PARTY_NOTICES.md`, and
test the profile checksum and transfer-curve parameters.

### 3.5 Metadata

The user supplies a film date but not an original time. For practical sorting,
create synthetic ordering times:

- With complete source timestamps: assign the first output `12:00:00` on the
  film date, then add each frame's elapsed scan time.
- If sorting fell back to filenames: assign `12:00:00`, then add one second
  per frame.
- After calculating each time, require it to be at least one second after the
  previous output. This makes tied source timestamps strictly ordered.
- If any calculated time would leave the entered film date, stop with
  `CAPTURE_SPAN_TOO_LONG` and ask the user to split the run.

Write the following exact mapping. IFD0 is the TIFF's main metadata area; EXIF
is its nested photo-metadata area. "ASCII", "SHORT", and "RATIONAL" are TIFF
value types.

- IFD0 `DateTime` (306, ASCII): conversion time.
- IFD0 `Make` (271, ASCII): source `Make`; fail if missing.
- IFD0 `Model` (272, ASCII): source `Model`; fail if missing.
- IFD0 `Software` (305, ASCII): `Scanny Boy <version>`.
- IFD0 `ImageDescription` (270, ASCII): source filename and
  "unstitched scan frame".
- IFD0 `Orientation` (274, SHORT): always `1` because pixels are already
  upright. Never copy the source Orientation value here.
- IFD0 `InterColorProfile` (34675, bytes): the exact vetted ICC profile.
- EXIF `DateTimeOriginal` (36867, ASCII): synthetic film date and ordering
  time.
- EXIF `SubSecTimeOriginal` (37521, ASCII): fractional synthetic time when
  present. Omit otherwise.
- EXIF `DateTimeDigitized` (36868, ASCII): copy the source
  `DateTimeOriginal`; if it is absent, use source `DateTimeDigitized`; omit if
  neither exists.
- EXIF `SubSecTimeDigitized` (37522, ASCII): copy the subsecond tag belonging
  to the source date chosen above: source `SubSecTimeOriginal` when source
  `DateTimeOriginal` was used, otherwise source `SubSecTimeDigitized`; omit
  when absent.
- EXIF `OffsetTimeDigitized` (36882, ASCII): copy the offset tag belonging to
  the source date chosen above: source `OffsetTimeOriginal` when source
  `DateTimeOriginal` was used, otherwise source `OffsetTimeDigitized`; omit
  when absent. Do not invent an offset for the synthetic film time.
- EXIF `LensModel` (42036, ASCII): source `LensModel`; fail if missing.
- EXIF `ExposureTime` (33434, RATIONAL): source exposure time; fail if
  missing.
- EXIF `FNumber` (33437, RATIONAL): source aperture; fail if missing.
- EXIF `PhotographicSensitivity` (34855, SHORT): source ISO; fail if missing.
- EXIF `FocalLength` (37386, RATIONAL): source focal length; fail if missing.
- EXIF `ColorSpace` (40961, SHORT): `65535` (uncalibrated), because the
  embedded ICC profile identifies ROMM.

Do not copy Nikon MakerNotes, serial numbers, thumbnails, or arbitrary unknown
tags.

Use `tifffile` to write `<name>.base.tif` with pixels, compression, ordinary
tags, and ICC data. Use `tifftools` to rewrite it as `<name>.final.tif` with
the nested EXIF directory. Perform both passes inside the group's staging
directory, verify the final file, and only then remove the base file.

Describe these files as 16-bit TIFFs whose metadata standard readers can read,
not as strictly EXIF-conforming primary images.

### 3.6 Output folder, overwriting, and grouping

- One output folder contains one run/roll.
- The output folder must differ from the input folder.
- An empty folder is valid.
- A nonempty folder without a valid Scanny Boy manifest is rejected.
- A valid manifest contains only relative output names without `..`, absolute
  components, or symlink escapes. Every resolved output must remain inside the
  chosen output folder.
- Permitted folder contents are the manifest, outputs listed by it, `.DS_Store`,
  and staging directories whose run identifier matches that manifest.
  Anything else makes the folder unrelated and is rejected.
- A rerun in the same folder must match the previous source filenames and
  hashes, order, grouping, film date, processing settings, and ICC hash. A
  different run requires a new empty folder.
- For a matching rerun, show the exact files that will be replaced and require
  confirmation.
- The command-line program rejects conflicts by default. The app passes an
  explicit `--overwrite` option only after confirmation.
- Each negative is processed as a group. All new files for a group are staged
  first.
- If any frame in a group fails, delete that group's staged files and continue
  with the next group.
- For ordinary failures and cooperative cancellation, never publish only part
  of a group.
- Completed groups remain after cancellation. The currently processing group
  is not published.
- A sudden process or machine failure cannot replace several files as one
  all-or-nothing operation. The manifest records progress so a rerun can
  safely replace an incomplete group.
- Write and safely store the initial `running` manifest before publishing the
  first TIFF. After a crash, treat every non-final group as incomplete and
  replace all of that group's expected outputs on rerun, including files that
  may already have been renamed into place.

### 3.7 Manifest

Write `scanny-boy-manifest.json` to a temporary file, then rename it into place
so readers never see a half-written manifest. Include:

- manifest-format version and Scanny Boy version;
- run identifier and status: `running`, `partial`, `cancelled`, or `complete`;
- input folder and film date;
- shots per negative;
- all pixel-processing parameters;
- ICC profile name and SHA-256;
- each source filename, absolute path, byte size, modification time, and
  SHA-256;
- official source order;
- negative group identifiers and membership;
- expected output names;
- each completed output's byte size and SHA-256;
- completed, failed, and pending groups;
- curated source metadata;
- start and finish times.

Create temporary manifest files in the output directory. Flush each temporary
file and use `fsync` to ask the operating system to store it, replace the old
manifest, then sync the directory where the platform permits it. Update after
every group reaches a final state.

Immediately before and after decoding each source, confirm its byte size and
modification time still match the values recorded during hashing. Stop that
group if the source changed.

Phase 2 must reject a manifest that is not `complete`, any missing output, or
any output whose size or SHA-256 differs.

### 3.8 Concurrency and cancellation

- Implement and verify a serial path first.
- Use `ThreadPoolExecutor` for parallel RAW work. Each worker opens one RAW,
  writes its staged TIFF, adds metadata, and returns only status and paths.
  Do not return full image arrays to the parent.
- Keep inner TIFF compression at one worker.
- Default workers:
  `min(shots_per_negative, os.process_cpu_count() or 1, 4)`.
- `--jobs 1` uses the serial path. Accept explicit values from 1–12.
- Budget 2 GiB of memory per requested worker initially. Reject a job count
  whose budget exceeds half of physical RAM. Chunk 6 measures peak resident
  memory for jobs 1 and 4; raise the per-worker budget if measured peak plus
  25% is larger.
- Performance is measured on sample files. Near-linear speedup is not a
  requirement.
- The app requests cancellation with SIGTERM.
- The Python signal handler sets a cancellation flag. Cleanup occurs through
  normal control flow; the handler does not perform complex file operations.
- On cancellation, stop submitting work and cancel queued tasks. A task that
  has started cannot be cancelled safely; let it finish its current RAW call
  and check the cancellation flag between decode, TIFF writing, and metadata
  stages.
- Wait for every running worker to stop before deleting the current staging
  directory. Then update the manifest, emit a final event when possible, and
  exit 130. Never delete a directory while a worker may still write to it.
- Swift treats a user-requested cancellation as cancelled even if the frozen
  helper ends because of signal 15 rather than returning 130.
- After a five-second grace period, Swift may force termination and reports
  the run as cancelled locally. A forced stop cannot clean files, update the
  manifest, or emit a final event.
- The next probe or conversion detects a manifest left as `running` and
  staging directories owned by that run. It removes those staging directories
  before rerunning, but does not remove published TIFFs.

### 3.9 Disk checks

Use these conservative values:

```text
P = height × width × 3 channels × 2 bytes
B = ceil(P × 1.05)                 # one TIFF plus metadata overhead
M = number of expected outputs that do not already exist
G = largest group size
D = max(1 MiB, estimated manifest size)
required free bytes = ceil((M × B + 2 × G × B + D) × 1.20)
```

During validation, derive pixel count from rawpy's processed active
`sizes.width × sizes.height`, not raw sensor dimensions or an assumed Z f
crop. Orientation does not change pixel count. After decode, verify and record
the actual final array shape. `M × B` covers final files not already present.
`2 × G × B` covers simultaneous base and rewritten TIFFs for one staged
group. For a pure overwrite, `M` is zero because old files already occupy
disk, but the staging term still applies. Check free space on the output
volume.

Report the estimate and available space in an error. Do not claim that
streaming limits total final output size; it limits temporary working space.

## 4. Command-line contract

`shared/contract/CONTRACT.md` and `schema.json` define the interface. The
summary here must stay consistent with them.

### 4.1 Commands

```text
scanny-boy probe \
  --input DIR --files FILE [FILE ...] --per-negative 3

scanny-boy convert \
  --input DIR --files FILE [FILE ...] \
  --out DIR --film-date YYYY-MM-DD --per-negative 3 \
  [--jobs N] [--overwrite]
```

`probe` is read-only. It returns the full folder catalogue, official selected
order, groups, warnings, metadata consistency results, output conflicts, and
whether conversion may start.

`convert` repeats all important validation. It does not trust an earlier probe
result.

### 4.2 Output transport

- stdout contains one UTF-8 JSON object per line and flushes after every line.
- stderr contains human-readable logs and is never parsed.
- stdout and stderr must both be drained while the process is running.
- Every event includes `protocol_version`, `event`, and `run_id` when a run
  exists.

Required event types:

- `started`
- `probe_result`
- `progress`
- `item_done`
- `group_done`
- `group_failed`
- `warning`
- `error`
- `finished`

`progress` includes a stable source index, stage, completed work count, and
total. Parallel completion order need not match source order. The UI derives
overall progress from counts, never from the largest source index seen.

`progress` may report decoded or staged work. `item_done` means the TIFF has
been published in the output folder after its whole group completed
successfully. If a group fails or is cancelled, it emits no `item_done` events
for that group's staged files. Emit `group_done` after that group's
`item_done` events.

Stable error and warning codes include:

- `NO_FILES`
- `NON_CONTIGUOUS_SELECTION`
- `NOT_DIVISIBLE`
- `MISSING_CAPTURE_TIME`
- `FILENAME_SORT_USED`
- `UNSUPPORTED_RAW`
- `CAPTURE_METADATA_MISSING`
- `CAPTURE_SETTINGS_DIFFER`
- `CAPTURE_SPAN_TOO_LONG`
- `UNREADABLE_RAW`
- `OUTPUT_NOT_WRITABLE`
- `OUTPUT_NOT_EMPTY`
- `OUTPUT_CONFLICT`
- `INSUFFICIENT_DISK`
- `BAD_MANIFEST`
- `CANCELLED`

Exit status:

- `0`: complete success;
- `1`: validation, conversion, or partial-run failure;
- `2`: invalid command usage;
- `130`: cooperative user cancellation.

The event stream, not message text, is the app's machine-readable interface.

## 5. Dependency and build rules

### 5.1 Python

- Support Python `>=3.13,<3.14` for v0.1.
- Runtime dependencies:
  - `rawpy>=0.27,<0.28`
  - `numpy>=2.5,<3`
  - `tifffile>=2026.8.23,<2027`
  - `imagecodecs>=2026.8.16,<2027`
  - `exifread>=3.5,<4`
  - `tifftools>=1.7,<2`
- Development dependencies belong in `[dependency-groups].dev`: pytest,
  pytest-cov, ruff, and PyInstaller.
- Commit `cli/uv.lock` and `.python-version`.
- CI uses `uv sync --locked`.
- Do not add OpenCV in Phase 1.

### 5.2 Packaged command-line program

Use PyInstaller one-directory mode, not one-file mode. Frequent `probe`
launches must not extract a large package on every call.

- Build the helper bundle at `cli/dist/ScannyBoyCLI.app`.
- Run quick packaged checks with `--version`, `probe`, serial conversion,
  threaded conversion, and cancellation.
- Inspect PyInstaller warnings and `otool -L` output.
- PyInstaller already has a NumPy hook. Add a narrow rawpy dynamic-library
  collection hook only if the packaged checks prove LibRaw is missing.
- Wrap the one-directory result as a proper nested
  `ScannyBoyCLI.app` helper bundle. Put its executable and libraries in the
  standard locations declared by that bundle; do not copy an arbitrary
  directory of executable files into `Contents/Helpers`.
- Set a unique helper bundle identifier and make it a background-only helper.
- Copy `ScannyBoyCLI.app` into
  `ScannyBoy.app/Contents/Helpers/ScannyBoyCLI.app` with Xcode's
  **Code Sign On Copy** enabled. Sign the helper before the outer app.
- Launch the executable declared at
  `ScannyBoyCLI.app/Contents/MacOS/scanny-boy`.
- In Debug builds, allow an absolute `SCANNY_BOY_CLI` environment override.
  Never find the development CLI relative to the app's current directory.
- Release builds never fall back to the repository.

For this local-only release, ad-hoc signing or Xcode's "Sign to Run Locally" is
enough. External distribution requires a separate signing and notarisation
plan.

### 5.3 Swift process runner

- Represent each invocation with an owned session object, not global static
  mutable state.
- Isolate process, buffer, and stream state for Swift 6 concurrency.
- Keep the UI model `@MainActor @Observable`.
- Stream stdout by line and drain stderr concurrently.
- Handle JSON split across arbitrary reads.
- Finish the event stream exactly once after process termination, stdout EOF,
  and stderr EOF. Do not block the main actor while waiting.
- Launch failure or Swift task cancellation closes both reader tasks and
  finishes their continuations exactly once.
- Report launch, read, and decode errors separately from normal completion.
- Check both `terminationReason` and `terminationStatus`.

## 6. Implementation chunks

Every chunk:

1. Starts from updated `main`.
2. Changes only the stated scope.
3. Adds the listed tests.
4. Runs all existing tests.
5. Includes actual command output in the pull-request body.
6. Does not change a decision in section 3 without user approval.

### Chunk 0 — Repository, licence, Python environment, and CI

**Branch:** `chunk-00-repository`

Do:

- Add `origin` for `git@github.com:lonnie-smith/scanny-boy.git`.
- Add the root `LICENSE` using the exact all-rights-reserved text in section
  3.1. Do not create the GitHub repository with `--license` or any other
  open-source template.
- Update `.gitignore` for sample NEFs, generated Xcode projects, environments,
  build output, and staging folders.
- Remove the placeholder `scan` command and its Swift/Python contract types.
- Configure Python 3.13 dependencies, dev dependency group, ruff, pytest,
  `.python-version`, and `uv.lock`.
- Update `bootstrap.sh` to run uv's project sync from `cli/`.
- Add a Python CI job on `ubuntu-latest`: checkout, install uv, select Python
  3.13, `uv sync --locked`, ruff, and pytest.
- Do not add a failing placeholder Swift job.
- Protect `main` after the Python check has run successfully: require a PR and
  the current `python` check; block force-push and deletion; require the branch
  to be current.

Verify:

```bash
./scripts/bootstrap.sh
cd cli && uv run ruff check . && uv run pytest
gh run watch RUN_ID --exit-status
```

### User gate A — Supply sample RAW files

Before Chunk 2, place at least six real Nikon Z f files in
`tests/fixtures/nef/`: two complete negatives at three frames each.

They must use the real copy stand and:

- 14-bit lossless-compressed NEF;
- full intended image area;
- fixed exposure and white balance;
- fixed lens, focal length, and orientation.

Sample RAW files stay outside Git. Record their filenames, dimensions,
compression mode, and SHA-256 values in a local inventory that is also ignored.

### Chunk 1 — Protocol and command skeleton

**Branch:** `chunk-01-protocol`

Do:

- Replace the shared contract and JSON format with section 4.
- Implement typed Python events and a flushing event writer.
- Add `probe` and `convert` argument parsing and stable usage errors.
- Add protocol-version handling.

Tests:

- Every event serialises to one line and validates against the JSON format.
- Every write flushes.
- Invalid command, date, range, and job count return status 2 and structured
  errors where the contract promises them.
- stderr never contains machine-readable events.

### Chunk 2 — Catalogue, sorting, selection, and grouping

**Branch:** `chunk-02-catalogue`

Do:

- Read the minimum NEF metadata needed for catalogue and validation.
- Implement timestamp sorting, whole-catalogue filename fallback, natural
  filename order, uninterrupted-range validation, divisibility, and grouping.
- Make `probe` return the official list used by Swift.
- Implement setting-consistency validation.

Tests:

- Natural order handles `DSC_9` before `DSC_10`.
- Valid timestamps survive `9999` to `0001` rollover.
- One missing timestamp anywhere in the catalogue switches the complete
  catalogue to filename order and emits a warning, including when that file is
  outside the selection.
- Separated selections are rejected.
- Grouping works for 1–12, with default 3.
- Non-divisible selections explain the nearest valid counts.
- Exposure, white balance, lens, focal length, and orientation mismatches name
  the differing files.
- Missing, non-finite, or non-positive rawpy camera white-balance multipliers
  are rejected.

### Chunk 3 — RAW decode and base TIFF

**Branch:** `chunk-03-raw-tiff`

Do:

- Add RAW decoding with the exact parameters in section 3.4.
- Add the CC0 ICC profile, its CC0 licence entry in `THIRD_PARTY_NOTICES.md`,
  checksum test, and curve test.
- Write RGB16 TIFFs with Deflate, horizontal prediction, ordinary tags, and
  embedded ICC data, with one compression worker per outer RAW worker.
- Derive and record output dimensions from rawpy.
- Fail rather than writing an untagged ROMM TIFF.

Tests:

- Synthetic array round-trip preserves `uint16`, shape, channels, pixels,
  compression, prediction, orientation, and ICC bytes.
- Local sample-file decode succeeds and is repeatable at the pixel-array level.
- Output Orientation is `1`, and dimensions match the final postprocess array.
- `adjust_maximum_thr=0` and `no_auto_bright=True` are verified against a
  deliberately changed control decode.
- The same sample file decoded twice has the same pixel hash.

Manual gate:

- Produce representative TIFFs and a metadata report.
- The user opens them in Preview and at least one colour-managed editor.
- The PR does not merge until the user approves orientation, appearance, and
  reported ROMM profile.

### Chunk 4 — EXIF metadata and synthetic film dates

**Branch:** `chunk-04-metadata`

Do:

- Implement the date rules and curated metadata list in section 3.5.
- Add nested EXIF metadata with `tifftools`.
- Preserve digitisation subseconds and time-zone offset when present.
- Validate output with both `tifftools` and an independent metadata reader in
  development tests.

Tests:

- Noon-plus-elapsed preserves order across a midnight scanning session without
  moving frames to the next film date.
- Filename fallback assigns noon plus one second per frame.
- Nested EXIF fields round-trip.
- The `tifftools` rewrite leaves decoded pixel hash, ICC bytes, compression,
  predictor, dimensions, bit depth, channel count, and Orientation unchanged.
- Base and final staging paths are separate, and the base file is removed only
  after final-file verification.
- Repeated runs compare equal after excluding documented volatile fields such
  as baseline conversion time.
- Lens/exposure fields read from the sample file are present in the TIFF.
- MakerNotes and serial-number tags are absent.

### Chunk 5 — Manifest and serial group pipeline

**Branch:** `chunk-05-serial-pipeline`

Do:

- Add source hashing, disk checks, manifest progress records, group staging, and the
  serial conversion path.
- Add empty-folder, valid-manifest rerun, and invalid nonempty-folder rules.
- Make `--overwrite` explicit and safe.
- Commit a group only after every staged frame is complete.

Tests:

- Manifest format and hashes are correct.
- A `running` manifest is safely stored before the first output appears.
- Completed output sizes and SHA-256 values match the files.
- A failed frame removes all new staged files for that group and later groups
  continue.
- Missing output space fails before decode.
- Existing files fail without `--overwrite`.
- A valid manifest plus `--overwrite` replaces expected files.
- A rerun with changed sources, hashes, order, grouping, film date, processing
  settings, or ICC is rejected and requires a new empty folder.
- An unrelated nonempty folder is rejected.
- Manifest output paths cannot escape the output folder through absolute paths,
  `..`, or symlinks.
- A source changed after hashing is rejected before its group is published.
- A forced test failure after the first TIFF rename leaves the group
  incomplete; recovery replaces every expected output in that group.
- Disk checks fail just below and pass just above the required-free-bytes
  formula for both a new run and a pure overwrite.
- No staging directory survives normal success or handled failure.

### Chunk 6 — Threaded conversion and cancellation

**Branch:** `chunk-06-concurrency`

Do:

- Add the thread-worker path and conservative default worker count.
- Keep `--jobs 1` independent of the executor.
- Add cooperative SIGTERM cancellation and final status handling.

Tests:

- Jobs 1 and 4 produce equal pixel hashes and metadata after documented
  changing fields are ignored.
- Every source index reports one final result despite out-of-order completion.
- A controlled blocking test cancels only after work has definitely started;
  do not use a race-prone fixed sleep.
- Cancellation removes the current group, retains completed groups, updates
  the manifest, and returns the documented terminal state.
- Queued work is cancelled, running workers stop, and only then is staging
  deleted.
- Forced termination may leave a `running` manifest and staging directory.
  The next run removes that abandoned staging work and safely reruns the
  incomplete group.
- Thread workers never return image arrays to the parent.
- TIFF compression uses one inner worker.
- Record peak resident memory (actual RAM in use) for jobs 1 and 4 and verify
  the runtime memory guard rejects an unsafe explicit job count.
- Record benchmark results, but do not require a fixed speedup.

### Chunk 7 — Package the command-line program

**Branch:** `chunk-07-package-cli`

Do:

- Build a PyInstaller one-directory distribution wrapped in
  `ScannyBoyCLI.app`.
- Bundle all runtime libraries and the ICC profile.
- Update `build-cli.sh` and add quick packaged checks.
- Inspect macOS library dependencies rather than assuming hooks are missing.
- Give the helper a unique bundle identifier and background-only setting.
- Verify the helper's signature before it can be copied into the outer app.

Tests:

- Packaged `--version`, `probe`, jobs 1, jobs 4, and cancellation work.
- Development and packaged runs have equal pixel hashes and metadata after
  documented changing fields are ignored.
- No dependency is loaded from `cli/.venv`.
- `codesign --verify --strict` succeeds for the helper bundle.

### Chunk 8 — XcodeGen project and streaming runner

**Branch:** `chunk-08-xcode-runner`

Do:

- Create `mac/project.yml` for macOS 14 deployment and Swift 6.
- Use Swift Testing for unit tests.
- Make `build-cli.sh` an explicit prerequisite because `cli/dist/` is ignored
  by Git and is absent from a clean checkout.
- Copy `ScannyBoyCLI.app` to `Contents/Helpers/` with **Code Sign On Copy**.
- Implement debug override and bundled release resolution.
- Implement the owned streaming session described in section 5.3.
- Add Swift CI on `macos-15`. In order, it must check out the repository,
  install uv, select Python 3.13, sync `cli/`, install or verify XcodeGen 2.46,
  set `DEVELOPER_DIR=/Applications/Xcode_16.2.app/Contents/Developer`, print
  `xcodebuild -version`, run `build-cli.sh`, generate the project, and test it.
- Add the `swift` job to required branch checks after it passes.

Tests:

- Decode every event and preserve unknown events safely.
- Reassemble JSON split across reads.
- Drain large stdout and stderr concurrently without deadlock.
- Do not finish until the process and both output streams have ended.
- Drive the runner with a temporary test executable.
- Test normal success, structured failure, cooperative cancellation, signal
  termination, and forced-cancel classification.
- Test recovery from a forced stop that leaves a `running` manifest and
  abandoned staging directory.
- A clean XcodeGen generation builds and tests successfully.
- `codesign --verify --strict` succeeds for both the nested helper and the
  built Scanny Boy app.

Verify without piping away the exit status:

```bash
./scripts/build-cli.sh
cd mac
xcodegen generate
xcodebuild test \
  -scheme ScannyBoy \
  -destination 'platform=macOS' \
  -resultBundlePath TestResults.xcresult
```

### Chunk 9 — Folder catalogue and configuration UI

**Branch:** `chunk-09-configuration-ui`

Do:

- Add input-folder selection and last-folder memory.
- Ask the CLI for the official folder catalogue.
- Add one-range selection and reject gaps.
- Add shots per negative, 1–12, default 3.
- Add a required film-date field that starts blank.
- Add output-folder selection and validation.
- Add grouping preview, setting mismatch errors, missing-time warnings, disk
  estimate, and overwrite-conflict preview.

Tests:

- Model state follows probe results; Swift does not reimplement EXIF sorting.
- Run remains disabled for a gap, non-divisible count, blank date, bad output
  folder, setting mismatch, or unresolved overwrite confirmation.
- A valid six-file, three-per-negative selection shows two groups.

### Chunk 10 — Run, progress, cancellation, and completion UI

**Branch:** `chunk-10-run-ui`

Do:

- Add Run and overwrite confirmation.
- Show work stage, current filename, completed count, elapsed time, and
  estimated remaining time.
- Add cooperative Cancel with forced termination after the grace period.
- Show completed and failed groups.
- Add Reveal in Finder.
- Read a final manifest for normal completion and cooperative cancellation.
  After a forced stop, accept a stale `running` manifest, report that cleanup
  was incomplete, and explain that the next run will recover it.

Tests:

- Synthetic out-of-order events produce correct progress.
- Partial group work is not presented as published output.
- Repeated Cancel requests have no extra effect.
- Terminal stream errors and CLI errors are distinct.

Manual verification:

- Six sample files at three per negative produce six TIFFs and one complete
  manifest.
- A five-file selection is blocked.
- A selection with a gap is blocked.
- Unrelated content in the output folder is blocked.
- Rerunning a valid output folder requires confirmation.
- Progress updates during conversion.
- Cancel retains earlier groups and removes the current group's staged work.

### Chunk 11 — Documentation and v0.1 sign-off

**Branch:** `chunk-11-documentation`

Do:

- Rewrite root and component READMEs from a clean-clone perspective.
- State clearly in the root README that the project is all rights reserved and
  point readers to `LICENSE`.
- Add `CONTRIBUTING.md` for the one-person workflow. It must not imply that
  outside contributions are accepted under an open-source licence.
- Add `docs/DECISIONS.md` based on section 3.
- Document the sample-file requirement and the required 14-bit
  lossless-compressed NEF camera setting.
- Document local-only Apple-silicon scope and deferred distribution work.
- Verify a fresh clone using only the written instructions.

After the documentation PR merges and both required checks pass, create and
push an annotated `v0.1.0` tag. Do not create the tag from the PR branch.

## 7. Test rules

- Python tests live next to Python code as `*_test.py`.
- Swift tests use Swift Testing.
- Tests needing local NEFs skip clearly when sample files are absent.
- A skipped sample-file test must say what was not tested.
- Do not mock rawpy's decoding. Use a real NEF or test a lower-level synthetic
  TIFF operation.
- Compare pixel hashes and metadata after documented changing fields are
  ignored, not entire TIFF bytes that
  contain conversion timestamps.
- Use isolated temporary directories. Do not share filenames between
  concurrently running Swift tests.
- Never hide a failing build by piping it through `tail`.

## 8. Human approval points

Implementation pauses for the user at:

1. Sample-file supply before Chunk 2.
2. Visual RAW/TIFF approval in Chunk 3.
3. Overwrite and cancellation behaviour in the finished app.
4. Final clean-clone and end-to-end sign-off before `v0.1.0`.

Agents may prepare evidence for these checks but may not approve them on the
user's behalf.

## 9. Risks

- **Unsupported RAW compression:** reject Z f HE/HE* and document the required
  lossless-compressed camera setting.
- **Frame-to-frame scaling changes:** lock camera settings, compare metadata,
  disable automatic brightness and maximum adjustment, and stop on mismatch.
- **Wrong colour interpretation:** bundle and verify one matching ICC profile;
  never emit untagged ROMM.
- **Metadata incompatibility:** write a real nested EXIF directory and test
  with more than one reader and the target photo applications.
- **Unexpected memory or disk use:** stage one negative at a time, cap default
  threads at four, have workers write files directly, and budget uncompressed
  size with margin.
- **Cancellation damage:** stage by group, record progress in the manifest, clean through
  normal control flow, and make reruns replace incomplete groups.
- **Packaged helper failure:** test the actual PyInstaller directory with real
  sample files and place it in the app's helper area.
- **CI image changes:** use `macos-15` and select the supported Xcode version
  explicitly.

## 10. Phase 2 compatibility only

Phase 1 guarantees:

- a complete manifest with ordered groups and source hashes;
- one upright RGB16 ROMM TIFF per source;
- fixed processing settings recorded in the manifest;
- no partially published group after handled failures or cancellation.

Do not choose an OpenCV registration model in Phase 1. The later stitching
plan must test translation, rigid, partial-affine, and full-affine models on
real sample files. It must estimate transforms from derived detection images,
linearise ROMM masters before interpolation/blending, and define crop and seam
behaviour separately.

## 11. Primary references

- [rawpy parameters](https://letmaik.github.io/rawpy/api/rawpy.Params.html)
- [rawpy thread support](https://github.com/letmaik/rawpy/releases/tag/v0.21.0)
- [rawpy 0.27.0 / LibRaw 0.22.1](https://github.com/letmaik/rawpy/releases/tag/v0.27.0)
- [LibRaw processing parameters](https://www.libraw.org/docs/API-datastruct.html)
- [LibRaw supported cameras](https://www.libraw.org/supported-cameras/)
- [Nikon Z f dimensions and formats](https://onlinemanual.nikonimglib.com/zf/en/specifications_350.html)
- [ROMM RGB definition](https://registry.color.org/rgb-registry/rommrgb)
- [ICC parametric curve definition](https://archive.color.org/files/whitepapers/ICC_White_Paper35-Use_of_the_parametricCurveType.pdf)
- [Compact ICC Profiles and CC0 licence](https://github.com/saucecontrol/Compact-ICC-Profiles)
- [tifffile](https://pypi.org/project/tifffile/)
- [tifffile EXIF-directory limitation](https://github.com/cgohlke/tifffile/issues/1)
- [tifftools](https://pypi.org/project/tifftools/)
- [ExifRead](https://pypi.org/project/ExifRead/)
- [EXIF tag meanings](https://exiftool.org/TagNames/EXIF.html)
- [Python CPU-count APIs](https://docs.python.org/3/library/os.html)
- [uv project dependencies](https://docs.astral.sh/uv/concepts/projects/dependencies/)
- [uv syncing](https://docs.astral.sh/uv/concepts/projects/sync/)
- [PyInstaller macOS behaviour](https://pyinstaller.org/en/stable/feature-notes.html)
- [Apple nested-code placement](https://developer.apple.com/library/archive/technotes/tn2206/_index.html)
- [Apple asynchronous file-handle bytes](https://developer.apple.com/documentation/foundation/filehandle/bytes)
- [GitHub-hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
- [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [macOS 14 runner retirement](https://github.com/actions/runner-images/issues/13518)
- [GitHub branch protection](https://docs.github.com/en/rest/branches/branch-protection)
- [XcodeGen](https://github.com/yonaskolb/XcodeGen)

## 12. Agent handoff

Use this prompt for one chunk at a time:

> Implement Chunk N from `docs/IMPLEMENTATION_PLAN.md`.
> Read sections 3–5 first. Do not change those decisions without user
> approval. Work only in the chunk's scope, add its tests, run all existing
> tests plus the chunk verification, and include actual command output in the
> pull-request body. Stop and report if a required sample file, human
> approval, or earlier chunk is missing.
