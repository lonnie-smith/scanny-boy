# Decisions

This is a readable summary of the locked decisions in
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) section 3. **The plan is
authoritative.** If this file and the plan ever disagree, the plan wins —
that mismatch is a bug in this file, not a licence to follow whichever one is
convenient. Changing any decision below means updating the plan first, and
only after asking the user (plan section 3's own rule); this file just makes
those decisions easier to find without reading the whole plan.

## Product and repository

- Python owns all logic: file discovery, validation, sorting, grouping,
  conversion, manifest, and progress reporting. Swift is the interface only,
  and starts the Python program as a subprocess — it never re-sorts or
  re-validates on its own.
- The repository is public, but the project's own code is all rights
  reserved (see [`LICENSE`](../LICENSE)). No open-source licence, no SPDX
  identifier, no open-source badge. Public visibility is for reference, not
  reuse.
- Bundled third-party assets (LibRaw, the embedded ICC profile) keep their
  own licences, recorded in [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).
- `mac/project.yml` is the source of truth for the Xcode project; the
  generated `.xcodeproj` is never committed.
- `main` requires a pull request and passing status checks, and a branch
  must be up to date with `main` before merging. No required approval count
  — this is a one-person project. Force-push and branch deletion are
  blocked.
- Each implementation chunk is one branch and one pull request, merged in
  order.

## Input rules

- Accept `.nef` files case-insensitively from one folder, no recursion.
  Reject duplicates and anything outside the chosen folder.
- The selection must be one uninterrupted range of the catalogue in
  canonical order.
- Shots per negative: 1–12, default 3. The selected count must divide evenly
  by it.
- The camera workflow requires lossless-compressed NEF (never High
  Efficiency or High Efficiency\*), fixed manual exposure, fixed manual white
  balance, one lens and focal length, and one camera orientation across the
  whole selection.
- White balance is validated from `raw.camera_whitebalance` itself —
  normalised, compared with a `1e-6` tolerance — not from an EXIF Manual/Auto
  flag.

## Sorting

- Sort by NEF `DateTimeOriginal` (with `SubSecTimeOriginal` when present);
  break exact ties with natural filename order.
- If any file in the whole catalogue lacks a usable timestamp, the entire
  catalogue falls back to natural filename order and a warning is emitted —
  even if the affected file is outside the selection.
- Never mix timestamp and filename comparisons within one sort.
- No warning for uneven time gaps between frames; there's no measured
  threshold to base one on.

## Pixel output

- One TIFF per source frame, three-channel unsigned 16-bit RGB, named from
  the source (`DSC_0042.NEF` → `DSC_0042.tif`).
- Decode with the source orientation applied so pixels are upright, then
  write TIFF `Orientation` as `1` always — never the source value.
- Encode in ROMM RGB (ProPhoto RGB), standard transfer curve. Every TIFF
  embeds a vetted, checksum-verified ICC profile; an untagged ROMM file is
  never written.
- Lossless Deflate compression with horizontal prediction, one compression
  worker per outer RAW worker.
- Fixed exposure is preserved by disabling both auto-brightness and
  content-dependent maximum adjustment (`no_auto_bright`,
  `adjust_maximum_thr=0.0`).
- The exact `RAW_PARAMS` dict and the four `tifffile` writing rules
  (`metadata=None`, `description=`/`software=` keywords, `iccprofile=`
  keyword, compression code `32946`) are in plan section 3.4 and must not
  drift — each was independently verified to matter.

## Metadata

- The user supplies a film date, not a time. Synthetic ordering times start
  at noon on that date and add each frame's elapsed scan time (or one second
  per frame, if sorting fell back to filenames), strictly increasing.
  Leaving the film date fails with `CAPTURE_SPAN_TOO_LONG`.
- IFD0 and EXIF tags are curated, not copied wholesale — see plan section 3.5
  for the full table. Required tags (exposure, aperture, ISO, focal length)
  stop conversion if missing; optional tags warn and are omitted. Nikon
  MakerNotes, serial numbers, and thumbnails are never copied.
- The nested EXIF directory is written with `tifftools` in a second pass,
  addressed entirely by numeric tag code (its name constants don't match
  plan section 3.5's names for two tags). The base file is removed only
  after the final file is verified.

## Output folder, overwriting, and grouping

- One output folder holds one run. It must differ from the input folder
  (`OUTPUT_SAME_AS_INPUT`).
- An empty folder is valid. A nonempty folder needs a valid Scanny Boy
  manifest to be accepted; dot-files (`.DS_Store`, AppleDouble files, etc.)
  are always ignored when judging this.
- A rerun must match the previous run's sources, hashes, order, grouping,
  film date, processing settings, and ICC hash, or it's rejected as
  `MANIFEST_MISMATCH`. The CLI rejects conflicts by default; `--overwrite`
  is explicit and the app only passes it after the user confirms.
- Each negative is staged as a group and published atomically: if any frame
  in a group fails, the whole group's staging directory is deleted and the
  next group continues. Completed groups survive cancellation; the group in
  progress does not.

## Manifest

- `scanny-boy-manifest.json` is written to a temp file, fsynced, then
  renamed into place, so readers never see a half-written manifest.
  `shared/contract/manifest.schema.json` is the authoritative format.
- The manifest records enough to make Phase 2 safe to build on: run status,
  every source's path/size/mtime/hash, canonical order, groups, expected and
  completed outputs with their hashes, and processing settings.

## Concurrency and cancellation

- `ThreadPoolExecutor` for parallel RAW work (rawpy's LibRaw build releases
  the GIL). Default workers:
  `min(shots_per_negative, os.process_cpu_count() or 1, 4)`. `--jobs 1` uses
  a fully serial path.
- A 640 MiB per-worker memory budget (measured in Chunk 6, see plan section
  3.8's table) silently reduces the *default* worker count but rejects an
  *explicit* `--jobs` with `INSUFFICIENT_MEMORY`.
- Cancellation is cooperative via SIGTERM: stop submitting work, let running
  workers finish their current step, wait for them, then clean up and exit
  143. A forced kill after a grace period can leave a `running` manifest and
  an orphaned staging directory; the next run detects and cleans that up.

## Disk checks

- Required free space is computed conservatively from pixel dimensions,
  compression-free size assumptions, the largest group size, and estimated
  manifest size, then padded 20%. The exact formula is in plan section 3.9.

## Scope this project does not cover

- **Distribution:** no App Store, no Developer ID signing, no notarisation,
  no Intel build. Ad-hoc signing is enough for this local, single-user
  release.
- **Stitching:** Phase 1 produces one upright TIFF per frame and a manifest;
  it does not register, stitch, crop, or invert negatives. That's Phase 2,
  which Phase 1's manifest is deliberately built to support (plan section
  10) without committing to a registration model yet.
