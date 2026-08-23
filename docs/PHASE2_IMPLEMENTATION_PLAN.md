# Scanny Boy — Phase 2 implementation plan

**Written:** 2026-08-29
**Status:** awaiting user gate B (sample scans) before Chunk P2-1.

This plan is authoritative for Phase 2 the way
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) is authoritative for
Phase 1. Where the two overlap, the Phase 1 plan still governs Phase 1's
code; section 3 below records the two places Phase 2 deliberately amends it.

## 0. Scope correction — read this first

The original Phase 2 brief said "Phase 1 covered workflow steps 1–5, so
Phase 2 is steps 6 and 7." That is wrong, and the whole plan turns on it.

**Stitching was never implemented.** Phase 1 stops after writing one upright
16-bit ROMM TIFF per *frame* plus a manifest. Three places in the repository
say so:

- `IMPLEMENTATION_PLAN.md` section 10: "Do not choose an OpenCV registration
  model in Phase 1."
- `DECISIONS.md`: "Phase 1 produces one upright TIFF per frame and a
  manifest; it does not register, stitch, crop, or invert negatives."
- Every TIFF Phase 1 writes is stamped `"<source>: unstitched scan frame"`
  (`cli/src/scanny_boy/tiff_writer.py`).

So the folder the app currently calls the "output folder" is really an
*intermediate* folder, and **Phase 2 covers workflow steps 5, 6, and 7**:
register and composite each negative's frames, write one TIFF per negative to
the folder the user chooses, and remove the intermediates.

## 1. Goal

Extend the app so that one run takes a selection of NEFs all the way to
finished negatives:

1. Everything Phase 1 already does, but into a working directory the user
   never has to think about.
2. Register each negative's frames against each other — rigid model,
   rotation and translation only.
3. Composite them into one planar panorama per negative, blended in linear
   light.
4. Write one 16-bit ROMM RGB TIFF per negative into the user's chosen output
   folder, carrying the same colour profile and curated metadata rules
   Phase 1 established.
5. Remove the intermediate per-frame TIFFs, keeping them only when the user
   asks or when something failed.
6. Record everything about the stitch — solved geometry, per-pair quality
   numbers, canvas, valid area — in a manifest in the output folder.

Phase 2 still does not invert, crop, or colour-correct negatives. Those are
Phase 3 (`punchlist.md`).

### 1.1 Vocabulary added to Phase 1's section 1.1

Phase 1's vocabulary still applies unchanged — catalogue, canonical order,
selection, group, pipeline step, staging directory, published. Phase 2 adds:

- **Negative** — a group, seen from the output side. One negative produces
  exactly one stitched TIFF. Where Phase 1 says "group", Phase 2 says
  "negative" for the same thing whenever the output is what matters.
- **Work directory** — where the per-frame TIFFs and the Phase 1 manifest
  live. Normally a temporary directory the user never sees; `--work` can
  place it somewhere durable.
- **Intermediate** — one per-frame TIFF in the work directory. Phase 1 calls
  these "outputs"; from Phase 2's point of view they are inputs.
- **Detection image** — a small, contrast-normalised 8-bit greyscale
  derivative of an intermediate, used only for finding and matching
  features. Never composited, never written out.
- **Pair** — two frames of one negative that were matched against each
  other, with a solved relative transform and quality numbers.
- **Layout** — the solved position and rotation of every frame of one
  negative in a common canvas coordinate system.
- **Canvas** — the axis-aligned bounding box of every placed frame of one
  negative. The stitched TIFF is exactly canvas-sized.
- **Valid rectangle** — the largest axis-aligned rectangle inside the canvas
  containing no fill pixels. Recorded, never applied, in Phase 2.
- **Roll manifest** — `scanny-boy-roll.json`, the output folder's record of
  one stitched roll. Distinct from Phase 1's `scanny-boy-manifest.json`,
  which now lives in the work directory.

## 2. Facts verified before implementation

Everything in this section was checked by running code on the development
machine on 2026-08-29, in a throwaway virtual environment, exactly the way
Phase 1's section 2 was established. Items are marked **(measured)** where a
number came out of a real run. Trust these; do not re-litigate them.

Facts that could only be established against real Nikon Z f scans are
**not** here — they are Chunk P2-1's job, and they are listed in section 5
as things that chunk must measure and record.

### 2.1 OpenCV availability and API surface

- `opencv-python-headless` installs cleanly on Python 3.13.3, macOS arm64,
  with no system libraries and no Qt or FFmpeg dependency. Two lines resolve
  today: **5.0.0.93** and **4.14.0.94**. **(measured)**
- **OpenCV 5.0.0 ships only SIFT and ORB.** `cv2.AKAZE_create`,
  `cv2.KAZE_create`, `cv2.BRISK_create`, and `cv2.xfeatures2d` are all absent
  from the 5.0.0.93 headless wheel. OpenCV **4.14.0** has SIFT, ORB, **and**
  AKAZE. **(measured)**
- On the synthetic benchmark of section 2.2, AKAZE produced roughly four
  times as many good matches as SIFT. Losing it would remove the strongest
  of the three candidates before Chunk P2-1 can even compare them. **This is
  why section 5.1 pins the 4.x line.**
- The 4.14.0.94 macOS arm64 wheel is 44.3 MiB. **(measured)**
- Every OpenCV symbol this plan depends on exists in 4.14.0:
  `SIFT_create`, `ORB_create`, `AKAZE_create`, `BFMatcher`,
  `estimateAffinePartial2D`, `estimateAffine2D`, `distanceTransform`,
  `warpAffine`, `createCLAHE`, `resize`, `erode`, `Canny`, `HoughLinesP`,
  and the `INTER_LANCZOS4` / `INTER_CUBIC` / `INTER_AREA` / `RANSAC`
  constants. **(measured)**
- OpenCV is Apache-2.0 and must be added to `THIRD_PARTY_NOTICES.md`.

### 2.2 The registration model

Measured on a synthetic film-like scene — smooth gradients, blobs, and fine
grain, never pure noise, per Phase 1's test rules — cut into two 2100×1400
frames with a known 3.0° rotation and 25% overlap:

| Detector | keypoints | good matches | inliers | recovered scale | angle error | rigid RMS | time |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SIFT | 296 / 182 | 39 | 21 (54%) | 0.99965 | 0.031° | 1.14 px | 0.28 s |
| ORB | 1563 / 611 | 76 | 61 (80%) | 1.00007 | 0.021° | 1.13 px | 0.09 s |
| AKAZE | 1733 / 1046 | 163 | 99 (61%) | 1.00009 | 0.041° | 1.01 px | 0.37 s |

What this establishes, and what it does not:

- **Scale is 1.** All three estimators, given freedom to fit a similarity,
  returned a scale within 0.0004 of unity. The copy stand really does hold
  magnification constant, so the locked model is **rigid (Euclidean):
  rotation and translation, scale fixed at exactly 1**, not affine and
  certainly not homography. **(measured)**
- Several degrees of rotation are recovered to better than 0.05°, so the
  user's "maybe several degrees between frames" is comfortably inside what
  this approach handles. **(measured)**
- The keypoint counts are **not** evidence about real negatives. This
  synthetic scene is smoother than film grain and unfairly punishes SIFT.
  Detector choice is Chunk P2-1's decision, made on real scans.
- The RMS figures include resampling error from how the fixtures themselves
  were generated, so they are an upper bound, not a target.

### 2.3 Compositing primitives

- `cv2.warpAffine` accepts a 3-channel `float32` image directly and warps a
  2100×1400×3 frame in 0.03 s. No tiling is needed for correctness.
  **(measured)**
- **`INTER_LANCZOS4` undershoots below zero** — a warp of data clipped to
  `[0, 1]` produced a minimum of **−0.088**. Warped linear-light data must
  therefore be clamped to `>= 0` before it is accumulated, or the blend will
  carry negative energy into the result. **(measured)**
- `cv2.distanceTransform(mask, DIST_L2, 5)` returns `float32` distances and
  is the intended source of feather weights: it produces a weight that falls
  linearly to zero at the border of an arbitrarily shaped, rotated frame,
  which a simple axis-aligned ramp cannot do. **(measured)**
- The ROMM transfer curve round-trips **bit-exactly**. Decoding all 65,536
  16-bit codes to linear and re-encoding them returned the identical code in
  every case — maximum error 0 LSB, zero codes changed. **(measured)**

  ```python
  # decode (encoded -> linear); breakpoint is in the ENCODED domain
  L = E / 16.0            if E < 0.03125      else E ** 1.8
  # encode (linear -> encoded); breakpoint is in the LINEAR domain
  E = L * 16.0            if L < 0.001953125  else L ** (1 / 1.8)
  ```

  Phase 1's section 3.4 warns about exactly this pair of breakpoints. Both
  appear above, each in its own domain; swapping them is the documented
  mistake.

  This proves the LUT pair is self-consistent. It does **not** prove that
  rawpy's `gamma=(1.8, 16)` output actually follows this curve — Chunk P2-1
  must check that against a real intermediate TIFF.

### 2.4 Size and memory arithmetic

From Phase 1's appendix A: one frame is 6064×4040, so `P` = 146,991,360
bytes = 140.2 MiB.

A frame rotated by θ has an axis-aligned bounding box of
`w·cosθ + h·sinθ` by `w·sinθ + h·cosθ`. At θ = 5° that is 6392×4553.

For `N` frames in a strip sharing a fraction `v` of their width, the canvas
is roughly `Wbbox + (N−1)(1−v)·W` wide. Worked examples at v = 0.25, θ = 5°:

| Frames | Canvas ≈ | Stitched TIFF (uncompressed) | Composite peak RAM |
| --- | --- | --- | --- |
| 3 | 13,972 × 4,553 | 382 MB | 1.8 GB |
| 4 | 20,036 × 4,553 | 547 MB | 2.5 GB |
| 6 | 32,164 × 4,553 | 879 MB | 4.0 GB |
| 12 | 68,548 × 4,553 | 1.87 GB | 8.6 GB |

Two consequences the plan must handle rather than discover:

- **A 12-frame negative is 68,548 px wide.** That exceeds the 30,000-pixel
  limit most consumer editors impose on TIFF, and its peak memory is beyond
  a 16 GB machine. Section 3.7 defines the guards.
- Everything at the realistic end — three or four frames per negative — is
  comfortable, which is why section 3.6 composites the whole canvas at once
  instead of building a tiling engine nobody needs.

## 3. Decisions implementation agents must preserve

**This section is locked, exactly as Phase 1's section 3 is locked.** Do not
change a decision here — a threshold, a code, a file name, a formula —
without stopping and asking the user. If a decision looks wrong, say so and
wait. Do not "improve" it in passing.

Two decisions carry an explicit **user-approved amendment** to the Phase 1
plan; they are marked as such.

### 3.1 Shape of the work

- Phase 2 delivers steps 5, 6 and 7 **end to end, including the SwiftUI
  app**. A CLI that stitches but no app that drives it is not done.
- Python continues to own all logic. Swift remains the interface, and still
  never sorts, validates, or decides anything for itself.
- Phase 1's `convert` command is **not modified in meaning**. It still turns
  a selection of NEFs into per-frame TIFFs in the folder given by `--out`.
  Phase 2 simply points it at a work directory. Every Phase 1 test must keep
  passing untouched; a chunk that needs to rewrite Phase 1's conversion
  tests has gone wrong.
- Each chunk is one branch, one pull request, merged in order, exactly as in
  Phase 1.

### 3.2 Frame layout and the registration model

- A negative's frames form a **one-dimensional strip**, but **capture order
  is not assumed to be spatial order**. The sequence may run right-to-left,
  or be shuffled. Nothing may depend on frame *k* neighbouring frame *k+1*.
- Therefore: match **all pairs** of a negative's frames, then solve a global
  layout. Do not implement neighbour chaining, and do not try to detect the
  order first — the global solve makes order irrelevant for free.
- The geometric model is **rigid: rotation plus translation, scale fixed at
  exactly 1**, per section 2.2. Never fit an affine or a homography to
  produce the final transform. `estimateAffinePartial2D` may be used to get
  RANSAC inliers, but the transform that is *used* is always re-fitted
  rigidly from those inliers (closed-form Umeyama with scale forced to 1).
- Rotation between frames may be **several degrees**. Resampling is
  therefore unavoidable; do not build an integer-shift fast path.
- Overlap is **at least 20% on every overlapping edge**, guaranteed by the
  capture workflow. Treat that as a validation expectation, not as an
  assumption the solver relies on.

### 3.3 Colour, resampling, and blending

- All geometric and photometric work happens in **linear light**. Decode
  ROMM to linear `float32` before warping, blending, or comparing anything;
  encode back to 16-bit ROMM once, at the end. Phase 1's section 3.4
  requires this and section 2.3 above proves the round trip is exact.
- Use the section 2.3 formulas, implemented as a **65,536-entry `float32`
  lookup table** for decode. Do not call `numpy.power` per pixel.
- Warp with `INTER_LANCZOS4` on `float32`, `BORDER_CONSTANT`, border value
  0. **Clamp the result to `>= 0` immediately after warping** (section 2.3).
- Warp each frame into **its own bounding box**, not into the full canvas,
  and record that box's offset. This is what keeps peak memory to the
  section 2.4 figures.
- Warp the validity mask separately with `INTER_NEAREST`, then **erode it by
  5 pixels** — Lanczos4 has a support radius of 4, so the outermost 4 pixels
  of a warped frame are contaminated by the zero border, and one more pixel
  is cheap insurance.
- Blend by **linear feather in linear light**: per-frame weight is
  `cv2.distanceTransform(eroded_mask, DIST_L2, 5)`, and the output is
  `sum(weight × linear_rgb) / sum(weight)` wherever `sum(weight) > 0`.
- This blend choice is deliberate and provisional. It is safe *because* the
  user's exposure and white balance are locked across a roll, so there is no
  exposure step to hide. **Chunk P2-11 must record it in the README along
  with the alternatives that were not chosen** — a hard seam at the overlap
  midline (preserves grain exactly, shows any misalignment as a line) and a
  multi-band Laplacian blend (hides misalignment best, softens fine grain,
  much heavier) — so a later revisit starts from the reasoning rather than
  from scratch.
- Pixels covered by no frame are filled with **`FILL_COLOR`, a single named
  constant, initially black (0, 0, 0)**. `punchlist.md` already contemplates
  changing it to a contrasting colour in Phase 3; it must therefore be one
  constant in one place, and its value must be recorded in the roll manifest
  so a file can be interpreted without knowing which build wrote it.

### 3.4 Quality gates

Every stitched negative must be *proved* correct, not merely finished.
Record all of these in the roll manifest; fail the negative when a gate is
exceeded.

Per pair:

- `inliers` — RANSAC inlier count. Gate: `>= MIN_PAIR_INLIERS`.
- `inlier_ratio` — inliers / good matches. Gate: `>= MIN_PAIR_INLIER_RATIO`.
- `rms_residual_px` — RMS reprojection residual of the inliers under the
  **rigid re-fit**, reported in **full-resolution pixels** (scale the
  detection-space figure up by the detection scale factor). Gate:
  `<= MAX_PAIR_RMS_PX`.
- `scale_drift` — `|scale − 1|` from the similarity fit before the rigid
  re-fit. Above `SCALE_DRIFT_WARN` emits `STITCH_SCALE_DRIFT`; above
  `SCALE_DRIFT_FAIL` fails the pair. A real scale change means the copy
  stand moved, and no rigid model can be correct.
- `overlap_fraction` — measured, recorded, never gated. It tells the user
  what they are actually shooting.
- `overlap_mad` — **the honest gate.** After warping, the mean absolute
  difference between the two frames' linear-light values over their shared
  valid area, normalised by the mean level. This is the only metric that
  measures whether the pixels actually line up, rather than whether the
  solver was pleased with itself. Gate: `<= MAX_OVERLAP_MAD`.

Per negative:

- `global_rms_px` — RMS residual of every used pair's inliers under the
  solved global layout. Gate: `<= MAX_GLOBAL_RMS_PX`.
- The pair graph must be **connected**. A frame reachable from no other
  frame cannot be placed: fail with `STITCH_UNDERCONSTRAINED`, naming it.
- `rebate_deviation_px` — the user's own idea, and a genuinely independent
  check: the straight edges of the film rebate, mapped into canvas space,
  should be collinear across every frame. **Recorded, and a warning when it
  exceeds `REBATE_DEVIATION_WARN`, but never a hard gate in Phase 2** — it
  depends on the rebate being visible and cleanly detectable, which Chunk
  P2-1 must assess before it could ever be promoted. Write `null` when the
  edges could not be found.
- A strip sanity check: project the placed frame centres onto their
  principal axis; a large perpendicular spread means the solve produced
  something that is not a strip. Emits `STITCH_LAYOUT_UNEXPECTED` as a
  **warning**, not a failure.

**Every threshold named above is set in Chunk P2-1 from measurements on real
scans, and nowhere else.** They live in **section 3.12**, which is empty
until user gate C fills it, and production code reads them from there and
from nowhere else. Do not invent a plausible-looking number and move on; a
threshold calibrated against nothing is worse than no threshold, because it
looks like a guarantee.

### 3.5 Failure, cancellation, and cleanup

- **A negative that cannot be stitched fails alone.** Its intermediates are
  kept, the run continues with the next negative, and the run ends `partial`.
  This mirrors Phase 1's group-failure rule exactly.
- A negative is staged and published atomically, exactly as in Phase 1:
  the stitched TIFF is written into a staging directory inside the output
  folder and moved into place only when it is complete and verified.
- Cancellation stays cooperative via SIGTERM. Check the cancellation token
  between sub-steps and between frames while warping, never mid-`warpAffine`.
  A cancelled negative is **abandoned, not failed**: no `negative_failed`
  event, exactly as Phase 1 treats a cancelled group.
- The work directory is **removed on complete success only**. It is kept —
  and its path reported through an `INTERMEDIATES_KEPT` warning so the app
  can show it — when any negative failed, when the run was cancelled, or
  when `--keep-intermediates` was given.
- Cleanup never deletes a directory the user named with `--work` unless the
  run created it. Deleting a folder the user pointed at is not this
  program's decision to make.

### 3.6 Command surface

Phase 1's two commands keep their exact meanings. Phase 2 adds two.

```text
scanny-boy probe   --input DIR [--files FILE ...] [--per-negative N] [--out DIR]
scanny-boy convert --input DIR --files FILE ... --out DIR --film-date YYYY-MM-DD
                   [--per-negative N] [--jobs N] [--overwrite]

scanny-boy stitch  --work DIR --out DIR
                   [--jobs N] [--overwrite] [--allow-partial]

scanny-boy run     --input DIR --files FILE ... --out DIR --film-date YYYY-MM-DD
                   [--per-negative N] [--jobs N] [--overwrite]
                   [--work DIR] [--keep-intermediates]
```

- **`run` is the app's normal path.** It creates a work directory, runs the
  conversion stage into it by calling Phase 1's `run_convert` in-process,
  runs the stitch stage into `--out`, and cleans up. One process, one event
  stream, one cancellation. It never spawns a subprocess of itself.
- **`stitch` is the re-stitch path.** It reads the Phase 1 manifest in
  `--work`, verifies every intermediate's size and SHA-256, and stitches. It
  is what makes tuning possible without paying for RAW decoding again.
- `--work` without a value means a fresh directory under the system
  temporary location. Given a value, that directory is used and is never
  deleted by cleanup (section 3.5).
- `--work` and `--out` must resolve to different directories
  (`WORK_SAME_AS_OUTPUT`), and `--out` must still differ from `--input`
  (`OUTPUT_SAME_AS_INPUT`, unchanged).
- `--jobs` keeps its Phase 1 meaning for the conversion stage. In the stitch
  stage it bounds **feature detection only**, which is cheap and per-frame.
  Compositing is always one negative at a time and single-threaded through
  the accumulator; section 2.4's memory figures assume exactly that.

**Amendment to Phase 1 section 3.7.** That section says "Phase 2 must reject
a manifest that is not `complete`." Taken literally, one failed negative in
the conversion stage would throw away every good one. `stitch` therefore
accepts a `complete` manifest by default, and accepts a `partial` one under
`--allow-partial`, stitching only the groups that manifest marks
`completed`. `run` passes `--allow-partial` semantics implicitly, because it
has just produced the manifest itself and already knows which groups
succeeded. A `running` or `cancelled` manifest is still rejected. Every
other guarantee of section 3.7 — missing output, wrong size, wrong SHA-256 —
is enforced exactly as written.

### 3.7 Output folder, naming, and the roll manifest

- One output folder holds one stitched roll.
- **Each negative's TIFF is named after the first frame of its group**, by
  canonical order: the negative shot as `_DSC4638`, `_DSC4639`, `_DSC4640`
  becomes `_DSC4638.tif`. There is no collision with the intermediate of the
  same name because intermediates live in the work directory, which is
  always a different directory (section 3.6).
- The output folder's record is **`scanny-boy-roll.json`**, a new file with
  its own schema at `shared/contract/roll-manifest.schema.json`.
  Phase 1's `scanny-boy-manifest.json` is **not renamed and not changed**;
  it simply now lives in the work directory. This answers `punchlist.md`'s
  "should we rename the manifest?" — no, but the file the user actually
  keeps gets a name of its own.
- The roll manifest must carry enough that the output folder is
  self-describing without the work directory: the sources and their hashes,
  the film date, the conversion parameters and ICC hash copied from the work
  manifest, the stitch parameters and every threshold in force, and per
  negative its members, solved layout, every section 3.4 metric, canvas
  size, valid rectangle, fill colour, and output hash.
- Output-folder rules are Phase 1's rules, applied to the roll manifest:
  empty is valid; nonempty needs a valid roll manifest; dot-files are always
  ignored; a rerun must match or it is `MANIFEST_MISMATCH`; conflicts need an
  explicit `--overwrite` that the app passes only after the user confirms.
  Implement this by **generalising `output_folder.py` over which manifest it
  is reading**, not by copying it.
- **The canvas is the full union bounding box**, with `FILL_COLOR` in the
  gaps. Nothing captured is discarded.
- **The valid rectangle is computed and recorded, never applied.** It is
  what lets Phase 3's crop tool ("Crop based on manifest data" in
  `punchlist.md`) work without re-deriving geometry from pixels.
- **Guards on size**, from section 2.4:
  - Any canvas dimension above **30,000 px** emits an
    `OUTPUT_DIMENSIONS_LARGE` warning naming the dimension. Most consumer
    editors will not open the file; the user should know before they build a
    roll of them.
  - An estimated file above **3.5 GiB** fails the negative with
    `STITCH_OUTPUT_TOO_LARGE` rather than writing something a classic TIFF
    cannot address. Do not silently switch to BigTIFF: its support in the
    applications this user actually opens files with is not established, and
    Phase 1's rule against writing subtly-unreadable files applies here too.
  - Composite peak memory is estimated from the solved canvas **before any
    allocation** and checked against the section 3.8 budget, failing with
    `INSUFFICIENT_MEMORY`.

### 3.8 Disk and memory arithmetic

Composite peak memory, checked before allocating anything:

```text
C          = canvas_width × canvas_height
accum      = C × 3 × 4                  # float32 RGB weighted sum
weight     = C × 4                      # float32 weight sum
result     = C × 3 × 2                  # uint16 encoded output
frame      = bbox_width × bbox_height × 3 × 4    # one warped frame
mask       = bbox_width × bbox_height × 4
peak_bytes = accum + weight + result + frame + mask
```

`peak_bytes` must not exceed **half of physical RAM**, the same ceiling
Phase 1's section 3.8 applies to its worker budget. Over that, fail with
`INSUFFICIENT_MEMORY` reporting both numbers.

Free space for the stitch stage, in the style of Phase 1's section 3.9:

```text
S        = ceil(canvas_width × canvas_height × 3 × 2 × 1.05)
N        = negatives whose output does not already exist
D        = max(1 MiB, estimated roll-manifest size)
required = ceil((N × S + S + D) × 1.20)
```

`S` again assumes compression saves nothing. The lone extra `S` covers the
one staged file held alongside the finished ones.

For `run`, the work directory and the output folder may be on different
volumes — the work directory is normally under `TMPDIR`. **Check each volume
separately**: Phase 1's existing formula against the work volume, the
formula above against the output volume. Do not add them together and check
once.

### 3.9 Event protocol

`PROTOCOL_VERSION` goes to **2**, on both sides, in Chunk P2-0.

This is deliberate even though the app embeds its own helper. The one way
these can disagree in practice is a developer forgetting to re-run
`scripts/build-cli.sh` after changing Python — and Phase 1 already made that
a required, easy-to-forget step. A version bump turns that mistake into an
immediate, obvious failure instead of strange behaviour. Swift already
rejects any version it does not recognise, and `CLIEventTests` already has a
test asserting that; both need updating together.

Changes to the stream:

- `progress` gains **`stage`**: `"convert"` or `"stitch"`. It defaults to
  `"convert"` so Phase 1's emitters need no edit beyond passing the field.
- `PipelineStep` gains the stitch steps: `load`, `detect`, `match`, `solve`,
  `warp`, `blend`, `write_stitched`.
- Two new events: **`negative_done`** (a stitched TIFF was published;
  carries `negative_id`, `output`, and the section 3.4 metrics) and
  **`negative_failed`** (carries `negative_id`, `code`, `message`).
- `item_done`, `group_done`, and `group_failed` keep their exact Phase 1
  meanings and now describe the conversion stage's work in the work
  directory. Do not overload them for stitched output.
- For `run`, `progress.total` spans both stages. The conversion stage is 3
  units per frame, as today. The stitch stage's weight per frame is a
  **constant set from Chunk P2-1's timing measurements**, with the measured
  seconds recorded in a comment next to it. A coarse but honest weighting is
  the goal; a smooth-looking bar built on a guess is not.

### 3.10 New stable codes

Errors:

| Code | Meaning |
| --- | --- |
| `WORK_SAME_AS_OUTPUT` | `--work` resolves to `--out` |
| `WORK_MANIFEST_UNUSABLE` | Work manifest is `running`/`cancelled`, or `partial` without `--allow-partial` |
| `INTERMEDIATE_MISSING` | An intermediate named by the work manifest is absent |
| `INTERMEDIATE_CHANGED` | An intermediate's size or SHA-256 differs from the work manifest |
| `STITCH_INSUFFICIENT_MATCHES` | A pair fell below the inlier count or ratio gate |
| `STITCH_UNDERCONSTRAINED` | The pair graph is disconnected; a frame cannot be placed |
| `STITCH_RESIDUAL_TOO_HIGH` | A section 3.4 residual or overlap gate was exceeded |
| `STITCH_OUTPUT_TOO_LARGE` | Estimated stitched file exceeds 3.5 GiB |
| `STITCH_FAILED` | Any other failure while stitching one negative |

Warnings:

| Code | Meaning |
| --- | --- |
| `STITCH_SCALE_DRIFT` | Similarity fit's scale left `SCALE_DRIFT_WARN` |
| `STITCH_LAYOUT_UNEXPECTED` | Solved layout is not strip-shaped |
| `STITCH_REBATE_CHECK_FAILED` | Rebate edges not collinear, or not found |
| `OUTPUT_DIMENSIONS_LARGE` | A canvas dimension exceeds 30,000 px |
| `INTERMEDIATES_KEPT` | Work directory retained; carries its path |

Existing codes keep their meanings and are reused rather than duplicated:
`OUTPUT_SAME_AS_INPUT`, `OUTPUT_NOT_WRITABLE`, `OUTPUT_NOT_EMPTY`,
`OUTPUT_CONFLICT`, `INSUFFICIENT_DISK`, `INSUFFICIENT_MEMORY`,
`BAD_MANIFEST`, `MANIFEST_MISMATCH`, `ICC_PROFILE_INVALID`,
`TIFF_WRITE_FAILED`, `CANCELLED`.

### 3.11 Stitched TIFF format

Identical to Phase 1's section 3.4 and 3.5 rules, with three differences:

- Dimensions are the canvas, not a frame.
- `ImageDescription` names the negative's sources and says it is stitched —
  `"_DSC4638.NEF+2: stitched scan"` rather than Phase 1's
  `"<source>: unstitched scan frame"`. The exact format is Chunk P2-5's to
  fix and to test, but it must name the first source and the frame count.
- Curated EXIF comes from the negative's **first frame in canonical order**.
  Phase 1 already proved every frame of a negative carries identical
  exposure, aperture, ISO, focal length, lens, and white balance — that is
  what `CAPTURE_SETTINGS_DIFFER` enforces — so there is nothing to
  reconcile. The synthetic `DateTimeOriginal` is the one Phase 1 computed
  for that first frame.

Everything else is unchanged and must be re-asserted in tests: three-channel
`uint16`, ROMM, the embedded checksum-verified ICC profile, `Orientation`
always 1, Deflate with horizontal prediction, compression code `32946`,
`metadata=None`, `description=`/`software=`/`iccprofile=` as keyword
arguments, and the two-pass `tifftools` write for the nested EXIF directory.

### 3.12 Measured constants

**Empty until user gate C.** Chunk P2-1 measures these; the user approves
them; they are written here and become locked like everything else in
section 3. Every one of them lives in exactly one Python module constant,
named exactly as below, and no other module redefines it.

| Constant | Module | Value | Justified by |
| --- | --- | --- | --- |
| `DETECTION_LONG_EDGE` | `detection.py` | *unset* | appendix B table 2 |
| `USE_CLAHE` | `detection.py` | *unset* | appendix B table 2 |
| `DETECTOR` | `registration.py` | *unset* | appendix B table 1 |
| `RATIO_TEST` | `registration.py` | *unset* | appendix B table 1 |
| `RANSAC_REPROJ_PX` | `registration.py` | *unset* | appendix B table 1 |
| `MIN_PAIR_INLIERS` | `registration.py` | *unset* | appendix B table 1 |
| `MIN_PAIR_INLIER_RATIO` | `registration.py` | *unset* | appendix B table 1 |
| `MAX_PAIR_RMS_PX` | `registration.py` | *unset* | appendix B table 1 |
| `SCALE_DRIFT_WARN` | `registration.py` | *unset* | appendix B table 3 |
| `SCALE_DRIFT_FAIL` | `registration.py` | *unset* | appendix B table 3 |
| `MAX_OVERLAP_MAD` | `composite.py` | *unset* | appendix B table 5 |
| `INTERPOLATION` | `composite.py` | *unset* | appendix B table 4 |
| `MAX_GLOBAL_RMS_PX` | `layout.py` | *unset* | appendix B table 1 |
| `STRIP_SPREAD_RATIO` | `layout.py` | *unset* | appendix B table 7 |
| `REBATE_DEVIATION_WARN` | `layout.py` | *unset* | appendix B table 6 |
| `STITCH_UNITS_PER_FRAME` | `run_pipeline.py` | *unset* | appendix B table 7 |
| `STITCH_UNITS_PER_NEGATIVE` | `run_pipeline.py` | *unset* | appendix B table 7 |

`RANSAC_REPROJ_PX` and `MAX_PAIR_RMS_PX` are **full-resolution pixels**, not
detection-image pixels — Chunk P2-3 converts matched points to full
resolution before RANSAC precisely so that every number downstream shares
one unit.

## 4. Dependency and build rules

### 4.1 Python

- Add exactly one runtime dependency:
  **`opencv-python-headless>=4.14,<5`**.
  - `headless`, not the plain wheel: no Qt, no GTK, no FFmpeg, nothing that
    needs system libraries. This is also what keeps the Ubuntu CI job working
    without installing `libGL`.
  - Pinned below 5 for the reason measured in section 2.1: OpenCV 5.0 has no
    AKAZE, and AKAZE is a live candidate until Chunk P2-1 decides otherwise.
    If Chunk P2-1 selects SIFT or ORB, moving to the 5.x line becomes a
    considered follow-up, not something to do in passing.
- Add **no other runtime dependency**. In particular **do not add SciPy.**
  The global layout solve of section 3.2 is deliberately formulated as two
  linear least-squares problems precisely so `numpy.linalg.lstsq` is
  sufficient (Chunk P2-4). SciPy is a large wheel and a packaging risk for
  a solve that does not need it.
- Development dependencies are unchanged.
- Regenerate `uv.lock` with `uv lock` and commit it.
- Record OpenCV's Apache-2.0 licence in `THIRD_PARTY_NOTICES.md`.

### 4.2 Packaged program

Phase 1's section 5.2 rules all still apply. OpenCV adds one known risk:

- PyInstaller ships a `cv2` hook, but OpenCV's Python layer performs its own
  dynamic loading at import time, and the failure mode is an `ImportError`
  **only in the frozen bundle**, never in development. Phase 1 already
  learned this lesson with LibRaw and answered it the right way: its
  packaged check performs a **real conversion**, not an import.
- **Chunk P2-8 must therefore extend `packaging_test.py` to perform a real
  packaged `run` on the sample NEFs, ending in a real stitched TIFF.** An
  import check, a `--version` check, or a conversion-only check does not
  discharge this. Record the resulting bundle size in the PR.
- If the frozen bundle cannot find `cv2`, the fix is `--collect-all cv2` in
  the spec file, alongside the two fixes Phase 1's section 5.2 already
  documents. Add it as a third documented fix with a comment saying why,
  rather than as an unexplained flag.

### 4.3 Swift

Unchanged from Phase 1's section 5.3. `CLISession`, `CLIRunner`,
`LineAssembler`, and `JSONValue` need no structural change; Phase 2 adds
event kinds, command builders, and views on top of them.


## 5. Chunks

Twelve chunks and three user gates. One branch and one pull request each,
merged in order, using `docs/PHASE2_CHUNK_PROMPT.md`.

**These entries are written to be executed, not interpreted.** Every new
module, every public signature, every constant name, and every test name is
given. An agent that finds itself designing an API, choosing a data shape, or
picking a threshold has left the plan and must stop and report — see section
5.1.

Each entry carries a **Model** line recommending which Claude model to run it
with. Section 5.2 explains the reasoning; the summary table lives in
`docs/PHASE2_CHUNK_PROMPT.md`.

### 5.1 The rule that makes this plan safe to execute

If a chunk requires a decision this plan does not already make, **stop and
report — do not decide.** Concretely, stop if you need to:

- name a module, class, function, field, event, or error code that is not
  written down here;
- choose a numeric threshold, tolerance, or magic number that is not written
  down here or in appendix B;
- change a signature this plan gives;
- modify Phase 1 code beyond the two changes sections 5 names explicitly;
- work around a failing test by relaxing an assertion.

Report what you needed, what you would have chosen, and why. A stopped chunk
costs one message. A chunk that invented an API costs a rewrite of everything
built on top of it.

### 5.2 Choosing a model per chunk

Most of Phase 2 is mechanical once specified, and this plan specifies it, so
**most chunks run well on Sonnet 5.** The exceptions are the chunks where the
cost of a wrong call is paid by every later chunk, or where the work is
diagnosing opaque failures rather than writing described code.

- **Sonnet 5** — P2-0, P2-2, P2-3, P2-4, P2-5, P2-7, P2-9, P2-10, P2-11.
  Described modules, given signatures, given tests.
- **Opus 5** — P2-1 and P2-6. P2-1 sets every threshold the rest of the plan
  depends on and must recognise when a measurement invalidates the design;
  P2-6 refactors shared Phase 1 code that Phase 1's own tests must keep
  passing, which is the highest regression risk in Phase 2.
- **Start Sonnet 5, escalate to Opus 5 on failure** — P2-8. Packaging is
  ordinary until PyInstaller produces an opaque error, at which point it
  becomes debugging with poor signal.
- **Haiku 4.5 is not recommended for any Phase 2 chunk.** Even the mechanical
  ones touch numerically sensitive code where a plausible-looking wrong line
  passes review.

---

### User gate B — sample scans for stitching — **BLOCKING Chunk P2-1**

The six NEFs at `tests/fixtures/nef/` were captured to test RAW conversion.
Nothing is known about whether they overlap usefully, whether they contain
rotation, or whether they are in spatial order. Chunk P2-1 cannot calibrate a
single threshold without scans that represent real practice.

**The user supplies, at `tests/fixtures/nef/`, following the same camera
rules Phase 1 requires (lossless-compressed NEF, fixed manual exposure, fixed
manual white balance, one lens, one focal length, one orientation):**

1. **A routine negative** — however many shots per negative is actually
   normal, shot the way a real roll is shot. This calibrates the thresholds. (These are named normal_1.nef, normal_2.nef, normal_3.nef)
2. **A deliberately rotated negative** — the same negative re-shot with
   several degrees of rotation deliberately introduced between frames. This
   proves the rigid model earns its keep. (the wonky_n.nef series)
3. **A reverse-order negative** — shot right-to-left, or with the middle
   frame captured last. This is the only thing that proves section 3.2's
   order-independence claim on real data. (order_n.nef series)
4. **A tight-overlap negative** — at the user's minimum, around 20%. This
   sets the lower bound the gates must still accept. (tight_n.nef series)
5. **A negative that should fail** — two frames that genuinely do not
   overlap, or a badly out-of-focus frame. Nothing else proves the gates
   reject anything; a pipeline that has never refused a negative has not been
   shown to have working gates. (mismatch_n.nef series)

At least one frame of each must include a **visible film rebate edge**, or
section 3.4's rebate check cannot be assessed at all.

These files stay out of Git — `.gitignore` already excludes
`tests/fixtures/nef/`, the repository is public, and the existing six are
already about 190 MB. Never `git add` them. Record their hashes, groupings,
and settings in `tests/fixtures/INVENTORY.md` locally and in this plan's
**appendix C**, exactly as Phase 1's appendix A does.

**An agent may not proceed past this gate by synthesising substitutes.** If
the files are absent, stop and say so.

---

### Chunk P2-0 — Dependency, contract, and protocol v2

Branch: `p2-chunk-00-contract` · **Model: Sonnet 5**

**Files to change:**

| File | Change |
| --- | --- |
| `cli/pyproject.toml` | add `"opencv-python-headless>=4.14,<5"` to `dependencies` |
| `cli/uv.lock` | regenerate with `uv lock` |
| `THIRD_PARTY_NOTICES.md` | add OpenCV, Apache-2.0 |
| `cli/src/scanny_boy/events.py` | protocol bump, new members, two new events |
| `cli/src/scanny_boy/events_test.py` | cover the additions |
| `shared/contract/schema.json` | new commands, events, fields, codes |
| `shared/contract/roll-manifest.schema.json` | **new file** |
| `shared/contract/CONTRACT.md` | document all of the above |
| `cli/src/scanny_boy/roll_manifest_schema_test_support.py` | **new file** |
| `mac/ScannyBoy/CLIBridge/CLIEvent.swift` | version 2, two new kinds, `stage` |
| `mac/ScannyBoyTests/CLIEventTests.swift` | update every literal |

**Exact edits to `events.py`:**

```python
PROTOCOL_VERSION = 2

class EventType(enum.StrEnum):
    ...                                   # existing members unchanged
    NEGATIVE_DONE = "negative_done"
    NEGATIVE_FAILED = "negative_failed"

class Stage(enum.StrEnum):
    CONVERT = "convert"
    STITCH = "stitch"

class PipelineStep(enum.StrEnum):
    DECODE = "decode"                     # existing three unchanged
    WRITE_TIFF = "write_tiff"
    ADD_METADATA = "add_metadata"
    LOAD = "load"                         # seven new
    DETECT = "detect"
    MATCH = "match"
    SOLVE = "solve"
    WARP = "warp"
    BLEND = "blend"
    WRITE_STITCHED = "write_stitched"

class Code(enum.StrEnum):
    ...                                   # existing members unchanged
    WORK_SAME_AS_OUTPUT = "WORK_SAME_AS_OUTPUT"
    WORK_MANIFEST_UNUSABLE = "WORK_MANIFEST_UNUSABLE"
    INTERMEDIATE_MISSING = "INTERMEDIATE_MISSING"
    INTERMEDIATE_CHANGED = "INTERMEDIATE_CHANGED"
    STITCH_INSUFFICIENT_MATCHES = "STITCH_INSUFFICIENT_MATCHES"
    STITCH_UNDERCONSTRAINED = "STITCH_UNDERCONSTRAINED"
    STITCH_RESIDUAL_TOO_HIGH = "STITCH_RESIDUAL_TOO_HIGH"
    STITCH_OUTPUT_TOO_LARGE = "STITCH_OUTPUT_TOO_LARGE"
    STITCH_FAILED = "STITCH_FAILED"
    STITCH_SCALE_DRIFT = "STITCH_SCALE_DRIFT"
    STITCH_LAYOUT_UNEXPECTED = "STITCH_LAYOUT_UNEXPECTED"
    STITCH_REBATE_CHECK_FAILED = "STITCH_REBATE_CHECK_FAILED"
    OUTPUT_DIMENSIONS_LARGE = "OUTPUT_DIMENSIONS_LARGE"
    INTERMEDIATES_KEPT = "INTERMEDIATES_KEPT"
```

`Progress` gains one field, placed last so existing positional construction
is unaffected:

```python
stage: Stage = Stage.CONVERT
```

Two new events, following the existing dataclass style exactly:

```python
@dataclasses.dataclass(frozen=True, kw_only=True)
class NegativeDone(Event):
    event_type: ClassVar[EventType] = EventType.NEGATIVE_DONE

    negative_id: str
    output: str
    width: int
    height: int
    global_rms_px: float
    max_overlap_mad: float

@dataclasses.dataclass(frozen=True, kw_only=True)
class NegativeFailed(Event):
    event_type: ClassVar[EventType] = EventType.NEGATIVE_FAILED

    negative_id: str
    code: Code
    message: str
```

**`roll-manifest.schema.json` — required top-level properties**, mirroring
the style of `manifest.schema.json`:

`manifest_format_version` (const 1), `manifest_kind` (const `"stitch"`),
`scanny_boy_version`, `run_id`, `status` (`running`/`partial`/`cancelled`/
`complete`), `input_folder`, `film_date`, `shots_per_negative`,
`convert_run_id`, `processing_params`, `icc_profile`, `stitch_params`,
`source_order`, `sources`, `negatives`, `started_at`, `finished_at`.

Each entry of `negatives` requires: `negative_id`, `members`,
`expected_output`, `status` (`pending`/`completed`/`failed`), `output`
(nullable, `{name, size, sha256, width, height}`), `frames`, `pairs`,
`global_rms_px` (nullable), `canvas` (`{width, height}`, nullable),
`valid_rect` (nullable `[x, y, width, height]`), `fill_color`
(`[r, g, b]`), `rebate_deviation_px` (nullable), `error_code` (nullable),
`error_message` (nullable).

Each entry of `frames` requires: `name`, `rotation_deg`, `translation`
(`[x, y]`).

Each entry of `pairs` requires: `a`, `b`, `inliers`, `good_matches`,
`inlier_ratio`, `rms_residual_px`, `scale_drift`, `overlap_fraction`
(nullable), `overlap_mad` (nullable), `accepted`.

**Swift edits:** `supportedProtocolVersion = 2`; add `.negativeDone` and
`.negativeFailed` to `CLIEvent.Kind` with their `name` cases; add a
`stage: String?` accessor reading `fields["stage"]`.

**Do not:** implement any stitching behaviour, add any other dependency, or
touch `pipeline.py`.

**Tests:**

- `events_test.py::test_protocol_version_is_two`
- `events_test.py::test_negative_done_round_trips`
- `events_test.py::test_negative_failed_round_trips`
- `events_test.py::test_progress_defaults_to_convert_stage`
- `events_test.py::test_progress_carries_stitch_stage`
- new `opencv_availability_test.py::test_opencv_version_and_symbols` —
  asserts `cv2.__version__` starts with `"4."` and that `SIFT_create`,
  `ORB_create`, `AKAZE_create`, `estimateAffinePartial2D`,
  `distanceTransform`, `warpAffine`, and `createCLAHE` all exist. This
  pins section 2.1 where it will keep being checked.
- `roll_manifest_schema_test_support.py` reads the new schema's enums and
  required-field lists out of the file itself, exactly as
  `manifest_schema_test_support.py` does.
- Swift: a v2 line decodes; a v1 line is rejected; `negative_done` and
  `negative_failed` decode with all their fields.

**Verify — paste the output of each:**

```bash
cd cli && uv run ruff check . && uv run pytest
```

```bash
cd cli && uv run python -c "import cv2; print(cv2.__version__, hasattr(cv2,'AKAZE_create'))"
```

```bash
cd mac && xcodebuild test -scheme ScannyBoy -destination 'platform=macOS'
```

---

### Chunk P2-1 — Registration spike and measurement — **produces user gate C**

Branch: `p2-chunk-01-spike` · **Model: Opus 5**

This chunk writes **no production code under `cli/src/`**. Its deliverable is
numbers, and a recommendation the user approves at gate C.

**Do:** write `scripts/measure-registration.py`, modelled on the existing
`scripts/measure-concurrency.py`. It takes `--nef-dir` (default
`tests/fixtures/nef/`) and `--out` for a scratch directory, converts each
gate-B negative to intermediates once by calling `run_convert`, and then
prints the seven tables below as GitHub-flavoured Markdown on stdout.

**Table 1 — detector comparison.** Columns: `negative`, `pair`, `detector`
(SIFT/ORB/AKAZE), `keypoints_a`, `keypoints_b`, `good_matches`, `inliers`,
`inlier_ratio`, `rigid_rms_px` (full-resolution), `seconds`.

**Table 2 — detection-image preparation.** Best detector from table 1, as a
grid over `long_edge` in {1200, 2000, 3000} × CLAHE in {on, off}. Columns:
`long_edge`, `clahe`, `median_inliers`, `median_inlier_ratio`,
`median_rms_px`, `seconds_per_frame`.

**Table 3 — scale drift.** Every pair of every negative: `negative`, `pair`,
`scale`, `abs(scale - 1)`. Report min, median, max, and the 99th percentile.

**Table 4 — interpolation.** `INTER_LANCZOS4` vs `INTER_CUBIC`. Columns:
`interpolation`, `overlap_mad`, `min_value_after_warp`, `seconds`.

**Table 5 — overlap MAD separation.** Every pair, marked `good` or
`should_fail` from the gate-B labelling. Report the distribution of each and
**the gap between the worst good pair and the best bad pair.** If those
distributions overlap, say so plainly — `MAX_OVERLAP_MAD` cannot then be set,
and that is a finding, not a failure to report.

**Table 6 — rebate edges.** Per frame: whether a long straight edge was found
(Canny + `HoughLinesP`), its angle, and after a good solve, the maximum
perpendicular deviation between frames' rebate lines in canvas pixels.

**Table 7 — cost.** Per negative: `frames`, `canvas_width`, `canvas_height`,
`detect_seconds`, `match_seconds`, `solve_seconds`, `warp_seconds`,
`blend_seconds`, `write_seconds`, `total_seconds`, `peak_rss_mib`. Measure
peak RSS the same way `measure-concurrency.py` does.

**The ROMM check, which is a gate and not a table.** Decode one real
intermediate with the section 2.3 formulas and re-encode it. Report the
maximum absolute difference in LSB and the count of changed pixels.
**If the maximum is not 0, stop the chunk and report.** rawpy's gamma is then
not the ROMM curve, section 3.3 rests on a false premise, and the user must
decide before anything downstream is built.

**Also produce:** one stitched negative rendered to a file the user can open,
using the best settings found. This is what they look at.

**Then propose**, in the PR body, a concrete value for each of
`DETECTION_LONG_EDGE`, `DETECTOR`, `USE_CLAHE`, `RATIO_TEST`,
`RANSAC_REPROJ_PX`, `MIN_PAIR_INLIERS`, `MIN_PAIR_INLIER_RATIO`,
`MAX_PAIR_RMS_PX`, `SCALE_DRIFT_WARN`, `SCALE_DRIFT_FAIL`,
`MAX_OVERLAP_MAD`, `MAX_GLOBAL_RMS_PX`, `REBATE_DEVIATION_WARN`,
`STRIP_SPREAD_RATIO`, `INTERPOLATION`, `STITCH_UNITS_PER_FRAME`, and
`STITCH_UNITS_PER_NEGATIVE` — each with the table row that justifies it and
the safety margin chosen. State explicitly which ones the data does **not**
support choosing.

**Write** the tables into this plan as appendix B and the sample-scan facts
as appendix C.

**Do not:** write anything under `cli/src/scanny_boy/`, and do not merge
threshold values into production code. That is Chunk P2-2 onward, after gate
C.

**Tests:** the script runs to completion on the sample files, and skips
clearly — saying what was not measured — when they are absent.

**Verify:** paste every table in full into the PR.

**Stop at user gate C.**

---

### User gate C — approve the measured constants — **BLOCKING Chunk P2-2**

The user reads Chunk P2-1's report, opens the rendered stitch, and approves
or changes each proposed constant. Approved values are written into a new
**section 3.12** of this plan, which then becomes locked in the ordinary way.
Chunks P2-2 onward read them from there and from nowhere else.

---

### Chunk P2-2 — Colour transfer and detection images

Branch: `p2-chunk-02-color-detection` · **Model: Sonnet 5**

**New files:** `cli/src/scanny_boy/romm.py`, `romm_test.py`,
`detection.py`, `detection_test.py`. Change nothing else.

**`romm.py` — exact contents:**

```python
ROMM_GAMMA = 1.8
ROMM_SLOPE = 16.0
ENCODED_BREAKPOINT = 0.03125        # in the ENCODED domain
LINEAR_BREAKPOINT = 0.001953125     # in the LINEAR domain
MAX_CODE = 65535

DECODE_LUT: np.ndarray              # (65536,) float32, built once at import

def decode_to_linear(image: np.ndarray) -> np.ndarray:
    """uint16 array of any shape -> float32 linear, via DECODE_LUT indexing.
    Never calls numpy.power per pixel."""

def encode_from_linear(image: np.ndarray) -> np.ndarray:
    """float32 linear -> uint16 ROMM. Clamps to [0, 1] first, then rounds
    with numpy.rint."""
```

The two curves, each using the breakpoint in its own domain — swapping them
is the mistake Phase 1's section 3.4 documents:

```text
decode:  L = E / 16.0          if E <  0.03125       else E ** 1.8
encode:  E = L * 16.0          if L <  0.001953125   else L ** (1 / 1.8)
```

**`detection.py` — exact contents:**

```python
@dataclasses.dataclass(frozen=True)
class DetectionImage:
    image: np.ndarray                  # uint8, (h, w)
    scale: float                       # detection px * scale = full-res px
    source_size: tuple[int, int]       # (height, width) at full resolution

def build_detection_image(frame: np.ndarray, *, long_edge: int, clahe: bool) -> DetectionImage:
    """frame is uint16 (H, W, 3) as read from an intermediate TIFF.

    1. Decode to linear with romm.decode_to_linear.
    2. Luminance: 0.2126 R + 0.7152 G + 0.0722 B.
    3. Downscale with cv2.INTER_AREA so the long edge is `long_edge`,
       never upscaling; scale is the exact ratio used.
    4. Scale to 0..255 uint8 by the 0.5th and 99.5th percentiles, clipped.
    5. If `clahe`, apply cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).
    """

def to_full_resolution(points: np.ndarray, scale: float) -> np.ndarray:
    """(N, 2) float detection-space points -> full-resolution points."""
```

**Tests — exact names:**

- `romm_test.py::test_round_trip_is_exact_for_every_code` — all 65,536 codes
  decode and re-encode to themselves. Maximum error 0 LSB.
- `romm_test.py::test_round_trip_is_exact_on_a_real_intermediate` — skips
  clearly without sample NEFs.
- `romm_test.py::test_encoded_breakpoint_is_handled` — codes straddling
  `0.03125 * 65535` decode on the correct branch.
- `romm_test.py::test_linear_breakpoint_is_handled` — linear values
  straddling `0.001953125` encode on the correct branch. **These two exist
  because a test using only mid-tones passes with the breakpoints swapped.**
- `romm_test.py::test_decode_is_monotonic_and_maps_endpoints` — 0 → 0.0,
  65535 → 1.0, and `numpy.diff(DECODE_LUT) >= 0` everywhere.
- `romm_test.py::test_encode_clamps_out_of_range_input` — −0.5 → 0,
  1.5 → 65535. This is the section 2.3 Lanczos undershoot's safety net.
- `detection_test.py::test_long_edge_and_dtype`
- `detection_test.py::test_scale_maps_points_back_within_half_a_pixel`
- `detection_test.py::test_never_upscales_a_small_frame`
- `detection_test.py::test_clahe_flag_changes_the_result`

**Verify:** `cd cli && uv run ruff check . && uv run pytest`

---

### Chunk P2-3 — Pairwise registration

Branch: `p2-chunk-03-pairwise` · **Model: Sonnet 5**

**New files:** `cli/src/scanny_boy/registration.py`, `registration_test.py`,
and `synthetic_scene_support.py` (the shared fixture generator described
below). Change nothing else.

**`synthetic_scene_support.py`** — one generator used by every registration
test and, later, by `scripts/measure-registration.py`. Film-like, never pure
noise (section 6):

```python
def synthetic_scene(height: int, width: int, *, seed: int) -> np.ndarray:
    """float32 in [0, 1]: smooth sinusoidal gradients, ~220 filled circles
    of random radius and value, Gaussian blur sigma 1.4, then Gaussian grain
    at sigma 0.012."""

def cut_frames(scene, *, frame_size, count, overlap, rotations_deg, seed) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Returns the frames and the 2x3 ground-truth placement of each."""
```

**`registration.py` — exact contents:**

```python
@dataclasses.dataclass(frozen=True)
class FrameFeatures:
    name: str
    keypoints: tuple            # cv2.KeyPoint
    descriptors: np.ndarray
    scale: float                # from DetectionImage

@dataclasses.dataclass(frozen=True)
class PairResult:
    a: str
    b: str
    transform: np.ndarray       # 2x3 float64, rigid, maps b -> a, FULL-RES px
    good_matches: int
    inliers: int
    inlier_ratio: float
    rms_residual_px: float      # full-resolution pixels
    scale_drift: float          # abs(similarity scale - 1)
    accepted: bool
    reject_code: Code | None
    reject_message: str | None
    # Filled in later by composite.py; None here.
    overlap_fraction: float | None
    overlap_mad: float | None

class StitchError(Exception):
    def __init__(self, code: Code, message: str) -> None: ...

def detect_features(detection: DetectionImage, name: str) -> FrameFeatures:
    """Uses the gate-C DETECTOR. SIFT/AKAZE -> float descriptors,
    ORB -> uint8."""

def rigid_from_correspondences(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Closed-form Umeyama with scale forced to exactly 1. Returns 2x3.

        mu_s, mu_d = src.mean(0), dst.mean(0)
        S = ((dst - mu_d).T @ (src - mu_s)) / len(src)
        U, _, Vt = np.linalg.svd(S)
        D = np.diag([1.0, np.sign(np.linalg.det(U @ Vt))])
        R = U @ D @ Vt
        t = mu_d - R @ mu_s
    """

def register_pair(a: FrameFeatures, b: FrameFeatures) -> PairResult:
    """1. BFMatcher — NORM_HAMMING for uint8 descriptors, NORM_L2 otherwise.
       2. knnMatch(k=2) plus Lowe ratio test at RATIO_TEST.
       3. Convert both point sets to full resolution with
          detection.to_full_resolution BEFORE anything else, so every number
          from here on is in full-resolution pixels — including
          RANSAC_REPROJ_PX.
       4. cv2.estimateAffinePartial2D(..., method=cv2.RANSAC,
          ransacReprojThreshold=RANSAC_REPROJ_PX, maxIters=5000) for the
          inlier mask and the similarity scale.
       5. rigid_from_correspondences on the inliers only. This, not the
          similarity, is the returned transform.
       6. Apply the section 3.4 gates and set accepted/reject_code.
    """
```

**Gate order, so the reported code is the first real reason:** too few good
matches → `STITCH_INSUFFICIENT_MATCHES`; inliers below `MIN_PAIR_INLIERS` or
ratio below `MIN_PAIR_INLIER_RATIO` → `STITCH_INSUFFICIENT_MATCHES`;
`scale_drift` above `SCALE_DRIFT_FAIL` → `STITCH_RESIDUAL_TOO_HIGH`;
`rms_residual_px` above `MAX_PAIR_RMS_PX` → `STITCH_RESIDUAL_TOO_HIGH`.

**Tests — exact names:**

- `test_recovers_a_known_rotation_and_translation` — angle within 0.1°,
  translation within 1 full-resolution px, `scale_drift` under 0.001.
- `test_recovers_across_the_rotation_range` — parametrised over
  0, 1, 2, 3, 5, 8 degrees.
- `test_recovers_at_minimum_overlap` — 20%, the section 3.2 floor.
- `test_reverse_pair_is_the_inverse_transform` — registering (a, b) and
  (b, a) gives transforms whose composition is identity within tolerance.
- `test_non_overlapping_pair_is_rejected` — `accepted is False` and
  `reject_code is Code.STITCH_INSUFFICIENT_MATCHES`.
- `test_rigid_fit_never_returns_a_scale_other_than_one` — asserts the
  returned transform's implied scale is 1.0 within 1e-9. A regression here
  would silently reintroduce an affine fit.
- `test_real_sample_pairs_meet_their_gates` — every gate-B negative's pairs
  are accepted, and the should-fail negative's bad pair is not. Skips
  clearly without sample files.

**Verify:** full suite, plus a pasted table of per-pair metrics for the real
samples.

---

### Chunk P2-4 — Global layout solve

Branch: `p2-chunk-04-layout` · **Model: Sonnet 5**

**New files:** `cli/src/scanny_boy/layout.py`, `layout_test.py`.
Change nothing else.

**The model, written out so it does not have to be derived.** Frame *i* maps
its own pixel `p` into canvas space as `x = R(θ_i)·p + t_i`, where
`R(θ) = [[cos θ, −sin θ], [sin θ, cos θ]]`. A `PairResult` for (a, b) gives
`p_a = R(φ_ab)·p_b + u_ab`. Requiring both routes into canvas space to agree
gives exactly two relations:

```text
θ_b = θ_a + φ_ab
t_b = t_a + R(θ_a) · u_ab
```

**Step 1 — rotations.** These are scalars, so it is one linear
least-squares problem. Build `M` with one row per accepted pair, `−1` in
column `a`, `+1` in column `b`, right-hand side `φ_ab`; append an anchor row
with `1` in column 0 and right-hand side `0`. Solve with
`numpy.linalg.lstsq`. Assert every `|φ_ab| < 45°` first — small angles mean
no wrap-around handling is needed, and an input that violates it is a bug
upstream, not a case to handle.

**Step 2 — translations.** With `θ` known, the second relation is linear in
`t`. Unknowns are `[t_0x, t_0y, t_1x, t_1y, …]`. Each accepted pair
contributes two rows — `t_bx − t_ax = (R(θ_a)·u_ab)_x` and the same in `y` —
and two anchor rows fix `t_0 = (0, 0)`. Solve with `numpy.linalg.lstsq`.

**This two-step formulation is why section 4.1 forbids SciPy. Do not replace
it with a nonlinear bundle adjustment.**

**`layout.py` — exact contents:**

```python
@dataclasses.dataclass(frozen=True)
class FramePlacement:
    name: str
    rotation_deg: float
    translation: tuple[float, float]

    def matrix(self) -> np.ndarray:      # 2x3 float64

@dataclasses.dataclass(frozen=True)
class Layout:
    placements: list[FramePlacement]
    canvas_size: tuple[int, int]         # (width, height)
    global_rms_px: float
    used_pairs: list[PairResult]
    strip_spread_ratio: float

def check_connectivity(names: list[str], pairs: list[PairResult]) -> None:
    """Union-find over accepted pairs. Raises
    StitchError(STITCH_UNDERCONSTRAINED, ...) naming every frame not in the
    component containing names[0]."""

def solve_layout(names, frame_size, pairs) -> Layout:
    """frame_size is (height, width), identical for every frame.
    Steps 1 and 2 above, then:
      - transform each frame's four corners (0,0), (W,0), (W,H), (0,H);
      - take the union bounding box;
      - subtract (min_x, min_y) from every translation so the canvas origin
        is (0, 0);
      - canvas_size = (ceil(max_x - min_x), ceil(max_y - min_y)).
    """

def global_rms(placements, pairs) -> float:
    """For every accepted pair and every one of its inlier correspondences,
    the distance between the two frames' canvas-space predictions of the
    same point. Returns the RMS over all of them."""

def strip_spread_ratio(placements, frame_size) -> float:
    """Placed frame centres, mean-subtracted; the ratio of the second
    singular value to the first. A strip is near 0."""

def largest_valid_rect(layout, frame_size, *, probe_long_edge: int = 2000) -> tuple[int, int, int, int]:
    """Returns (x, y, width, height) in canvas pixels.

    Build a uint8 coverage mask on a canvas downscaled so its long edge is
    at most probe_long_edge, filling each frame's transformed quadrilateral
    with cv2.fillConvexPoly. Find the largest all-covered axis-aligned
    rectangle with the standard histogram-and-stack sweep. Scale the result
    back up, then shrink it by one probe cell on every side, so the
    recorded rectangle is always inside the true valid area and never
    larger than it.
    """
```

**Tests — exact names:**

- `test_recovers_a_known_layout` — N frames at known poses, pairs generated
  from ground truth with small noise; every pose within 1 px and 0.1°.
- `test_shuffled_frame_order_gives_the_same_layout` — the same layout with
  frames presented scrambled and reversed produces the same canvas and the
  same relative poses. **This is the direct test of section 3.2's central
  claim and it is not optional.**
- `test_disconnected_graph_is_rejected` — `StitchError` with
  `STITCH_UNDERCONSTRAINED`, naming the right frames.
- `test_canvas_bounds_match_hand_computed_corners` — one rotated placement,
  corners computed by hand in the test.
- `test_valid_rect_contains_no_uncovered_pixel` — the returned rectangle is
  fully covered, checked against a full-resolution coverage mask.
- `test_valid_rect_is_conservative_not_optimistic` — it is inside the true
  maximal rectangle, never larger.
- `test_l_shaped_layout_reports_a_high_spread_ratio` — solves successfully
  and reports a ratio above `STRIP_SPREAD_RATIO`.
- `test_rejects_an_implausibly_large_pair_rotation` — a `φ_ab` beyond 45°
  raises rather than silently wrapping.

**Verify:** full suite, plus the solved layout for one real negative.

---

### Chunk P2-5 — Compositing and the stitched TIFF

Branch: `p2-chunk-05-composite` · **Model: Sonnet 5**

**New files:** `cli/src/scanny_boy/composite.py`, `composite_test.py`,
`stitched_tiff.py`, `stitched_tiff_test.py`. Change nothing else.

**`composite.py` — exact contents:**

```python
FILL_COLOR: tuple[int, int, int] = (0, 0, 0)   # section 3.3: one constant, one place
MASK_ERODE_PX = 5                              # Lanczos4 support radius 4, plus one
MAX_CANVAS_DIMENSION = 30_000                  # warn above this
MAX_STITCHED_BYTES = int(3.5 * 1024**3)        # fail above this

@dataclasses.dataclass(frozen=True)
class CompositeResult:
    image: np.ndarray                          # uint16 (H, W, 3)
    overlap_mad: dict[tuple[str, str], float]
    overlap_fraction: dict[tuple[str, str], float]
    coverage_fraction: float

def estimate_peak_bytes(canvas_size, frame_bbox_size) -> int:
    """Section 3.8's formula, exactly."""

def check_memory_budget(peak_bytes: int) -> None:
    """Raises StitchError(INSUFFICIENT_MEMORY, ...) reporting both numbers
    when peak_bytes exceeds half of physical RAM."""

def check_output_size(canvas_size, *, on_warning) -> None:
    """OUTPUT_DIMENSIONS_LARGE warning above MAX_CANVAS_DIMENSION;
    StitchError(STITCH_OUTPUT_TOO_LARGE) above MAX_STITCHED_BYTES."""

def composite(layout, load_frame, *, cancel, on_progress) -> CompositeResult:
    """load_frame(name) -> uint16 (H, W, 3). Called once per frame and the
    result released immediately, so the caller controls residency.

    Per frame:
      1. romm.decode_to_linear -> float32.
      2. cv2.warpAffine into the frame's OWN bounding box (not the canvas)
         with INTERPOLATION, BORDER_CONSTANT, borderValue 0.
      3. np.clip(warped, 0.0, None) — section 2.3's measured -0.088
         undershoot.
      4. Warp a ones-mask with INTER_NEAREST; cv2.erode by MASK_ERODE_PX.
      5. weight = cv2.distanceTransform(mask, cv2.DIST_L2, 5).
      6. Accumulate weight * rgb into the canvas accumulator and weight into
         the weight canvas, at the bounding box's offset.
      7. Check `cancel` between frames.

    Then: divide where weight > 0; write FILL_COLOR elsewhere; encode with
    romm.encode_from_linear.

    overlap_mad for a pair is the mean absolute difference between the two
    frames' linear values over their shared valid area, divided by the mean
    level over that area. Compute it while both warped frames are in hand.
    """
```

**`stitched_tiff.py` — exact contents:**

```python
def stitched_image_description(first_source: str, frame_count: int) -> str:
    """f"{first_source}+{frame_count - 1}: stitched scan" — e.g.
    "_DSC4638.NEF+2: stitched scan"."""

def write_stitched_tiff(path, image, *, tags, exif, icc_bytes) -> None:
    """Thin wrapper. Calls tiff_writer.write_base_tiff and
    tiff_exif.finalize_tiff. Do NOT reimplement the two-pass write, the
    extratags rules, or the ICC handling — Phase 1 section 3.4 established
    all four of those and each was independently verified to matter."""
```

Curated EXIF comes from the negative's **first frame in canonical order**;
Phase 1's `CAPTURE_SETTINGS_DIFFER` already guarantees the frames agree, so
there is nothing to reconcile.

**Tests — exact names:**

- `test_reconstructs_a_known_scene` — **the strong one.** Cut one known
  scene into overlapping frames at known rotations, composite, and compare
  against the original over the interior of the valid rectangle. Assert a
  mean absolute error below a tolerance, and put the tolerance's
  justification in a comment: resampling twice is not lossless, and this
  test's job is to catch a wrong transform or a wrong blend, not to demand
  bit equality.
- `test_reconstruction_is_order_independent` — scrambled frame order gives
  an identical result.
- `test_feather_weights_sum_to_one_inside_coverage` — anything else is a
  brightness bug at the seam.
- `test_no_output_value_is_negative_or_clipped_high` — the section 2.3
  undershoot, caught permanently.
- `test_uncovered_pixels_are_exactly_fill_color`
- `test_memory_estimate_rejects_an_impossible_canvas` — before allocating.
- `test_oversized_canvas_warns` and `test_oversized_file_fails` — both with
  a **stubbed canvas size**; never allocate gigabytes in a test.
- `stitched_tiff_test.py::test_matches_every_phase_one_tiff_rule` — three
  channels, `uint16`, compression `32946`, horizontal prediction,
  `Orientation` 1, exactly one `ImageDescription`, the ICC profile present
  and byte-identical to the bundled one, every nested EXIF field readable.
- `stitched_tiff_test.py::test_image_description_names_source_and_count`

**Verify:** full suite. Produce one real stitched negative from the sample
files and record its dimensions, file size, `overlap_mad`, and
`global_rms_px` in the PR.

**This chunk reaches human approval point 3.** Stop and let the user look at
a real stitched negative before the pipeline is wired around it.

---

### Chunk P2-6 — The `stitch` command

Branch: `p2-chunk-06-stitch-command` · **Model: Opus 5**

This is the highest-regression-risk chunk in Phase 2: it refactors shared
Phase 1 code. **Phase 1's `output_folder_test.py`, `manifest_test.py`, and
`pipeline_test.py` must pass completely unmodified afterwards.** If a Phase 1
test needs changing, the refactor is wrong — stop and report.

**New files:** `roll_manifest.py`, `roll_manifest_test.py`,
`stitch_pipeline.py`, `stitch_pipeline_test.py`.
**Changed files:** `output_folder.py`, `cli.py`, and their tests
(additive only).

**The `output_folder.py` refactor, specified so it is not designed twice.**
Introduce a rules object and keep every existing public signature working by
defaulting to the Phase 1 rules:

```python
@dataclasses.dataclass(frozen=True)
class UnitView:
    unit_id: str
    expected_outputs: list[str]
    is_completed: bool

@dataclasses.dataclass(frozen=True)
class FolderRules:
    manifest_filename: str
    load: Callable[[Path], Any]
    run_id_of: Callable[[Any], str]
    units_of: Callable[[Any], list[UnitView]]
    all_expected_outputs_of: Callable[[Any], list[str]]

CONVERT_RULES = FolderRules(...)   # Phase 1 behaviour, byte for byte
ROLL_RULES = FolderRules(...)      # the roll manifest

def plan_rerun(output_dir, candidate, *, rules: FolderRules = CONVERT_RULES) -> RerunPlan
def plan_rerun_preview(output_dir, *, rules: FolderRules = CONVERT_RULES, ...) -> RerunPlan
def apply_recovery_cleanup(output_dir, plan) -> None      # unchanged
```

`_plan_rerun` takes `rules` and reads the manifest filename, the run id, and
the units through it. Nothing else about its logic changes.

**`roll_manifest.py`** mirrors `manifest.py` structurally — same temp-file /
`fsync` / rename discipline, same hand-rolled validator style reading its
enums out of the schema. Dataclasses: `PairRecord`, `FrameRecord`,
`NegativeRecord`, `RollManifest`. Functions: `write_roll_manifest`,
`load_roll_manifest`, `validate_roll_manifest_dict`,
`check_roll_rerun_matches`, `estimate_roll_manifest_size`,
`current_roll_manifest_path`. Constant:
`ROLL_MANIFEST_FILENAME = "scanny-boy-roll.json"`.

**`stitch_pipeline.py` — exact entry point:**

```python
@dataclasses.dataclass(frozen=True)
class StitchOutcome:
    status: str                  # "complete" | "partial" | "cancelled"
    published: list[str]
    failed: list[str]

def run_stitch(
    work_dir: Path,
    out_dir: Path,
    *,
    run_id: str,
    overwrite: bool,
    allow_partial: bool,
    jobs: int | None,
    cancel: CancellationToken,
    emit: EmitFn,
) -> StitchOutcome
```

Order of operations, which is not negotiable because each step protects the
next:

1. `validate_not_same_as_input`-style check for `work_dir` vs `out_dir` →
   `WORK_SAME_AS_OUTPUT`.
2. `validate_writable(out_dir)`.
3. Load the work manifest. Status `running` or `cancelled` →
   `WORK_MANIFEST_UNUSABLE`. Status `partial` without `allow_partial` →
   `WORK_MANIFEST_UNUSABLE`. Only groups the work manifest marks
   `completed` are stitched.
4. For every intermediate of every stitched group: exists →
   `INTERMEDIATE_MISSING`; size and SHA-256 match →
   `INTERMEDIATE_CHANGED`. Phase 1's section 3.7 requires exactly this and
   it is the one guarantee the amendment does not relax.
5. `plan_rerun(out_dir, candidate, rules=ROLL_RULES)`; conflicts without
   `overwrite` → `OUTPUT_CONFLICT`; `apply_recovery_cleanup`.
6. Section 3.8 disk check on the output volume.
7. Write the `running` roll manifest and `fsync` it **before** publishing
   anything, exactly as Phase 1 does.
8. Per negative, in canonical group order: load intermediates, build
   detection images, detect features (bounded by `jobs`), register all
   pairs, `check_connectivity`, `solve_layout`, `check_output_size`,
   `check_memory_budget`, `composite`, apply the section 3.4 gates,
   `largest_valid_rect`, write into the staging directory, verify, publish,
   update the roll manifest.
9. Cancellation checks between every sub-step and between frames. A
   cancelled negative emits **no** `negative_failed` — it was abandoned, not
   failed.

**`cli.py` additions:** a `stitch` subparser with `--work` (required),
`--out` (required), `--jobs`, `--overwrite`, `--allow-partial`.

**Tests — exact names:**

- `test_end_to_end_on_real_samples` — right number of outputs, each named
  after its group's first frame, roll manifest valid against its schema,
  every recorded hash matching the file on disk.
- `test_running_work_manifest_is_rejected`
- `test_partial_work_manifest_needs_allow_partial`
- `test_partial_work_manifest_stitches_completed_groups_only`
- `test_missing_intermediate_is_caught`
- `test_changed_intermediate_is_caught`
- `test_failing_negative_does_not_stop_the_run` — run ends `partial`, the
  other negatives are published, the failed one's staging directory is gone.
- `test_cancellation_keeps_completed_negatives` — no `negative_failed` for
  the abandoned one, manifest `cancelled`, exit status 143.
- `test_work_equal_to_out_is_rejected`
- `test_unrelated_nonempty_output_folder_is_rejected`
- `test_mismatched_roll_manifest_is_rejected`
- `test_conflicting_rerun_needs_overwrite`
- `test_phase_one_output_folder_behaviour_is_unchanged` — the explicit
  regression guard on the refactor.

**Verify:** full suite **with no Phase 1 test file modified** — say so
explicitly in the PR and paste `git diff --stat` to prove it. Paste the roll
manifest from a real run.

---

### Chunk P2-7 — The `run` command and cleanup

Branch: `p2-chunk-07-run-command` · **Model: Sonnet 5**

**New files:** `run_pipeline.py`, `run_pipeline_test.py`.
**Changed files:** `cli.py`, `pipeline.py` (one addition only).

**The single permitted change to `pipeline.py`:** `_ProgressReporter` gains
two optional keyword arguments, `completed_offset: int = 0` and
`total_override: int | None = None`, so `run` can report one span across both
stages. `run_convert` gains matching pass-through keywords. **`convert`'s own
emitted numbers must be identical to today's** — that is what
`pipeline_test.py` passing unmodified proves.

**`run_pipeline.py` — exact entry point:**

```python
def run_full(
    input_dir, files, out_dir, film_date, per_negative,
    *,
    run_id: str,
    work_dir: Path | None,          # None -> tempfile.mkdtemp()
    keep_intermediates: bool,
    overwrite: bool,
    jobs: int | None,
    cancel: CancellationToken,
    emit: EmitFn,
) -> RunOutcome
```

Progress totals: `3 * frame_count + STITCH_UNITS_PER_FRAME * frame_count +
STITCH_UNITS_PER_NEGATIVE * negative_count`, with both constants taken from
gate C and the measured seconds recorded in a comment beside them.

**Cleanup rules, stated as a table so there is nothing to infer:**

| Situation | Work directory |
| --- | --- |
| Complete success, work dir created by this run | removed |
| Complete success, `--work` supplied by the user | **kept** |
| Any negative failed | kept, `INTERMEDIATES_KEPT` warning with the path |
| Run cancelled | kept, `INTERMEDIATES_KEPT` warning with the path |
| `--keep-intermediates` given | kept, `INTERMEDIATES_KEPT` warning with the path |

Deleting a folder the user pointed at is never this program's decision.

**Disk:** section 3.8's **two-volume** check — Phase 1's formula against the
work volume, the stitch formula against the output volume, checked
separately. Do not add them and check once; they are usually different
filesystems.

**`cli.py` additions:** a `run` subparser taking everything `convert` takes
plus `--work` and `--keep-intermediates`.

**Tests — exact names:**

- `test_full_run_leaves_no_work_directory`
- `test_work_directory_survives_a_failed_negative`
- `test_work_directory_survives_cancellation`
- `test_keep_intermediates_keeps_it`
- `test_user_supplied_work_directory_is_never_deleted`
- `test_progress_total_spans_both_stages`
- `test_stage_transitions_exactly_once`
- `test_completed_never_decreases`
- `test_cancellation_during_convert_skips_stitch_entirely`
- `test_insufficient_work_volume_fails_before_converting`
- `test_insufficient_output_volume_fails_before_stitching`

**Verify:** full suite; paste a complete `run` event stream from the real
samples.

---

### Chunk P2-8 — Package and verify the frozen program

Branch: `p2-chunk-08-packaging` · **Model: Sonnet 5, escalate to Opus 5 if
the frozen bundle misbehaves**

**Do:** rebuild the PyInstaller bundle with OpenCV. Add `--collect-all cv2`
to `cli/build/scanny_boy.spec` **only if the bundle actually fails without
it**, and if you add it, add a comment saying which error it fixed — Phase 1
documents its two spec fixes that way and the third should match.

Extend `packaging_test.py` with
`test_packaged_program_runs_a_real_stitch`: the frozen binary performs a
complete `run` on the sample NEFs and the resulting stitched TIFF is opened
and checked. **An import check, a `--version` check, or a conversion-only
check does not discharge this requirement** (section 4.2) — Phase 1 learned
this with LibRaw, and the failure appears only in the frozen bundle.

Confirm `scripts/build-cli.sh` and the Xcode copy-and-sign phase still work.

**Verify — paste all three:** the packaged run's output; `codesign --verify
--strict` exiting 0; and `du -sh cli/dist/ScannyBoyCLI.app` before and after
OpenCV.

---

### Chunk P2-9 — App: the run flow

Branch: `p2-chunk-09-app-run` · **Model: Sonnet 5**

**Changed files:** `CLICommand` (wherever `convert` is built),
`ConfigurationModel.swift`, `RunModel.swift`, `RunSubviews.swift`,
`ContentView.swift`. **New file:** `Model/RollManifest.swift`, alongside the
existing `RunManifest.swift`.

**`RunModel` additions:**

```swift
struct StitchedNegative: Sendable, Hashable {
    let negativeID: String
    let output: String
    let width: Int
    let height: Int
    let globalRMS: Double
    let maxOverlapMAD: Double
}

private(set) var stage: String?                     // "convert" | "stitch"
private(set) var stitchedNegatives: [StitchedNegative] = []
private(set) var failedNegatives: [FailedGroup] = []
private(set) var keptWorkDirectory: String?         // from INTERMEDIATES_KEPT
```

`apply(_ event:)` gains `.negativeDone` and `.negativeFailed` cases and reads
`stage` from `progress`. `completionSummary` counts **negatives**, not
frames. Everything else about `RunModel` — the counts-not-indices rule, the
separation of `cliError` from `streamFailures` — is unchanged and must stay
that way.

**`ConfigurationModel` additions:** a `keepIntermediates: Bool` property, and
output-folder validation against `scanny-boy-roll.json`.

**Views:** the run section shows the stage by name; a per-negative results
list with each negative's output name and quality numbers; failed negatives
with their codes; Reveal in Finder; and, when `keptWorkDirectory` is set, a
button that opens it.

**Tests:** `RunModelTests` for two-stage progress, `negative_done`,
`negative_failed`, `INTERMEDIATES_KEPT`, and the negative-counting summary;
`ConfigurationModelTests` for the toggle and roll-manifest validation;
`RunIntegrationTests` driving the real helper end to end; UI tests for the
new controls.

**Verify:** both suites, plus a screenshot of a completed run.

---

### Chunk P2-10 — App: re-stitch

Branch: `p2-chunk-10-app-restitch` · **Model: Sonnet 5**

**Do:** a Re-stitch action — a menu command and a button — that takes a kept
work directory and an output folder and runs `stitch`, reusing the same
progress and results UI as `run`. This is what makes tuning cost minutes
rather than hours, and it is why the work directory is a first-class concept
rather than a hidden temp path.

**Tests:** `CLICommandTests` for `stitch` invocation building; a full
re-stitch integration test against a work directory produced by a prior
`run`; and the error path when the chosen folder holds no valid work
manifest.

**Verify:** both suites.

---

### Chunk P2-11 — Documentation and v0.2 sign-off

Branch: `p2-chunk-11-documentation` · **Model: Sonnet 5**

**Do:**

- Rewrite `README.md` for what the app now does: negatives in, stitched
  negatives out.
- **Record the blending decision and its alternatives in the README**, per
  section 3.3. The user asked for this explicitly. Cover, briefly: the
  linear feather that was chosen and why locked exposure makes it safe; a
  hard seam at the overlap midline (preserves grain exactly, shows any
  misalignment as a visible line); and a multi-band Laplacian blend (hides
  misalignment best, softens fine grain, much heavier). Frame it as a
  decision worth revisiting with real rolls in hand, not as a closed
  question.
- Update `DECISIONS.md` with Phase 2's locked decisions, including the two
  amendments to the Phase 1 plan (sections 3.6 and 3.7 here).
- Update `CONTRIBUTING.md`, `cli/README.md`, `mac/README.md`, and point
  `docs/CHUNK_PROMPT.md` at Phase 2's own prompt file.
- Move anything Phase 2 turned out not to do into `punchlist.md`.
- Clean-clone check: fresh clone, `bootstrap.sh`, `build-cli.sh`,
  `xcodegen generate`, both suites, one real end-to-end run.

**Verify:** paste the entire clean-clone transcript.

**This chunk reaches human approval point 5.** The user signs off before
`v0.2.0` is tagged.

## 6. Test rules

Phase 1's section 7 applies unchanged. Phase 2 adds:

- **Synthetic scenes must be film-like, never pure noise.** Phase 1 banned
  random-noise fixtures because Deflate cannot compress them; Phase 2 has a
  second reason, which is that feature detectors behave completely
  differently on noise than on photographic content, so a noise fixture
  would prove nothing about registration. Use gradients, blobs, and light
  grain — `scripts/measure-registration.py` and section 2.2's benchmark
  share one generator, and so should the tests.
- **Every registration test needs ground truth.** Generate the fixture by
  applying a known transform, then assert the recovered transform against
  it. "It produced a picture" is not a test.
- **Test the rejections.** Each of `STITCH_INSUFFICIENT_MATCHES`,
  `STITCH_UNDERCONSTRAINED`, `STITCH_RESIDUAL_TOO_HIGH`, and
  `STITCH_OUTPUT_TOO_LARGE` needs a test that actually triggers it.
- Do not assert exact pixel hashes of stitched output. It depends on the
  OpenCV build, exactly as Phase 1 refuses to hard-code a pixel hash that
  depends on the LibRaw build. Compare against ground truth with a stated
  tolerance instead.
- Tests that need real scans skip clearly and say what was not tested, using
  the existing shared helper.
- Do not allocate a full 12-frame canvas in a test. Stub the canvas size for
  the guard tests.

## 7. Human approval points and where to pause

### 7.1 Approval points — hard stops

Implementation halts here until the user acts. Agents may prepare evidence
for these. **Agents may not approve them.**

1. **User gate B** — sample scans, before Chunk P2-1.
2. **User gate C** — the measured constants, after Chunk P2-1.
3. **Visual approval of a real stitched negative**, in Chunk P2-5.
4. **Failure, cancellation, and cleanup behaviour in the finished app**,
   after Chunk P2-10.
5. **Final clean-clone and end-to-end sign-off**, before `v0.2.0`.

### 7.2 Pause points — where reconsidering the plan is worth the time

Distinct from the hard stops above: these are boundaries where **new
information exists that could change the plan**, and where changing it is
still cheap. Running straight through them is allowed and will usually be
fine; the point is that stopping here costs one conversation, and not
stopping costs a rewrite.

**Pause after P2-1 (this one really matters).** It coincides with gate C,
but the reconsideration is broader than approving numbers. Three findings
would change the plan itself rather than fill in a blank:

- the ROMM round trip is not exact on real rawpy output — section 3.3 rests
  on a false premise and the whole colour path needs rethinking;
- no detector clears the gates on real negatives — the fallbacks in section
  8 (rebate edges, phase correlation) come off the shelf, and sections 3.2
  and 3.4 change with them;
- `overlap_mad` does not separate the good negatives from the deliberately
  bad one — the headline quality gate cannot be set, and section 3.4 needs a
  different metric.

**Pause after P2-3.** The first point at which real per-pair numbers exist
inside production code with the gates actually applied. If the gates are
rejecting good pairs or waving through bad ones, retuning here is a
one-line change; retuning after P2-6 means re-verifying everything built on
top.

**Pause after P2-5.** This is approval point 3, and it is the moment the
blend and interpolation choices become visible in a file you can open.
Section 3.3 flags the linear feather as deliberate but provisional —
changing it here costs one chunk, and changing it after P2-9 costs the
pipeline and the app.

**Pause after P2-8.** Packaging is where OpenCV either survives freezing or
does not. If the bundle grows unacceptably or `cv2` cannot be frozen
reliably, the fork in the road is real: ship the app unpackaged for local
use, or drop OpenCV for a numpy-only phase-correlation registration. Both
are viable; neither is a decision to make while halfway through P2-9.

**No pause needed after** P2-0, P2-2, P2-4, P2-6, P2-7, P2-9, or P2-10.
Those chunks are mechanical against a specification, they produce no new
information about whether the design is right, and stopping after them buys
nothing.

## 8. Risks

- **Feature detection on negatives.** Low contrast, an orange mask, and film
  grain are not what these detectors were tuned on. Chunk P2-1 measures
  three detectors and two preparation strategies rather than assuming.
  Mitigation if all three do badly: the rebate edges and the frame borders
  are strong straight features, and phase correlation on the overlap is a
  fallback that needs no features at all — but do not build either until
  measurement says features are insufficient.
- **OpenCV under PyInstaller.** Fails only in the frozen bundle. Answered by
  Chunk P2-8's real packaged stitch (section 4.2).
- **OpenCV 5 dropping AKAZE.** Already measured (section 2.1) and answered
  by the pin. Revisit only if Chunk P2-1 chooses SIFT or ORB.
- **Lanczos undershoot.** Measured at −0.088 (section 2.3). Answered by a
  mandatory clamp and a permanent test.
- **Memory on a large negative.** Section 2.4's table shows 12 frames needs
  8.6 GB. Answered by an up-front estimate and `INSUFFICIENT_MEMORY`, not by
  discovering it during a run.
- **File-size and dimension limits.** A wide negative produces a TIFF most
  editors will not open. Answered by `OUTPUT_DIMENSIONS_LARGE` and
  `STITCH_OUTPUT_TOO_LARGE` rather than by writing it and hoping.
- **rawpy's gamma may not be exactly the ROMM curve.** The LUT round trip is
  proved exact in the abstract (section 2.3) but not yet against real rawpy
  output. Chunk P2-1 checks it and **stops** if it differs.
- **Thresholds calibrated on too little data.** Five negatives is a small
  sample. The roll manifest records every metric on every run, so the
  thresholds can be revisited against accumulated real evidence rather than
  re-derived from memory.

## 9. Primary references

- [OpenCV Python packages and the headless variant](https://pypi.org/project/opencv-python-headless/)
- [`cv2.estimateAffinePartial2D`](https://docs.opencv.org/4.x/d9/d0c/group__calib3d.html)
- [`cv2.distanceTransform`](https://docs.opencv.org/4.x/d7/d1b/group__imgproc__misc.html)
- [SIFT, ORB, and AKAZE in OpenCV](https://docs.opencv.org/4.x/d5/d51/group__features2d__main.html)
- [Umeyama's least-squares estimation of similarity transforms](https://web.stanford.edu/class/cs273/refs/umeyama.pdf)
- [ROMM RGB definition](https://registry.color.org/rgb-registry/rommrgb)
- [Phase 1 plan](IMPLEMENTATION_PLAN.md) and [Phase 1 decisions](DECISIONS.md)

## 10. Agent handoff

Use `docs/PHASE2_CHUNK_PROMPT.md`, one chunk at a time, in chunk order.

## Appendix B — Registration measurements

*Written by Chunk P2-1. Empty until then; user gate C depends on it.*

## Appendix C — Stitching sample-scan facts

*Written by Chunk P2-1 from the user gate B files, in the style of Phase 1's
appendix A. Empty until then.*
