# Scanny Boy — Phase 1 implementation plan

**Last reviewed:** 2026-08-28 (drift review after Chunk 7)

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

### 1.1 Vocabulary used throughout this plan

Define these once so agents use them consistently.

- **Catalogue** — every `.nef` file in the chosen input folder.
- **Canonical order** — the single sorted order of the catalogue that the
  command-line program computes using the rules in section 3.3. Only the
  command-line program decides this order. Swift always uses the order it is
  given and never sorts files itself.
- **Selection** — the uninterrupted run of files the user picked out of the
  catalogue, in canonical order.
- **Group** — one negative: `shots_per_negative` consecutive files from the
  selection.
- **Pipeline step** — one of decode, write TIFF, add metadata. Reported in
  `progress` events.
- **Staging directory** — a temporary directory inside the output folder that
  holds a group's files until the whole group succeeds. Never called a
  "stage", to avoid confusion with pipeline steps.
- **Published** — a finished TIFF that has been moved out of its staging
  directory into the output folder.

## 2. Facts verified before implementation

Every item below was checked against current documentation, the local machine,
or a working prototype on 2026-08-27. Items marked **(prototyped)** were proven
by building and running the code, not by reading documentation.

### 2.1 Local machine and repository

- macOS 14.6.1 on Apple silicon; Xcode 16.2 (build `16C5032a`); Swift 6.0.3;
  Python 3.13.3; uv 0.11.7; GitHub CLI 2.98.0; XcodeGen 2.46.0.
- The local checkout **does** have an `origin` remote pointing at
  `git@github.com:lonnie-smith/scanny-boy.git`.
- Local `main` and `origin/main` are in sync (re-checked 2026-08-28). Nothing
  needs pushing before branch protection is enabled.
- `lonnie-smith/scanny-boy` is public, GitHub reports its licence as "Other"
  (`NOASSERTION`) from the all-rights-reserved `LICENSE`, and `main` is not
  yet protected.
- The local sample-NEF directory `tests/fixtures/nef/` holds the six required
  sample files. See appendix A; user gate A is satisfied.
- `.gitignore` already excludes `tests/fixtures/nef/` and
  `tests/fixtures/INVENTORY.md`. Never remove those rules: the sample files are
  about 190 MB and the repository is public.

### 2.2 RAW decoding

- rawpy 0.27.0 bundles LibRaw 0.22.1 and ships macOS arm64 wheels for Python
  3.9 through 3.14. **(prototyped:** `rawpy.libraw_version` returns
  `(0, 22, 1)`.**)**
- LibRaw 0.22 supports the Nikon Z f, with the published note that
  "HE/HE* formats are not supported yet". This cannot be worked around:
  High Efficiency compression uses patented TicoRAW, licensed per-vendor.
  This project therefore requires lossless-compressed NEFs.
- The Z f records RAW at 14 bits only, so bit depth is not a user choice. The
  only camera setting that matters is compression: **Lossless compressed**,
  never High Efficiency or High Efficiency\*.
- Since rawpy 0.21, separate Python threads can decode RAW files at the same
  time safely — the release added a thread-safe LibRaw build that releases
  Python's global interpreter lock. Thread workers are therefore preferred
  over process workers.
- Every parameter name and enumeration value in section 3.4 was checked
  against the installed package. **(prototyped)**
- All six sample NEFs open and postprocess with the exact `RAW_PARAMS` of
  section 3.4, producing `(4040, 6064, 3)` `uint16` in about 1.1 s each. They
  are therefore lossless-compressed, not HE/HE*. **(prototyped 2026-08-28)**
- Every EXIF tag marked **required** in section 3.5 is present in all six
  sample files, and every **optional** tag in that mapping is present as well.
  The Chunk 2 approval gate for a missing required tag is not expected to
  trigger. **(prototyped 2026-08-28; see appendix A)**

### 2.3 TIFF writing

- `tifffile` 2026.8.23 writes RGB16, Deflate compression, horizontal
  prediction, ordinary TIFF tags, and an embedded colour profile.
  **(prototyped)**
- `tifffile` cannot create the nested EXIF directory needed for
  `DateTimeOriginal`. Its own source says: "Specifically, ExifIFD and GPSIFD
  tags are not supported." `tifftools` 1.7.0 can read and write nested TIFF
  directories and is pure Python.
- The two-pass write works end to end: after the `tifftools` rewrite, the
  pixel hash, embedded profile bytes, compression, prediction, orientation,
  and dimensions are all unchanged, and every nested EXIF field reads back
  correctly. **(prototyped)**
- Compression is not a performance concern. One full 24.5 MP frame with
  Deflate and horizontal prediction at one compression worker takes about
  **1.5 seconds** and compresses to about **74%** of uncompressed size.
  **(prototyped)**

### 2.4 Packaging

- PyInstaller 6.22.2 `BUNDLE()` with `console=True` produces a valid
  `ScannyBoyCLI.app`. PyInstaller ad-hoc signs it, `codesign --verify
  --strict` exits 0, and stdout and stderr both work through pipes.
  **(prototyped)**
- LibRaw needs **no** PyInstaller hook. `libraw_r.25.dylib` is collected
  automatically into `Contents/Frameworks/`. **(prototyped)**
- Two packaging failures are certain without explicit fixes, and both were
  reproduced. See section 5.2 for the fixes. **(prototyped)**

### 2.5 Platform

- `os.process_cpu_count()` reports the logical CPUs available to the process.
  Neither it nor `os.cpu_count()` reports physical cores. On this Apple-silicon
  Mac both return 10, which counts efficiency cores as well as performance
  cores, so the worker default must stay conservative.
- Standard GitHub-hosted runners are free and unmetered for public
  repositories. Larger runners and storage are not covered by that statement.
- Branch protection with required status checks is available on GitHub Free
  for public repositories.
- GitHub's `macos-14` runner is deprecated and is fully unsupported after
  2026-11-02. CI must use `macos-15`, which is supported until August 2027.
- The `macos-15` image does still carry Xcode 16.2, build `16C5032a` — the
  same build as the local machine — at `/Applications/Xcode_16.2.app`.
  Re-check this before each release; GitHub trims old Xcode versions over time.
- The standard macOS runner has 3 CPUs and about 7 GB of RAM. Any memory
  guard must not reject a default run on that machine.
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
  `THIRD_PARTY_NOTICES.md`. Two entries are required and must not be omitted:
  - **LibRaw**, bundled inside the packaged program as a shared library. It is
    offered under either LGPL 2.1 or CDDL 1.0. Both modes require attribution.
    Shared-library use, which is what rawpy ships, satisfies both.
  - **The ICC colour profile**, which is CC0. Added in Chunk 3.

  Python dependencies are used under their upstream licences and are not
  relicensed here.
- `project.yml` is the source for the Xcode project. Generated
  `.xcodeproj` files are not committed.
- `main` requires a pull request and passing status checks, and requires a
  branch to be up to date with `main` before merging. No approval count is
  required because this is a one-person project. Force-pushes and branch
  deletion are blocked.
- Each implementation chunk is one branch and one pull request. Merge chunks
  in order.

### 3.2 Input rules

- Accept `.nef` case-insensitively from one folder, without recursion.
- Resolve paths and reject duplicates or files outside the chosen input
  folder.
- The selection must be one uninterrupted range of the catalogue in canonical
  order.
- Shots per negative accepts 1–12 and defaults to 3.
- The selected count must be divisible by shots per negative.
- The camera workflow requires:
  - Lossless-compressed NEF (never High Efficiency or High Efficiency\*);
  - fixed manual exposure;
  - fixed manual white balance;
  - one lens and focal length;
  - one camera orientation.
- Open each selected RAW with rawpy before conversion. Map LibRaw's unsupported
  file error to `UNSUPPORTED_RAW` and explain that Z f HE/HE* files must be
  recaptured as lossless-compressed NEFs.
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
- Determine one canonical order for the complete catalogue. If any NEF in that
  catalogue lacks a usable capture timestamp, sort the complete catalogue by
  natural filename and show a warning, even when the missing timestamp is
  outside the selection.
- Never use a mixed timestamp/filename comparison.
- After sorting, verify that the selection is an uninterrupted range within
  the catalogue.
- Do not add a warning for uneven time gaps between frames. There is no
  measured threshold for one.

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
- Use lossless Deflate compression with horizontal prediction.
- Set `tifffile`'s compression `maxworkers=1`; outer RAW threads own
  concurrency.
- Preserve fixed exposure across the run by disabling both histogram-based
  brightening and content-dependent maximum adjustment.

Required rawpy settings. Every name and value here was checked against
rawpy 0.27.0:

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

Do not set `user_flip=0`; leave it at its default of `-1` so rawpy applies the
source orientation.

`gamma=(1.8, 16)` is the ROMM encoding curve. rawpy inverts the first value
internally, so LibRaw receives a power of `1/1.8` and a slope of `16`, which is
correct. The curve is quantised and not perfectly reversible. Phase 2 must
convert ROMM values to linear floating-point values before interpolation or
blending.

#### The ICC colour profile

Use the CC0 `ProPhoto-v4.icc` profile from Compact ICC Profiles unless a test
finds an interoperability problem:

- Source:
  `https://github.com/saucecontrol/Compact-ICC-Profiles/blob/master/profiles/ProPhoto-v4.icc`
- Size: 480 bytes.
- Expected SHA-256 (verified 2026-08-27):
  `090daf740c136b4a63bf979d64f034b4a65aa5abbb04a0917729222afe2bb5c2`
- Its inverse transfer curve uses an encoded-domain breakpoint of `0.03125`,
  which matches the ROMM curve. Do not use `0.001953125`; that is the same
  breakpoint expressed in the linear domain, and it is wrong here.

Commit the file at `cli/src/scanny_boy/resources/ProPhoto-v4.icc` so it is
packaged as ordinary package data. Load it with
`importlib.resources.files("scanny_boy.resources")`, which works identically
in a development checkout and inside the packaged program. Do not use
`sys._MEIPASS` or paths relative to `__file__`.

Verify the profile's SHA-256 at startup and fail with `ICC_PROFILE_INVALID`
if it does not match. Record the CC0 licence in `THIRD_PARTY_NOTICES.md`, and
test both the checksum and the transfer-curve parameters.

#### Writing the TIFF with tifffile

These four points are not obvious and each one was reproduced. Getting any of
them wrong produces a file that looks fine but is subtly incorrect.

1. **Pass `metadata=None`.** Otherwise `tifffile` writes a *second*
   `ImageDescription` tag containing shape JSON such as
   `{"shape": [4032, 6048, 3]}`. The later `tifftools` rewrite silently
   collapses the duplicate, so which description survives is unpredictable.
2. **`ImageDescription` (270) and `Software` (305) cannot be set through
   `extratags`.** `tifffile` refuses them with a message on stderr and
   continues. Use the `description=` and `software=` keyword arguments.
   Other IFD0 tags such as `Make` (271), `Model` (272), `Orientation` (274),
   and `DateTime` (306) do work through `extratags`.
3. **Pass the profile with the `iccprofile=` keyword argument**, not as an
   `extratags` entry.
4. **`tifffile` writes Deflate as TIFF compression code `32946`**
   (Adobe Deflate), not `8`. This is intended; assert `32946` in tests so a
   future change is caught.

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

Noon is deliberate. It leaves twelve hours of headroom, far more than any
realistic copy-stand session, and it keeps the timestamp away from a day
boundary so a viewer in another time zone still displays the correct film date.

#### Tag mapping

IFD0 is the TIFF's main metadata area; EXIF is its nested photo-metadata area.
"ASCII", "SHORT", and "RATIONAL" are TIFF value types.

Not every field is equally important. Tags are marked **required** or
**optional**:

- **required** — the frame cannot be converted without it, because it is what
  proves camera settings stayed fixed across the selection. A missing value
  stops the group with `CAPTURE_METADATA_MISSING`.
- **optional** — descriptive only. If the source lacks it, emit a warning,
  omit the tag from the output, and continue.

IFD0 tags:

| Tag | Code | Type | Value | Status |
| --- | --- | --- | --- | --- |
| `DateTime` | 306 | ASCII | conversion time | always written |
| `Make` | 271 | ASCII | source `Make` | optional |
| `Model` | 272 | ASCII | source `Model` | optional |
| `Software` | 305 | ASCII | `Scanny Boy <version>` | always written |
| `ImageDescription` | 270 | ASCII | source filename and "unstitched scan frame" | always written |
| `Orientation` | 274 | SHORT | always `1` | always written |
| `InterColorProfile` | 34675 | bytes | the exact vetted ICC profile | always written |

`Orientation` is always `1` because the pixels are already upright. Never copy
the source Orientation value here.

EXIF tags:

| Tag | Code | Type | Value | Status |
| --- | --- | --- | --- | --- |
| `DateTimeOriginal` | 36867 | ASCII | synthetic film date and ordering time | always written |
| `SubSecTimeOriginal` | 37521 | ASCII | fractional synthetic time when present | optional |
| `DateTimeDigitized` | 36868 | ASCII | see below | optional |
| `SubSecTimeDigitized` | 37522 | ASCII | see below | optional |
| `OffsetTimeDigitized` | 36882 | ASCII | see below | optional |
| `LensModel` | 42036 | ASCII | source `LensModel` | optional |
| `ExposureTime` | 33434 | RATIONAL | source exposure time | **required** |
| `FNumber` | 33437 | RATIONAL | source aperture | **required** |
| `PhotographicSensitivity` | 34855 | SHORT | source ISO | **required** |
| `FocalLength` | 37386 | RATIONAL | source focal length | **required** |
| `ColorSpace` | 40961 | SHORT | `65535` (uncalibrated) | always written |

`ColorSpace` is `65535` because the embedded ICC profile identifies ROMM.

The three copied "digitized" fields follow the source date that was used:

- `DateTimeDigitized`: copy the source `DateTimeOriginal`; if it is absent,
  use source `DateTimeDigitized`; omit if neither exists.
- `SubSecTimeDigitized`: copy the subsecond tag belonging to whichever source
  date was chosen above — source `SubSecTimeOriginal` when source
  `DateTimeOriginal` was used, otherwise source `SubSecTimeDigitized`; omit
  when absent.
- `OffsetTimeDigitized`: copy the offset tag belonging to whichever source
  date was chosen above — source `OffsetTimeOriginal` when source
  `DateTimeOriginal` was used, otherwise source `OffsetTimeDigitized`; omit
  when absent. Never invent an offset for the synthetic film time.

Do not copy Nikon MakerNotes, serial numbers, thumbnails, or arbitrary unknown
tags.

Chunk 2 must dump all of these tags from the real sample NEFs and record what
the Z f actually writes. If a **required** tag turns out to be absent from real
files, stop and ask the user rather than silently downgrading it.

#### Writing the nested EXIF directory with tifftools

Use `tifffile` to write `<name>.base.tif` with pixels, compression, ordinary
tags, and profile data. Use `tifftools` to rewrite it as `<name>.final.tif`
with the nested EXIF directory. Do both passes inside the group's staging
directory, verify the final file, and only then remove the base file.

Address every EXIF tag by its numeric code. `tifftools.Tag` does not contain
most of the names used above — they live in `tifftools.constants.EXIFTag` —
and two of them are spelled differently there (`36868` is `CreateDate`, and
`34855` is `ISOSpeedRatings`). Numeric codes avoid the whole problem.

These files are 16-bit TIFFs whose metadata standard readers can read. Do not
describe them, in code comments or documentation, as strictly EXIF-conforming
primary images.

### 3.6 Output folder, overwriting, and grouping

- One output folder contains one run/roll.
- The output folder must differ from the input folder. Compare resolved paths,
  and reject with `OUTPUT_SAME_AS_INPUT`.
- An empty folder is valid.
- A nonempty folder without a valid Scanny Boy manifest is rejected.
- A valid manifest contains only relative output names without `..`, absolute
  components, or symlink escapes. Every resolved output must remain inside the
  chosen output folder.
- When deciding whether a folder is related, **ignore every entry whose name
  begins with a dot.** This covers `.DS_Store`, `._*` AppleDouble files,
  `.Spotlight-V100`, `.fseventsd`, `.localized`, and anything Apple adds later.
  Scanny Boy never creates dot-files itself, so ignoring them is safe.
- Apart from ignored dot-files, permitted folder contents are the manifest,
  outputs listed by it, and staging directories whose run identifier matches
  that manifest. Anything else makes the folder unrelated and is rejected.
- A rerun in the same folder must match the previous source filenames and
  hashes, order, grouping, film date, processing settings, and ICC hash. A
  mismatch is `MANIFEST_MISMATCH` and requires a new empty folder. This is a
  different failure from `BAD_MANIFEST`, which means the manifest could not be
  read or did not match its schema.
- For a matching rerun, show the exact files that will be replaced and require
  confirmation.
- The command-line program rejects conflicts by default. The app passes an
  explicit `--overwrite` option only after confirmation.
- Each negative is processed as a group. All new files for a group are written
  into that group's staging directory first.
- If any frame in a group fails, delete that group's staging directory and
  continue with the next group.
- For ordinary failures and cooperative cancellation, never publish only part
  of a group.
- Completed groups remain after cancellation. The group being processed is not
  published.
- A sudden process or machine failure cannot replace several files as one
  all-or-nothing operation. The manifest records progress so a rerun can
  safely replace an incomplete group.
- Write the initial `running` manifest and `fsync` it before publishing the
  first TIFF. After a crash, treat every non-final group as incomplete and
  replace all of that group's expected outputs on rerun, including files that
  may already have been moved into place.

### 3.7 Manifest

Write `scanny-boy-manifest.json` to a temporary file in the output directory,
then rename it into place so readers never see a half-written manifest. Flush
each temporary file and call `fsync` on it before the rename, then `fsync` the
directory where the platform permits it. Update the manifest after every group
reaches a final state.

`shared/contract/manifest.schema.json` is the authoritative definition of the
format. Chunk 5 writes that schema, and Chunk 5's tests validate every written
manifest against it. The list below states what the schema must cover:

- manifest-format version and Scanny Boy version;
- run identifier and status: `running`, `partial`, `cancelled`, or `complete`;
- input folder and film date;
- shots per negative;
- all pixel-processing parameters;
- ICC profile name and SHA-256;
- each source filename, absolute path, byte size, modification time, and
  SHA-256;
- the canonical source order;
- negative group identifiers and membership;
- expected output names;
- each completed output's byte size and SHA-256;
- completed, failed, and pending groups;
- curated source metadata;
- start and finish times.

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
- Budget **640 MiB of memory per worker**. This began at 512 MiB — one output
  frame is about 140 MiB, with LibRaw working space on top — and Chunk 6
  raised it to 640 MiB from measurement, as the last item below instructed.
  - If the computed **default** worker count exceeds the budget for this
    machine, silently reduce it. Never fail a run because of the default.
  - If an **explicit** `--jobs` value exceeds the budget, reject it with
    `INSUFFICIENT_MEMORY` and report both numbers.
  - The budget is workers × 640 MiB, and it must not exceed half of physical
    RAM. On the 7 GB CI runner that permits five workers, so the default
    there — three, from `min(shots_per_negative, 3 CPUs, 4)` — is never
    reduced.
  - Chunk 6 measured peak resident memory on the six real sample NEFs using
    `scripts/measure-concurrency.py` (macOS 14.6.1, Apple silicon). Peak
    resident set size, and the per-worker budget each row demands:

    | jobs | peak MiB | peak + 25% | per worker |
    | --- | --- | --- | --- |
    | 1 | 463.8 | 579.8 | **579.8** |
    | 2 | 846.8 | 1058.5 | 529.3 |
    | 3 | 1217.4 | 1521.7 | 507.2 |
    | 4 | 1608.8 | 2011.0 | 502.8 |

    The serial row binds, because much of its peak is the fixed interpreter,
    numpy, rawpy, and imagecodecs baseline rather than per-frame cost. Its
    579.8 MiB exceeds 512 MiB, which is why the budget is now 640 MiB.
    Figures move by a few MiB between runs; re-measure before changing this
    again.
- Performance is measured on sample files. Near-linear speedup is not a
  requirement.
- The app requests cancellation with SIGTERM.
- The Python signal handler sets a cancellation flag. Cleanup happens through
  normal control flow; the handler does not perform complex file operations.
- On cancellation, stop submitting work and cancel queued tasks. A task that
  has started cannot be cancelled safely; let it finish its current RAW call
  and check the cancellation flag between the decode, TIFF-writing, and
  metadata steps.
- Wait for every running worker to stop before deleting the current staging
  directory. Then update the manifest, emit a final event when possible, and
  exit 143. Never delete a directory while a worker may still write to it.
- Swift treats a user-requested cancellation as cancelled whether the helper
  exits 143 or is reported as terminated by signal 15.
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

`B` deliberately assumes compression saves nothing. Measured Deflate output is
about 74% of `P`, so this leaves real headroom.

During validation, derive pixel count from rawpy's processed active
`sizes.width × sizes.height`, not raw sensor dimensions or an assumed Z f
crop. Orientation does not change pixel count. After decode, verify and record
the actual final array shape. `M × B` covers final files not already present.
`2 × G × B` covers the base and rewritten TIFFs held at once for one staged
group. For a pure overwrite, `M` is zero because old files already occupy
disk, but the staging term still applies. Check free space on the output
volume.

Report the estimate and the available space in the error. Streaming limits
temporary working space, not the total size of the final output.

## 4. Command-line contract

`shared/contract/CONTRACT.md` and `schema.json` define the interface. The
summary here must stay consistent with them.

### 4.1 Commands

```text
scanny-boy probe \
  --input DIR [--files FILE [FILE ...]] [--per-negative 3]

scanny-boy convert \
  --input DIR --files FILE [FILE ...] \
  --out DIR --film-date YYYY-MM-DD --per-negative 3 \
  [--jobs N] [--overwrite]
```

`probe` is read-only and works at two levels of detail:

- **`--input` alone** returns the catalogue in canonical order, plus any
  sorting warnings. Swift calls this first, because it cannot name a selection
  before it knows the order. Swift never sorts files itself.
- **`--input` with `--files`** additionally validates the selection: the
  uninterrupted-range check, grouping, metadata consistency, output conflicts,
  and whether conversion may start.

`--out` may be given to `probe` alongside `--files` to include output-folder
validation and the overwrite-conflict preview.

`convert` repeats all important validation. It does not trust an earlier probe
result.

`--files` takes filenames relative to `--input`, not absolute paths. macOS
allows roughly a megabyte of arguments, so a folder of several thousand frames
is safe; reject a selection above 5000 files with a usage error rather than
letting the operating system truncate it.

There is a third invocation, `scanny-boy --version`. It prints one plain-text
line (`scanny-boy 0.1.0`) and exits 0. It is deliberately outside the event
stream: the app never calls it, and it exists because section 5.2's packaged
checks need the cheapest possible proof that the frozen program starts and
can read its own package metadata. Every other invocation writes only JSON
event lines to stdout. Added in Chunk 7.

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

`progress` includes a stable source index, the pipeline step, a completed work
count, and a total. Parallel completion order need not match source order. The
UI derives overall progress from counts, never from the largest source index
seen.

`progress` may report decoded or staged work. `item_done` means the TIFF has
been published in the output folder after its whole group completed
successfully. If a group fails or is cancelled, it emits no `item_done` events
for that group's staged files. Emit `group_done` after that group's
`item_done` events.

Stable error and warning codes:

| Code | Meaning |
| --- | --- |
| `NO_FILES` | No `.nef` files, or none selected |
| `NON_CONTIGUOUS_SELECTION` | Selection has a gap in canonical order |
| `NOT_DIVISIBLE` | Selected count not divisible by shots per negative |
| `INVALID_PER_NEGATIVE` | Shots per negative outside 1–12 |
| `MISSING_CAPTURE_TIME` | A catalogue file has no usable capture timestamp |
| `FILENAME_SORT_USED` | Warning: whole catalogue fell back to filename order |
| `UNSUPPORTED_RAW` | LibRaw cannot read the file, typically HE/HE\* |
| `CAPTURE_METADATA_MISSING` | A required EXIF tag is absent |
| `CAPTURE_SETTINGS_DIFFER` | Exposure, white balance, lens, or orientation varies |
| `CAPTURE_SPAN_TOO_LONG` | Synthetic times would leave the film date |
| `UNREADABLE_RAW` | File exists but could not be decoded |
| `OUTPUT_SAME_AS_INPUT` | Output folder resolves to the input folder |
| `OUTPUT_NOT_WRITABLE` | Cannot write to the output folder |
| `OUTPUT_NOT_EMPTY` | Nonempty folder with no valid manifest |
| `OUTPUT_CONFLICT` | Existing outputs and no `--overwrite` |
| `INSUFFICIENT_DISK` | Free space below the section 3.9 estimate |
| `INSUFFICIENT_MEMORY` | Explicit `--jobs` exceeds the memory budget |
| `BAD_MANIFEST` | Manifest unreadable or fails its schema |
| `MANIFEST_MISMATCH` | Manifest valid but its run parameters differ |
| `ICC_PROFILE_INVALID` | Bundled profile missing or wrong SHA-256 |
| `TIFF_WRITE_FAILED` | A TIFF or metadata write failed |
| `CANCELLED` | Cooperative user cancellation |

Exit status:

- `0`: complete success;
- `1`: validation, conversion, or partial-run failure;
- `2`: invalid command usage;
- `143`: cooperative user cancellation, matching 128 + SIGTERM.

The event stream, not message text, is the app's machine-readable interface.

## 5. Dependency and build rules

### 5.1 Python

- Support Python `>=3.13,<3.14` for v0.1.
- Runtime dependencies. Every pin below was checked to resolve together:
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

- Build the helper bundle at `cli/dist/ScannyBoyCLI.app`, then stage a copy at
  `mac/ScannyBoy/Helpers/ScannyBoyCLI.app`. Chunk 7 added that staging step:
  an Xcode copy-files phase needs a path inside the project directory, so it
  cannot reach into `cli/dist/` directly. The staged copy is build output and
  is gitignored, exactly like `cli/dist/` itself.
- Use `BUNDLE()` in the spec with `console=True`. This is verified to produce a
  valid, signable bundle whose stdout and stderr work through pipes.
  PyInstaller also emits a plain `cli/dist/scanny-boy/` directory; ignore it
  and ship only the `.app`.
- Set a unique helper bundle identifier and `LSBackgroundOnly` so it never
  shows in the Dock.
- Launch the executable declared at
  `ScannyBoyCLI.app/Contents/MacOS/scanny-boy`.

#### Three fixes the spec file must contain

Every failure below was reproduced with the exact dependency set in section
5.1. None is optional, and none produces a build error — they fail only at
run time, so the packaged checks are what catch a regression.

1. **`tifftools` reads its own package metadata at import.** Without collected
   metadata the packaged program dies immediately with
   `importlib.metadata.PackageNotFoundError: No package metadata was found for
   tifftools`. Fix with `copy_metadata("tifftools")`.

2. **`imagecodecs` loads its codecs through delayed imports** that PyInstaller
   cannot see statically. Without them the horizontal predictor is missing and
   the first TIFF write fails with
   `imagecodecs.DelayedImportError: could not import name 'delta_encode'`.
   Fix with `collect_submodules("imagecodecs")`. This adds roughly 37 MB.
   Narrowing it to individual submodules such as `_imcd` and `_deflate` was
   tried and **does not work** — do not attempt to trim it without rebuilding
   and rerunning the packaged checks.

3. **Scanny Boy reads its own package metadata.** `manifest.py` and
   `tiff_writer.py` both call `importlib.metadata.version("scanny-boy")` for
   the manifest's Scanny Boy version and every TIFF's `Software` tag, so the
   packaged program fails with the same `PackageNotFoundError` that
   `tifftools` does. Fix with `copy_metadata("scanny-boy")`. Found in
   Chunk 7; the plan originally listed only the two fixes above.

LibRaw needs no hook. `libraw_r.25.dylib` is collected automatically. Do not
add a rawpy hook unless a packaged check proves one is needed.

#### Packaged checks and signing

- Run packaged checks with `--version`, `probe`, serial conversion, threaded
  conversion, and cancellation. These must exercise a real TIFF write, because
  that is the only thing that catches the `imagecodecs` failure.
- Inspect PyInstaller warnings and `otool -L` output.
- Copy the staged `mac/ScannyBoy/Helpers/ScannyBoyCLI.app` into
  `ScannyBoy.app/Contents/Helpers/ScannyBoyCLI.app` with Xcode's
  **Code Sign On Copy** enabled. Sign the helper before the outer app.
  `Contents/Helpers` is a permitted nested-code location; never
  `Contents/Resources`.
- In Debug builds, allow an absolute `SCANNY_BOY_CLI` environment override.
  Never find the development CLI relative to the app's current directory.
- Release builds never fall back to the repository.

For this local-only release, ad-hoc signing or Xcode's "Sign to Run Locally" is
enough. PyInstaller already ad-hoc signs the bundle it produces, and
`codesign --verify --strict` passes on it. External distribution requires a
separate signing and notarisation plan.

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

Every chunk below carries a **Status:** line. That line is the single record
of what has landed — there is deliberately no second summary table to fall out
of step with it. Update it in the same pull request that finishes the chunk,
and give the landing commit, because a closed-but-not-merged pull request is
otherwise invisible from this document. Chunk 7 was lost exactly that way: its
pull request was closed, its branch deleted, and only its build output
survived on disk, which made the tree look finished.

### Chunk 0 — Repository, licence, Python environment, and CI

**Branch:** `chunk-00-repository`

**Status:** Merged 2026-08-28, PR #1, `a747267`.

Do:

- Confirm `main` and `origin/main` are in sync before enabling branch
  protection (`git rev-list --left-right --count origin/main...main` prints
  `0	0`). They were in sync on 2026-08-28; push first if that has changed.
- Confirm the root `LICENSE` matches the exact all-rights-reserved text in
  section 3.1. Do not reword it to satisfy GitHub's licence detector.
- Check `.gitignore`. Sample NEFs (`tests/fixtures/nef/`),
  `tests/fixtures/INVENTORY.md`, environments, build output, and staging
  folders are already excluded and must stay excluded. Add generated Xcode
  projects (`mac/*.xcodeproj/`) — Chunk 8 introduces XcodeGen and section 3.1
  says generated projects are not committed.
- Remove the placeholder `scan` command and its Swift and Python contract
  types.
- Remove `mac/ScannyBoy/Resources/cli/` and its `.gitkeep`, and the
  `.gitignore` rules that reference them. The packaged helper goes to
  `Contents/Helpers`, so that path is dead.
- Configure Python 3.13 dependencies, the dev dependency group, ruff, pytest,
  `.python-version`, and `uv.lock`.
- Update `bootstrap.sh` to run uv's project sync from `cli/`.
- Add a Python CI job on `ubuntu-latest`: checkout, install uv, select Python
  3.13, `uv sync --locked`, ruff, and pytest.
- Do not add a failing placeholder Swift job.
- Protect `main` after the Python check has run successfully: require a pull
  request and the `python` status check; require the branch to be up to date
  with `main` before merging; block force-push and deletion.

Verify:

```bash
./scripts/bootstrap.sh
cd cli && uv run ruff check . && uv run pytest
gh run watch RUN_ID --exit-status
```

### User gate A — Supply sample RAW files — **SATISFIED 2026-08-28**

Six real Nikon Z f files are in `tests/fixtures/nef/`: two complete negatives
at three frames each. Verified lossless-compressed, full image area, and fixed
exposure, white balance, lens, focal length, and orientation across all six.

The local inventory is `tests/fixtures/INVENTORY.md` — filenames, byte sizes,
SHA-256 values, dimensions, timestamps, and settings. It and the NEFs are
ignored by Git and must stay ignored. Appendix A of this plan repeats the
values agents need without exposing the files.

Agents must not treat this gate as blocking. If a sample file is genuinely
absent, stop and report rather than substituting a synthetic RAW.

### Chunk 1 — Protocol and command skeleton

**Branch:** `chunk-01-protocol`

**Status:** Merged 2026-08-28, PR #2, `5ee5d8c`.

Do:

- Replace the shared contract and JSON format with section 4, including the
  full error-code table.
- Implement typed Python events and a flushing event writer.
- Add `probe` and `convert` argument parsing and stable usage errors, with
  `--files` optional on `probe`.
- Add protocol-version handling.

Tests:

- Every event serialises to one line and validates against the JSON format.
- Every write flushes.
- Invalid command, date, range, and job count return status 2 and structured
  errors where the contract promises them.
- `probe --input DIR` alone is accepted; `convert` without `--files` is not.
- A selection above 5000 files is rejected as a usage error.
- stderr never contains machine-readable events.

### Chunk 2 — Catalogue, sorting, selection, and grouping

**Branch:** `chunk-02-catalogue`

**Status:** Merged 2026-08-28, PR #3, `971e202`.

Do:

- Read the minimum NEF metadata needed for catalogue and validation.
- Implement timestamp sorting, whole-catalogue filename fallback, natural
  filename order, uninterrupted-range validation, divisibility, and grouping.
- Make `probe` return the canonical catalogue that Swift consumes, both with
  and without `--files`.
- Implement setting-consistency validation.
- **Dump every tag in the section 3.5 mapping from the real sample NEFs and
  record the results in the pull-request body.** If any tag marked
  **required** is absent from real Z f files, stop and ask the user before
  changing its status.

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
- A missing **required** tag stops the group; a missing **optional** tag warns
  and continues.

### Chunk 3 — RAW decode and base TIFF

**Branch:** `chunk-03-raw-tiff`

**Status:** Merged 2026-08-28, PR #4, `eab0fb6`.

Do:

- Add RAW decoding with the exact parameters in section 3.4.
- Add the CC0 ICC profile at
  `cli/src/scanny_boy/resources/ProPhoto-v4.icc`, load it through
  `importlib.resources`, verify its SHA-256 at startup, and record its CC0
  licence in `THIRD_PARTY_NOTICES.md`. Add the LibRaw entry at the same time.
- Write RGB16 TIFFs with Deflate, horizontal prediction, ordinary tags, and
  embedded ICC data, with one compression worker per outer RAW worker.
- Apply all four `tifffile` rules in section 3.4: `metadata=None`,
  `description=`/`software=` keyword arguments, `iccprofile=`, and compression
  code `32946`.
- Derive and record output dimensions from rawpy.
- Fail rather than writing an untagged ROMM TIFF.

Tests:

- Synthetic array round-trip preserves `uint16`, shape, channels, pixels,
  compression, prediction, orientation, and ICC bytes.
- Exactly one `ImageDescription` tag is present, and it is the intended text.
- `Software` and `ImageDescription` are actually written, not silently dropped.
- Compression code is `32946` and predictor is `2`.
- The profile's SHA-256 matches, and a corrupted profile fails with
  `ICC_PROFILE_INVALID`.
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

**Status:** Merged 2026-08-28, PR #5, `3c4c1de`.

Do:

- Implement the date rules and the curated tag mapping in section 3.5.
- Add nested EXIF metadata with `tifftools`, addressing every tag by numeric
  code.
- Preserve digitisation subseconds and time-zone offset when present.
- Validate output with both `tifftools` and an independent metadata reader in
  development tests.

Tests:

- Noon-plus-elapsed preserves order across a midnight scanning session without
  moving frames to the next film date.
- Filename fallback assigns noon plus one second per frame.
- A run that would leave the film date fails with `CAPTURE_SPAN_TOO_LONG`.
- Nested EXIF fields round-trip.
- The `tifftools` rewrite leaves decoded pixel hash, ICC bytes, compression,
  predictor, dimensions, bit depth, channel count, and Orientation unchanged.
- Exactly one `ImageDescription` survives the rewrite.
- Base and final staging paths are separate, and the base file is removed only
  after final-file verification.
- Repeated runs compare equal after excluding documented volatile fields such
  as baseline conversion time.
- Lens and exposure fields read from the sample file are present in the TIFF.
- MakerNotes and serial-number tags are absent.

### Chunk 5 — Manifest and serial group pipeline

**Branch:** `chunk-05-serial-pipeline`

**Status:** Merged 2026-08-28, PR #6, `330d03b`.

Do:

- Write `shared/contract/manifest.schema.json` covering every field in
  section 3.7.
- Add source hashing, disk checks, manifest progress records, group staging,
  and the serial conversion path.
- Add empty-folder, valid-manifest rerun, and invalid nonempty-folder rules,
  ignoring all dot-files.
- Make `--overwrite` explicit and safe.
- Commit a group only after every staged frame is complete.

Tests:

- Every written manifest validates against `manifest.schema.json`.
- Manifest format and hashes are correct.
- A `running` manifest is written and `fsync`ed before the first output
  appears.
- Completed output sizes and SHA-256 values match the files.
- A failed frame removes the whole staging directory for that group and later
  groups continue.
- Missing output space fails before decode.
- Existing files fail without `--overwrite`.
- A valid manifest plus `--overwrite` replaces expected files.
- A rerun with changed sources, hashes, order, grouping, film date, processing
  settings, or ICC is rejected with `MANIFEST_MISMATCH`.
- An unreadable or schema-invalid manifest is rejected with `BAD_MANIFEST`.
- An unrelated nonempty folder is rejected.
- A folder containing only outputs plus `.DS_Store`, `._DSC_0042.tif`, and
  `.Spotlight-V100` is still accepted.
- An output folder equal to the input folder fails with
  `OUTPUT_SAME_AS_INPUT`.
- Manifest output paths cannot escape the output folder through absolute paths,
  `..`, or symlinks.
- A source changed after hashing is rejected before its group is published.
- A forced test failure after the first TIFF is moved into place leaves the
  group incomplete; recovery replaces every expected output in that group.
- Disk checks fail just below and pass just above the required-free-bytes
  formula for both a new run and a pure overwrite.
- No staging directory survives normal success or handled failure.

### Chunk 6 — Threaded conversion and cancellation

**Branch:** `chunk-06-concurrency`

**Status:** Merged 2026-08-28, PR #7, `c130c0c`.

Do:

- Add the thread-worker path and conservative default worker count.
- Keep `--jobs 1` independent of the executor.
- Implement the section 3.8 memory budget: reduce the default silently, reject
  an explicit `--jobs` with `INSUFFICIENT_MEMORY`.
- Add cooperative SIGTERM cancellation and final status handling, exiting 143.

Tests:

- Jobs 1 and 4 produce equal pixel hashes and metadata after documented
  changing fields are ignored.
- Every source index reports one final result despite out-of-order completion.
- A controlled blocking test cancels only after work has definitely started;
  do not use a race-prone fixed sleep.
- Cancellation removes the current group, retains completed groups, updates
  the manifest, and exits 143.
- Queued work is cancelled, running workers stop, and only then is the staging
  directory deleted.
- Forced termination may leave a `running` manifest and staging directory.
  The next run removes that abandoned staging work and safely reruns the
  incomplete group.
- Thread workers never return image arrays to the parent.
- TIFF compression uses one inner worker.
- On a simulated 7 GB machine the default worker count always resolves,
  never below one and never above what the budget permits — section 2.5's
  "any memory guard must not reject a default run on that machine". At
  640 MiB that machine permits five workers, so its default of three is
  admitted unreduced; do not assert that a reduction happens. An explicit
  `--jobs 12` on the same machine is rejected with `INSUFFICIENT_MEMORY`.
- Record peak resident memory for jobs 1 and 4, and raise the per-worker
  budget if the measured peak plus 25% exceeds it. **Done:** the measurement
  is the table in section 3.8, and the budget is now 640 MiB.
- Record benchmark results, but do not require a fixed speedup.

### Chunk 7 — Package the command-line program

**Branch:** `chunk-07-package-cli`

**Status:** Merged 2026-08-28, `487c589` via `dfb4d33`. PR #8 was closed
rather than merged and its branch deleted; the commit was recovered from a
dangling object and merged directly.

Do:

- Build a PyInstaller one-directory distribution wrapped in
  `ScannyBoyCLI.app` using `BUNDLE()` with `console=True`.
- Add all three fixes of section 5.2 to the spec — `copy_metadata("tifftools")`,
  `collect_submodules("imagecodecs")`, and `copy_metadata("scanny-boy")`. Every
  one is required, and none fails at build time.
- Bundle all runtime libraries and the ICC profile.
- Add `scanny-boy --version` (section 4.1). The packaged checks below need it
  and the command-line program had none before this chunk.
- Update `build-cli.sh` to invoke PyInstaller through `uv run`, and to stage
  the `.app` at `mac/ScannyBoy/Helpers/ScannyBoyCLI.app` rather than the plain
  `dist/scanny-boy/` directory.
- Inspect macOS library dependencies rather than assuming hooks are missing.
- Give the helper a unique bundle identifier and `LSBackgroundOnly`.
- Verify the helper's signature before it can be copied into the outer app.

Tests:

- Packaged `--version`, `probe`, jobs 1, jobs 4, and cancellation work.
- **The packaged checks must write a real TIFF**, because that is the only
  thing that catches the `imagecodecs` delayed-import failure.
- Development and packaged runs have equal pixel hashes and metadata after
  documented changing fields are ignored.
- No dependency is loaded from `cli/.venv`.
- `codesign --verify --strict` succeeds for the helper bundle.

### Chunk 8 — XcodeGen project and streaming runner

**Branch:** `chunk-08-xcode-runner`

**Status:** Merged 2026-08-29, PR #9, `54c5ab7`.
`ScannyBoyUITests` is built but not run: its XCUITest
runner fails to start on this machine, so it is absent from the scheme's test
targets rather than making a required check flaky. Chunk 10 owns the UI tests.

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
  Xcode 16.2 is present on `macos-15` as of 2026-08-27; if a future image drops
  it, move to the newest Xcode 16 available rather than pinning a missing path.
- Add the `swift` job to required branch checks after it passes.

Tests:

- Decode every event and preserve unknown events safely.
- Reassemble JSON split across reads.
- Drain large stdout and stderr concurrently without deadlock.
- Do not finish until the process and both output streams have ended.
- Drive the runner with a temporary test executable.
- Test normal success, structured failure, cooperative cancellation exiting
  143, raw signal-15 termination, and forced-cancel classification.
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

**Status:** Not started.

Do:

- Add input-folder selection and last-folder memory.
- Call `probe --input DIR` to get the canonical catalogue before any selection
  exists, then call `probe` again with `--files` once the user has selected a
  range.
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
- Choosing an output folder equal to the input folder is blocked.

### Chunk 10 — Run, progress, cancellation, and completion UI

**Branch:** `chunk-10-run-ui`

**Status:** Not started.

Do:

- Add Run and overwrite confirmation.
- Show the pipeline step, current filename, completed count, elapsed time, and
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

**Status:** Not started.

Do:

- Rewrite root and component READMEs from a clean-clone perspective. The root
  README still describes a binary copied into `Resources/cli`; that is now
  wrong.
- State clearly in the root README that the project is all rights reserved and
  point readers to `LICENSE`.
- Add `CONTRIBUTING.md` for the one-person workflow. It must not imply that
  outside contributions are accepted under an open-source licence.
- Add `docs/DECISIONS.md` based on section 3.
- Document the sample-file requirement and the required lossless-compressed
  NEF camera setting.
- Document local-only Apple-silicon scope and deferred distribution work.
- Verify a fresh clone using only the written instructions.

After the documentation PR merges and both required checks pass, create and
push an annotated `v0.1.0` tag. Do not create the tag from the PR branch.

## 7. Test rules

- Python tests live next to Python code as `*_test.py`.
- Swift tests use Swift Testing.
- Sample NEFs live at `tests/fixtures/nef/`, at the repository root, not under
  `cli/`. Resolve that directory from the test file's own location, never from
  the current working directory, so tests pass from any directory.
- Tests needing local NEFs skip clearly when sample files are absent, using one
  shared helper rather than a per-file check.
- A skipped sample-file test must say what was not tested.
- Expected fixture values (SHA-256, dimensions, timestamps, settings) are in
  appendix A. Do not hard-code a pixel hash: it depends on the LibRaw build.
  Compare two decodes of the same file to each other instead.
- Do not mock rawpy's decoding. Use a real NEF or test a lower-level synthetic
  TIFF operation.
- The synthetic-TIFF route above has one shared implementation:
  `fake_nef_support.write_fake_nef` writes a tiny TIFF carrying crafted IFD0
  and nested-EXIF tags, so metadata tests can control exactly which tags are
  present without a real RAW. It works because the catalogue and metadata
  code reads through `exifread`, which cares about TIFF/EXIF structure and
  not about whether the file is really a NEF. These fixtures are **not**
  openable by rawpy, so anything reaching `rawpy.imread` — white-balance
  multipliers, and the full `probe --files` path — must still be tested
  against the real sample NEFs.
- Compare pixel hashes and metadata after documented changing fields are
  ignored, not entire TIFF bytes, which contain conversion timestamps.
- Use isolated temporary directories. Do not share filenames between
  concurrently running Swift tests.
- Schema conformance is checked by hand-rolled validators, not a JSON Schema
  engine: `schema_test_support.py` for events and
  `manifest_schema_test_support.py` for manifests. Both read their enums and
  required-field lists out of the schema files themselves, so a
  code/schema divergence is still caught. This keeps the dev dependency set
  exactly what section 5.1 lists, at a real cost: neither validator checks
  value types or `additionalProperties`. Where section 3.7 says Chunk 5's
  tests "validate every written manifest against it", that is the strength of
  the guarantee — structural, not total. Adding `jsonschema` to the dev group
  would close the gap and needs section 5.1 changed to match.
- Never hide a failing build by piping it through `tail`.
- Synthetic test images must not be pure random noise. Deflate cannot compress
  it, and a full-resolution random frame takes minutes to write instead of
  seconds. Use gradients with light noise.

## 8. Human approval points

Implementation pauses for the user at:

1. Sample-file supply before Chunk 2. **Satisfied 2026-08-28** — see user
   gate A and appendix A.
2. The Chunk 2 report on which EXIF tags real Z f files actually contain, if
   any **required** tag is missing. All required tags were present on
   2026-08-28, so this is not expected to trigger.
3. Visual RAW/TIFF approval in Chunk 3.
4. Overwrite and cancellation behaviour in the finished app.
5. Final clean-clone and end-to-end sign-off before `v0.1.0`.

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
- **Silent metadata loss:** `tifffile` drops some `extratags` without failing
  and can write a duplicate `ImageDescription`. Assert tag presence and count
  in tests rather than trusting the write.
- **Packaged helper failure:** the two failures in section 5.2 do not appear at
  build time. Packaged checks must perform a real conversion.
- **Unexpected memory or disk use:** stage one negative at a time, cap default
  threads at four, have workers write files directly, and budget uncompressed
  size with margin.
- **Cancellation damage:** stage by group, record progress in the manifest,
  clean through normal control flow, and make reruns replace incomplete groups.
- **CI image changes:** use `macos-15`, select the Xcode version explicitly,
  and re-check that the pinned Xcode is still on the image.

## 10. Phase 2 compatibility only

Phase 1 guarantees:

- a complete manifest, matching `manifest.schema.json`, with ordered groups
  and source hashes;
- one upright RGB16 ROMM TIFF per source;
- fixed processing settings recorded in the manifest;
- no partially published group after handled failures or cancellation.

Do not choose an OpenCV registration model in Phase 1. The later stitching
plan must test translation, rigid, partial-affine, and full-affine models on
real sample files. It must estimate transforms from derived detection images,
linearise ROMM masters before interpolation or blending, and define crop and
seam behaviour separately.

## 11. Primary references

- [rawpy parameters](https://letmaik.github.io/rawpy/api/rawpy.Params.html)
- [rawpy thread support](https://github.com/letmaik/rawpy/releases/tag/v0.21.0)
- [rawpy 0.27.0 / LibRaw 0.22.1](https://github.com/letmaik/rawpy/releases/tag/v0.27.0)
- [LibRaw processing parameters](https://www.libraw.org/docs/API-datastruct.html)
- [LibRaw supported cameras](https://www.libraw.org/supported-cameras/)
- [LibRaw licensing](https://www.libraw.org/node/2228)
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
- [macos-15 runner image contents](https://github.com/actions/runner-images/blob/main/images/macos/macos-15-Readme.md)
- [macOS 14 runner retirement](https://github.com/actions/runner-images/issues/13518)
- [GitHub branch protection](https://docs.github.com/en/rest/branches/branch-protection)
- [XcodeGen](https://github.com/yonaskolb/XcodeGen)

## 12. Agent handoff

Use the header in `docs/CHUNK_PROMPT.md`, one chunk at a time, in chunk order.

## Appendix A — Sample NEF facts

Recorded 2026-08-28 from `tests/fixtures/nef/`. The files themselves are not in
Git; `tests/fixtures/INVENTORY.md` holds the same table locally. Use these
values in tests and comparisons instead of re-deriving them.

Camera: `NIKON CORPORATION` / `NIKON Z f`. All six open and postprocess with
the section 3.4 `RAW_PARAMS`, confirming lossless compression.

Negatives, at three shots each:

| Negative | Frames |
| --- | --- |
| 1 | `_DSC4638.NEF`, `_DSC4639.NEF`, `_DSC4640.NEF` |
| 2 | `_DSC4644.NEF`, `_DSC4645.NEF`, `_DSC4646.NEF` |

The catalogue is exactly these six files, so all six form one uninterrupted
range. The break in frame numbers between 4640 and 4644 is **not** a catalogue
gap, and a six-file selection must not be rejected as non-contiguous. To test
`NON_CONTIGUOUS_SELECTION` with real files, select frames 1, 2, 4, 5, 6.

Sizes and hashes:

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `_DSC4638.NEF` | 32001076 | `b1be8ed9ec75745d83470270be7941e83f9858ad7e1631323217a2a569f4c166` |
| `_DSC4639.NEF` | 33256263 | `0a44470b754c3520308551f8939e6aed2a13cb3e174031457678ae73832a8071` |
| `_DSC4640.NEF` | 34383334 | `968506b90b7ab422aab140cb43b4e3d4d16c43265ffb913d82e3c939741b92b4` |
| `_DSC4644.NEF` | 31688590 | `87e1e21271c09c445ada78f8206d11154cb6a159eca5c3d085af994f1341ad85` |
| `_DSC4645.NEF` | 29485555 | `00e3d3b5796ab00a0479e00b0adcb16ac1e68337bf7d58ce294384c3f7f3ae3e` |
| `_DSC4646.NEF` | 29482374 | `fe7d264ebc167bf6357730a2e946919ac324e287396919052ce64b246e59841a` |

Dimensions, identical for all six: `raw_width` 6064, `raw_height` 4040,
`width` 6064, `height` 4040, `iwidth` 6064, `iheight` 4040, `flip` 0.
`postprocess(**RAW_PARAMS)` returns shape `(4040, 6064, 3)`, dtype `uint16`,
146,991,360 bytes = **140.2 MiB**, in about 1.1 s.

Because `flip` is 0, these files exercise the no-rotation path only. Do not
conclude from them that orientation handling is untested; keep the
Orientation-is-always-1 assertions of section 3.4.

Settings, identical across all six, so a six-file selection passes
consistency validation:

| Field | Value |
| --- | --- |
| `ExposureTime` | `1/30` |
| `FNumber` | `8` |
| `PhotographicSensitivity` (ISO) | `100` |
| `FocalLength` | `55` |
| `LensModel` | `55mm f/2.8` |
| `Orientation` | `1` (horizontal / normal) |
| `camera_whitebalance` | `[1.691406, 1.0, 1.378906, 1.0]` |
| `OffsetTimeOriginal` | `-05:00` |

Every tag in the section 3.5 mapping is present, **required** and **optional**
alike, including `Make`, `Model`, `LensModel`, `DateTimeDigitized`, and
`OffsetTimeOriginal`. The Chunk 2 approval gate for a missing required tag
should not trigger; Chunk 2 must still produce the dump and report it.

Capture timestamps, all with subseconds:

| File | `DateTimeOriginal` | `SubSecTimeOriginal` |
| --- | --- | --- |
| `_DSC4638.NEF` | 2026:08:02 12:33:27 | `77` |
| `_DSC4639.NEF` | 2026:08:02 12:33:41 | `45` |
| `_DSC4640.NEF` | 2026:08:02 12:33:52 | `02` |
| `_DSC4644.NEF` | 2026:08:02 12:35:34 | `15` |
| `_DSC4645.NEF` | 2026:08:02 12:35:48 | `62` |
| `_DSC4646.NEF` | 2026:08:02 12:36:03 | `93` |

The span is about 2 minutes 36 seconds, so synthetic noon-plus-elapsed times
stay far inside the film date. `CAPTURE_SPAN_TOO_LONG` cannot be reached with
these files; test it with synthetic timestamps.

Disk-check sanity: six outputs at `P` = 6064 × 4040 × 3 × 2 = 146,991,360
bytes each. With `B = ceil(P × 1.05)` = 154,340,928, `M` = 6, `G` = 3, and
`D` = 1 MiB, the section 3.9 requirement for a fresh six-file run is

```text
ceil((6 × B + 2 × 3 × B + 1 MiB) × 1.20) = 2,223,767,655 bytes ≈ 2.07 GiB
```

Earlier revisions of this appendix said "about 1.42 GiB". That figure was
wrong — it did not follow the section 3.9 formula, which
`cli/src/scanny_boy/disk_check.py` implements exactly. Use 2.07 GiB.
