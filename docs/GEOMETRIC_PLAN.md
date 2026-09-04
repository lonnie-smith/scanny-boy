# Geometric calibration implementation plan

Radial lens distortion and lateral chromatic aberration, fitted from ChArUco
frames and applied inside the existing stitch warp. The calibration is folded
into the **existing flat-field profile** — one profile record, one UI, one
CLI invocation — so a "profile" becomes the complete optical description of
one rig configuration rather than just its falloff.

This is the companion to `docs/FLATFIELD_PLAN.md`'s design and follows its
conventions: every constant lives in exactly one module, every threshold is
recorded in the profile so a record can be interpreted without knowing which
build wrote it, and a correction that does not measurably help is dropped
rather than carried.

---

## 0. Source documents and the corrections applied to them

Two design artifacts preceded this plan: a capture protocol ("Plumb-Line
Calibration: Capture Protocol") and a fitting/integration outline
("Geometric Calibration: Fitting and Pipeline Integration"). They are
sound on the mathematics. They were written without the codebase in view,
and this plan **overrides** them on the following points. Where they and
this document disagree, this document wins.

1. **There is no homography anywhere in this pipeline.** The artifact's
   Part C describes `undistort ∘ homography`. The real registration model is
   **rigid: rotation plus translation, scale forced to exactly 1**
   (`registration.rigid_from_correspondences`, closed-form Umeyama;
   `docs/DECISIONS.md`, "Registration model"). `estimateAffinePartial2D` is
   used only to obtain the RANSAC inlier mask and a similarity scale for the
   drift gate; the transform actually used is always re-fitted rigidly.
   Undistorting the matched points before the rigid fit is still exactly the
   right move, but every mention of "homography" below reads "rigid
   placement transform".

2. **The pipeline has a hard two-stage boundary the artifact does not
   mention.** `convert` decodes each NEF, applies flat-field, and writes a
   16-bit **linear** intermediate TIFF to a work directory. `stitch` reads
   those intermediates, detects and matches features, solves a global
   layout, and composites. Any correction has to choose a side of that
   boundary. Flat-field is on the convert side; radial distortion and the
   CA map path are on the stitch side (section 5).

3. **Flat-field is per-channel, not "luminance".** `flatfield.compute_gain`
   runs independently on R, G and B and divides each by its own mean. The
   artifact's "flat-field luminance correction" understates it. The ordering
   conclusion is unchanged and correct: flat-field is a per-sensor-pixel
   photometric property in native sensor coordinates and **must stay first**.

4. **Feature detection does not run on a colour channel.**
   `detection.build_detection_image` builds a Rec.709 luminance image,
   downscales it to a 2000 px long edge, and percentile-stretches it to
   8-bit. The artifact's "run feature detection on green" is therefore not a
   one-line change and is **not adopted**: luminance is already 71.5% green,
   the detection image is at roughly one-third resolution, and
   `DETECTION_LONG_EDGE` / `USE_CLAHE` are gate-C measured constants that a
   channel swap would invalidate. Residual CA in the keypoints is bounded
   well under `RANSAC_REPROJ_PX = 3.0`. The plan instead **measures** it
   (section 4.6) and leaves the change as a follow-up with a number attached.

5. **Lightroom is irrelevant here.** The production path is NEF → `rawpy`
   with `raw_decode.RAW_PARAMS` and nothing else. The artifact's warning
   about Lightroom lens corrections does not apply.

6. **The CA fit must undistort first.** The artifact fits the per-channel
   radial scale against raw observed corner radii. That conflates lateral CA
   with radial distortion, because the distortion polynomial is itself
   radial. Section 4.5 fits CA on corners that have already been undistorted
   with the green-fitted coefficients, which is also the only order
   consistent with how section 5.3 applies the two corrections.

7. **Radii are normalised, never scaled back up.** The artifact fits CA at
   `half_size=True` and then rescales the coefficients to full resolution.
   Working in normalised camera coordinates (divide by `fx`) makes half-size
   and full-size algebraically identical, so no rescaling step exists to get
   wrong. Same normalisation as the distortion fit.

8. Everything else in the two artifacts — the plumb-line objective, the
   OpenCV parameterisation gotcha, the staged fit, held-out validation, the
   gauge-freedom warning, the reason CA matters at seams specifically, and
   the whole capture protocol — is adopted as written.

---

## 1. Locked decisions

These were settled with the user before this plan was written. Do not
relitigate them.

| Decision | Choice |
| --- | --- |
| Where undistortion applies | Folded into the stitch warp: one interpolation pass per pixel |
| Chromatic aberration scope | Both paths built — pure-scale at decode, per-channel maps at composite; the fit picks |
| Fitting solver | `scipy.optimize.least_squares` (new dependency) |
| ChArUco boards | The two boards in `calibration/lens_calibration_targets.pdf`, hard-coded |
| Profile model | The existing `flatfield_profiles` record, extended — not a new table or command family |
| Gauge convention | `K_new = K`, output frame identical in size to the source frame |
| Held-out split | Automatic and deterministic, recorded in the profile |

### 1.1 Gauge freedom, resolved

The artifact correctly warns that `(1 + k1·r² + k2·r⁴)` rescales the image
and that plumb-line has nothing to say about absolute scale. The convention
chosen here is the simplest one that exists: **`K_new = K` and the
undistorted frame has exactly the same pixel dimensions as the source
frame.**

This matters far beyond tidiness. `frame_size` flows through
`layout.solve_layout`, `layout.largest_valid_rect`,
`composite.estimate_peak_bytes`, `disk_check.required_free_bytes` and
`_frame_bbox`. Holding it fixed means none of them change and none of them
need to learn about distortion.

The cost is a border of uncovered or unsampled pixels at the frame edge,
1–7 px wide at the distortion magnitudes expected here. `MASK_ERODE_PX = 5`
already discards a comparable margin, the validity mask records the truth
either way, and the capture workflow guarantees at least 20% overlap on
every overlapping edge. This is not a real loss.

`largest_valid_rect` computes the interior rect from frame *rectangles* and
will be off by the same 1–7 px at the corners under pincushion. Accepted;
note it in that function's docstring rather than complicating it.

### 1.2 The fixed camera matrix

Straightness is scale-invariant, so `K` only sets the normalisation of `r`
and therefore the numeric scale of `k1`. It must be held fixed so
coefficients stay comparable across sessions:

```
fx = fy = max(frame_width, frame_height)     # dimension-derived, reproducible
cx0 = (frame_width  - 1) / 2
cy0 = (frame_height - 1) / 2
```

`fx`, `fy` and the frame dimensions used to derive them are recorded in the
profile. `cx`, `cy` are the fit's starting point, not necessarily its result.

Because `k1` is normalised by `fx`, a profile is only valid for frames of
the dimensions it was fitted at. A run whose frames decode to different
dimensions fails with `GEOMETRY_FRAME_SIZE_MISMATCH` at validation time,
before anything is written. Do not attempt to rescale `K` to fit — a
dimension change means a different decode, and a silently rescaled
calibration is worse than no calibration.

---

## 2. The boards

`calibration/lens_calibration_targets.pdf` is a single US-Letter page
carrying both targets, printed at 100% and mounted flat in the negative
carrier. The PDF embeds its own OpenCV recreation line; these constants are
transcribed from it and must match it exactly.

New module `cli/src/scanny_boy/charuco.py`:

```python
@dataclasses.dataclass(frozen=True)
class BoardSpec:
    key: str                 # "35mm" | "6x9"
    squares_x: int           # columns, along the strip's long axis
    squares_y: int           # rows, across the strip's width
    square_length_mm: float
    marker_length_mm: float
    dictionary: str          # cv2.aruco predefined dictionary name

BOARDS = {
    "35mm": BoardSpec("35mm", 13,  9, 3.0, 2.2, "DICT_5X5_100"),
    "6x9":  BoardSpec("6x9",  21, 14, 4.0, 3.0, "DICT_5X5_250"),
}
```

Derived facts the fit relies on:

- Interior (ChArUco) corner grid is `(squares_x - 1) x (squares_y - 1)`:
  **12 x 8 = 96** corners for 35mm, **20 x 13 = 260** for 6x9.
- `charucoIds` index that grid row-major, so
  `row = id // (squares_x - 1)` and `col = id % (squares_x - 1)`.
  This is the whole reason ChArUco was chosen: every detected corner carries
  an exact, known collinear-set membership with no inference.
- Marker counts are `floor(squares_x * squares_y / 2)` — 58 and 147,
  matching the PDF's stated id ranges.

The two boards use **different dictionaries**, which makes format detection
free: build both detectors, run both on the first calibration frame, and
take the one with more detected corners. Require the winner to have at least
`MIN_CORNERS_PER_FRAME` and the loser to have essentially none; anything
ambiguous fails with `GEOMETRY_BOARD_NOT_DETECTED`. The detected board key
is recorded on the profile and reused for every remaining frame — never
re-detected per frame.

`calibration/lens_calibration_targets.pdf` stays in the repository as the
authoritative artefact. Do not add a board-generator command; the target
already exists.

---

## 3. Data model

### 3.1 Profile record

`flatfield.FlatFieldProfile` gains four nullable fields. Existing profiles
read back with all four `None` and behave exactly as they do today — that
backward compatibility is a hard requirement, not a nicety.

```python
@dataclasses.dataclass(frozen=True)
class FlatFieldProfile:
    # ... every existing field, unchanged ...
    board_key: str | None                 # "35mm" | "6x9"
    geometry: dict | None                 # section 3.2
    chromatic_aberration: dict | None     # section 3.3
    calibration_report: dict | None       # section 3.4
```

### 3.2 `geometry`

```json
{
  "format_version": 1,
  "frame_width": 6048,
  "frame_height": 4024,
  "fx": 6048.0,
  "fy": 6048.0,
  "k1": -0.00123,
  "k2": 0.0,
  "cx": 3023.4,
  "cy": 2011.8,
  "stage": "k1",
  "gauge": "identity",
  "board_key": "35mm"
}
```

`stage` is one of `"k1"`, `"k1k2"`, `"k1k2c"` — which staged fit won on
held-out residual (section 4.4). `gauge` is `"identity"` and is the only
value this format version defines; it is recorded so a future convention
change is detectable rather than silent.

`k1`/`k2` are in **OpenCV forward convention**, so they drop straight into
`cv2.undistortPoints`, `cv2.initUndistortRectifyMap` and the closed-form
forward map of section 5.3 with no conversion.

### 3.3 `chromatic_aberration`

```json
{
  "format_version": 1,
  "mode": "scale",
  "red":  {"c0": 1.00042, "c1": 0.0, "c2": 0.0, "center_x": 0.0, "center_y": 0.0},
  "blue": {"c0": 0.99961, "c1": 0.0, "c2": 0.0, "center_x": 0.0, "center_y": 0.0},
  "red_scale": 0.99958,
  "blue_scale": 1.00039
}
```

`mode` is `"scale"` or `"maps"`.

- Coefficients and centres are in **normalised camera coordinates** (section
  0.7). `center_x`/`center_y` are offsets from the principal point in those
  units.
- `red_scale` / `blue_scale` are present only in `"scale"` mode and are the
  values handed to `rawpy`'s `chromatic_aberration=(red_scale, blue_scale)`.
  **They are the reciprocals of `c0`.** The fit measures where the red
  corner *is* (`r_R = c0 · r_G`); the decoder is told what to *multiply* red
  by to put it back. Getting this backwards doubles the aberration instead
  of removing it, so section 8 requires a test that a synthetically scaled
  channel round-trips.

### 3.4 `calibration_report`

Everything a human needs to decide whether the profile is worth keeping.
The app displays it; the plan requires it precisely because the discipline
"drop any correction that fails its check" is only real if the numbers are
in front of someone.

```json
{
  "frames_total": 20,
  "frames_fit": 15,
  "frames_heldout": 5,
  "heldout_frame_names": ["...", "..."],
  "corners_detected_median": 88,
  "distortion": {
    "heldout_rms_px_before": 2.41,
    "heldout_rms_px_after": 0.28,
    "corner_displacement_px": 4.7,
    "corner_displacement_percent": 0.129,
    "accepted": true,
    "rejection_reason": null,
    "stage_heldout_rms_px": {"k1": 0.28, "k1k2": 0.28, "k1k2c": 0.27}
  },
  "chromatic_aberration": {
    "heldout_misregistration_px_before": {"red": 0.9, "blue": 1.4},
    "heldout_misregistration_px_after":  {"red": 0.11, "blue": 0.18},
    "radial_term_px_at_corner": {"red": 0.02, "blue": 0.03},
    "mode": "scale",
    "accepted": true,
    "rejection_reason": null
  },
  "detection_channel_ca_px": 0.21
}
```

`detection_channel_ca_px` is the measurement described in section 4.6 — the
CA displacement carried by the luminance detection image relative to green,
at the frame corners. It gates nothing; it exists so the "detect on green"
question can be settled with a number later.

### 3.5 Migration `0004_calibration_profiles.py`

Adds four nullable TEXT columns to `flatfield_profiles`: `board_key`,
`geometry`, `chromatic_aberration`, `calibration_report`. The last three are
`JSONText`. Downgrade drops them. No data migration — existing rows get
NULLs and stay valid.

`library/models.py`'s `FlatFieldProfileRow` and `library/repo.py`'s
`_to_flatfield_profile` / `save_flatfield_profile` gain the four fields.

### 3.6 Tokens and roll invariants

The single user-facing profile writes into **two** invariant buckets,
because the two corrections apply on opposite sides of the convert/stitch
boundary. Both follow the flat-field rule: **absent, not null**, when the
profile carries nothing for that bucket, so a profile without geometry
compares equal to a pre-geometry roll.

- `processing_params["flat_field"]` — unchanged, `flatfield.profile_token`.
- `processing_params["chromatic_aberration"]` — present only when
  `mode == "scale"`:
  `{"profile_id", "mode": "scale", "red_scale", "blue_scale"}`.
  It belongs here because it is a decode parameter.
- `stitch_params["geometry"]` — present only when the profile has geometry:
  `{"profile_id", "geometry": <the section 3.2 object>,
    "chromatic_aberration": <the section 3.3 object or absent>}`.
  The CA object appears here only in `"maps"` mode.

`repo.rolls_using_flatfield` gains a sibling that also scans
`stitch_params.geometry.profile_id`, and `flatfield delete`'s
`FLATFIELD_PROFILE_IN_USE` check uses the union. A profile whose geometry a
roll depends on is exactly as undeletable as one whose gain map it depends
on.

---

## 4. Calibration: the fit

New modules:

- `charuco.py` — board specs, detector construction, per-frame corner
  detection, format auto-detection.
- `geometry_fit.py` — the plumb-line fit, staged, with held-out evaluation
  and the acceptance gates.
- `ca_fit.py` — the half-size per-channel fit and the mode decision.
- `calibration.py` — the orchestrator. `flatfield.create_profile` moves
  here; `flatfield.py` goes back to owning only the gain map.

### 4.1 Inputs and the held-out split

`flatfield create` gains `--calibration FILE [FILE ...]` (paths relative to
nothing — absolute, like `--reference`).

- Fewer than `MIN_CALIBRATION_FRAMES = 12` fails with
  `GEOMETRY_INSUFFICIENT_FRAMES`.
- Fewer than `RECOMMENDED_CALIBRATION_FRAMES = 16` warns with
  `GEOMETRY_FEW_FRAMES` and proceeds.
- The split is deterministic: sort the given paths by filename, hold out
  every 4th (indices 3, 7, 11, …). Record the held-out names in the report.
  No randomness, no seed, no UI control — a rerun on the same files must
  produce the same profile.

### 4.2 Decode and detect

Decode each calibration NEF with **`raw_decode.RAW_PARAMS`, unchanged** —
the identical call the production path uses, because demosaic choice moves
sub-pixel corner position.

Build the detection greyscale at **full resolution** — not through
`detection.build_detection_image`, whose `DETECTION_LONG_EDGE` downscale
would throw away exactly the precision being measured. Reuse its luminance
weights and percentile stretch, at native size, in a small helper in
`charuco.py`.

Then:

1. `cv2.aruco.CharucoDetector(board).detectBoard(gray)`.
2. `cv2.cornerSubPix` with a window of roughly a quarter of the square pitch
   in pixels, derived from the median detected square spacing on the first
   frame rather than assumed.
3. A frame yielding fewer than `MIN_CORNERS_PER_FRAME = 20` corners is
   dropped with a warning, not a failure. If fewer than
   `MIN_CALIBRATION_FRAMES` survive, fail `GEOMETRY_INSUFFICIENT_FRAMES`.

### 4.3 Collinear sets

For each frame, group detected corners by `row = id // (squares_x - 1)` and
by `col = id % (squares_x - 1)`. Keep any set with at least 4 members. Add
the two diagonal families (`row - col` constant, `row + col` constant) —
they are what constrain `cx, cy`, and they cost nothing given the ids.

### 4.4 The objective and the staged fit

Straight from the artifact, with `K` fixed per section 1.2:

```python
def residuals(p, line_sets, K_base):
    k1, k2, cx, cy = p
    K = K_base.copy(); K[0, 2], K[1, 2] = cx, cy
    D = np.array([k1, k2, 0.0, 0.0, 0.0])
    out = []
    for pts in line_sets:                      # (N,1,2) float32
        u = cv2.undistortPoints(pts, K, D, P=K).reshape(-1, 2)
        c = u - u.mean(0)
        n = np.linalg.svd(c, full_matrices=False)[2][1]
        out.append(c @ n)
    return np.concatenate(out)
```

`cv2.undistortPoints` inverts the forward model iteratively — slower per
evaluation, and the entire point: the fitted coefficients are already in the
convention every consumer wants.

Three stages, each solved with
`scipy.optimize.least_squares(..., loss="huber", f_scale=1.0)`:

1. `k1` alone, `k2 = 0`, `cx, cy` fixed at the image centre.
2. `k1, k2`, centre still fixed.
3. `k1, k2, cx, cy` all free.

Evaluate **held-out** RMS perpendicular residual after each. Take the
earliest stage that the next does not beat by at least
`STAGE_IMPROVEMENT_FRACTION = 0.05` relative. Record every stage's held-out
RMS in the report. On a lens this clean, expect stage 1 to win.

### 4.5 Acceptance gates for distortion

Compute uncorrected held-out RMS (all parameters zero) and the winning
stage's held-out RMS. Accept the geometry only when **both** hold:

- relative improvement ≥ `GEOMETRY_MIN_IMPROVEMENT_FRACTION = 0.30`, and
- absolute improvement ≥ `GEOMETRY_MIN_IMPROVEMENT_PX = 0.3`.

Then sanity-check magnitude: corner displacement at the image corner, in px
and as a percentage of half-diagonal.

- Outside `[0.01%, 1.0%]` → reject with `GEOMETRY_FIT_REJECTED`.
- Outside `[0.03%, 0.2%]` but inside the hard band → accept with a
  `GEOMETRY_MAGNITUDE_SUSPECT` warning.

A rejected fit stores `geometry: null` and a populated
`calibration_report.distortion.rejection_reason`. The profile is still
created — it is a perfectly good flat-field profile. This is the artifact's
"any correction that fails its check should be dropped rather than carried",
made automatic instead of a judgement call.

### 4.6 Chromatic aberration

**Decode differently.** `RAW_PARAMS | {"half_size": True}`, defined in
`ca_fit.py` as an explicit derivation of `RAW_PARAMS` with a docstring
saying why it deviates. Each output pixel comes from one Bayer quad with no
interpolation, so the per-channel geometry is true rather than smeared by
the demosaic.

Because `K_half = K_full / 2`, normalised coordinates are identical at both
resolutions. Nothing is scaled back up.

Per frame:

1. Detect ChArUco and run `cornerSubPix` independently on R, G and B.
2. Keep corners detected in all three channels — correspondence is the whole
   point.
3. **Undistort all three channels' corners** with the accepted green
   coefficients (section 0.6). If geometry was rejected, undistort with
   zero coefficients; CA is still measurable.
4. Convert to normalised coordinates.

For each of red and blue, fit `(c0, c1, c2, center_x, center_y)` by least
squares against:

```
r_c = r_g · (c0 + c1·r_g² + c2·r_g⁴)
```

with `r_g` measured from that channel's own fitted centre. Fitting the
centre per channel is not optional — decentring is plausible on an
FTZ-adapted manual lens, and an assumed centre shows up as a spurious
tangential component the radial model cannot absorb.

**Mode decision.** Evaluate the radial terms' contribution in full-resolution
pixels at the image corner:
`|c1·r² + c2·r⁴| · r · fx`. If below `CA_SCALE_ONLY_PX = 0.05` for both
channels → `mode = "scale"`, `red_scale = 1/c0_R`, `blue_scale = 1/c0_B`.
Otherwise → `mode = "maps"`.

**Acceptance.** On held-out frames, measure R–G and B–G displacement at the
frame corners before and after correction. Accept only when residual is
below `CA_RESIDUAL_ACCEPT_PX = 0.3` **and** improves on the uncorrected
figure by at least 30%. Otherwise store `chromatic_aberration: null` with a
`CHROMATIC_FIT_REJECTED` warning and a reason in the report.

**The detection-channel measurement.** While the per-channel corners are in
hand, compute Rec.709 luminance corner positions the same way
`detection.build_detection_image` would weight them, and report their corner
displacement from green as `detection_channel_ca_px`. This is the number
that settles the artifact's "detect on green" recommendation later. It gates
nothing now.

### 4.7 Order of operations inside `create_profile`

This ordering is load-bearing, because in `"scale"` mode the flat-field
reference must be decoded with the *same* CA scales production will use, or
the gain map and the frames disagree about geometry.

1. Detect the board format from the first calibration frame.
2. Decode and detect all calibration frames at full resolution.
3. Fit and gate the distortion.
4. Decode and detect all calibration frames at half size, per channel.
5. Fit and gate CA; decide the mode.
6. Decode the flat-field reference with `RAW_PARAMS` plus the CA scales when
   `mode == "scale"`, and build the gain map from that.
7. Assemble the report, save the gain map, insert the row.

Record the CA scales in `flatfield.build_params()` output so the gain map's
provenance says which decode produced it.

### 4.8 Progress

Steps 2 and 4 each decode ~20 24MP RAWs; this command now runs for minutes
where it used to run for seconds. Add a `flatfield_progress` event carrying
`{phase, completed, total}` with `phase` in
`"detect" | "fit" | "chromatic" | "reference"`. It carries no `run_id` —
consistent with CONTRACT.md's rule that the `flatfield` family is not a
pipeline run.

Decoding is the bottleneck and is embarrassingly parallel; reuse
`concurrency.resolve_worker_count`'s budget rather than inventing a second
worker-count policy.

---

## 5. Application

### 5.1 Nothing changes when a profile has no geometry

Every code path below must be a no-op for a profile with
`geometry is None` and `chromatic_aberration is None`. Specifically,
`composite.composite` keeps its current `cv2.warpAffine` implementation
byte-for-byte on that path. Existing rolls, existing fixtures and every
current test must produce identical pixels. Guard this with an explicit
regression test, not by inspection.

### 5.2 Convert stage — CA in `"scale"` mode only

`pipeline.run_convert` gains the profile's CA scales and passes them to
`raw_decode.decode_raw`, which gains an optional
`chromatic_aberration: tuple[float, float] | None` merged into `RAW_PARAMS`
for that call. The merged params are what
`raw_decode.jsonable_raw_params()` must report, so `processing_params`
describes the decode that actually happened.

Order inside `_stage_one_frame` is unchanged: decode (now CA-corrected) →
flat-field → write intermediate. Flat-field stays first among the
corrections applied to decoded pixels, exactly as today.

`"maps"` mode adds nothing to the convert stage.

### 5.3 Stitch stage — the composed band map

This is the substance of the change and the reason the fold-into-the-warp
option was chosen.

**Registration.** `registration.register_pair` gains an optional
undistorter. After `to_full_resolution` and before
`estimateAffinePartial2D`, push both point sets through
`cv2.undistortPoints(pts, K, D, P=K)`. Everything downstream — RANSAC, the
rigid fit, `rms_residual_px`, `scale_drift`, `solve_layout`,
`global_rms` — then works in undistorted full-resolution pixels. No
threshold's units change.

Undistortion should *reduce* apparent scale drift and pair residuals. That
means `SCALE_DRIFT_WARN = 0.005`, `MAX_PAIR_RMS_PX = 6.0` and
`MAX_GLOBAL_RMS_PX` are now loose for geometry-corrected rolls. Do not
retune them in this change — record the before/after numbers.

**Compositing.** Replace the two `cv2.warpAffine` calls with a banded
`cv2.remap` when geometry is present. No `initUndistortRectifyMap`, no
cached frame-sized base map: the map this needs is *forward* (undistorted →
distorted), which is closed-form, so it can be generated a band at a time
for nothing.

Per frame, for each band of `GEOMETRY_BAND_ROWS = 256` output rows of the
frame's bounding box, and per channel:

```
1.  p = R⁻¹ · ([u, v] − t)          # bbox output px → undistorted frame px
2.  x = (p.x − cx) / fx             # normalise
    y = (p.y − cy) / fy
3.  CA, "maps" mode only, channel c:
        dx, dy = x − ccx, y − ccy
        r      = hypot(dx, dy)
        s      = c0 + c1·r² + c2·r⁴
        x, y   = ccx + dx·s, ccy + dy·s
    Green, and every channel in "scale" mode: unchanged.
4.  r² = x² + y²                    # forward radial distortion
    k  = 1 + k1·r² + k2·r⁴
    x, y = x·k, y·k
5.  map_x = x·fx + cx               # denormalise → source frame px
    map_y = y·fy + cy
6.  cv2.remap(source_channel, map_x, map_y, INTERPOLATION,
              borderMode=cv2.BORDER_CONSTANT, borderValue=0)
```

`R⁻¹` and `t` come from inverting the frame's `bbox_matrix` — the same 2×3
that `warpAffine` consumes today, so `_frame_bbox` is untouched.

In `"scale"` or no-CA mode the three channels share one map, so remap the
3-channel source once. In `"maps"` mode build three maps per band and remap
three single-channel sources. Either way: **exactly one interpolation pass
per output pixel.** The `np.clip(warped, 0.0, None)` undershoot guard after
the warp stays.

The validity mask is remapped with the **green** map at `INTER_NEAREST`,
then eroded by `MASK_ERODE_PX` exactly as today.

**Memory.** `composite.estimate_peak_bytes` gains:

- `3 * GEOMETRY_BAND_ROWS * bbox_width * 2 * 4` for the band maps, and
- `frame_pixels * 4` for the one contiguous single-channel source view held
  during a `"maps"`-mode remap.

At 24MP with a 7000 px bbox that is roughly 14 MB plus 97 MB — real, but an
order of magnitude below the frame-sized base map that
`initUndistortRectifyMap` would have forced. `MEMORY_SAFETY_FACTOR` is
unchanged; do not re-measure `concurrency.py`'s per-worker budget.

### 5.4 CLI surface for the stitch side

`stitch` gains `--flatfield ID`, which is where geometry reaches the stitch
stage. `run` passes its own `--flatfield` through to both stages
(`run_pipeline.run_full` currently drops it before `run_stitch` — fix that).

Omitting it on a roll whose `stitch_params` carry geometry fails
`ROLL_INVARIANT_MISMATCH` through the existing check, with no new code. That
self-enforcement is why the flag is explicit rather than inferred from the
work manifest.

`probe --flatfield` additionally checks `geometry.frame_width/height`
against the selection's decoded active size and fails
`GEOMETRY_FRAME_SIZE_MISMATCH` early, before conversion starts.

### 5.5 Flag naming

`--flatfield` now names a profile that may carry geometry and CA as well as
a gain map. The name is stale. **Do not rename it in this change** — it
reaches `cli.py`, `CONTRACT.md`, `schema.json`, `CLIRunner.swift`,
`ConfigurationModel.swift` and its stored-defaults key, and a rename would
bury the substance under churn. Note it in `docs/punchlist.md` as a
cosmetic follow-up; CONTRACT.md should say plainly that the flag names a
calibration profile.

---

## 6. Contract

Protocol version **6 → 7**. Update `events.PROTOCOL_VERSION`,
`shared/contract/schema.json`, `shared/contract/CONTRACT.md`,
`events_test.py`'s version assertions, and the Swift test that centralises
the literal (`mac/ScannyBoyTests/`, per commit `ce3179a`).

New events:

| Event | Payload |
| --- | --- |
| `flatfield_progress` | `phase`, `completed`, `total` |

`flatfield_created` and `flatfield_list`'s profile objects gain
`board_key`, `has_geometry` (bool), `chromatic_aberration_mode`
(`"scale"`/`"maps"`/`null`) and `calibration_report`. The gain map path and
SHA stay absent, per the existing rule that Swift never sees the CLI's
storage.

New codes:

| Code | Kind | Meaning |
| --- | --- | --- |
| `GEOMETRY_INSUFFICIENT_FRAMES` | error | Too few usable calibration frames |
| `GEOMETRY_BOARD_NOT_DETECTED` | error | Neither board detected, or ambiguous |
| `GEOMETRY_FRAME_SIZE_MISMATCH` | error | Profile fitted at other frame dimensions |
| `GEOMETRY_FIT_REJECTED` | warning | Fit did not clear its acceptance gates |
| `GEOMETRY_MAGNITUDE_SUSPECT` | warning | Distortion outside the expected 0.03–0.2% band |
| `GEOMETRY_FEW_FRAMES` | warning | Under 16 calibration frames |
| `CHROMATIC_FIT_REJECTED` | warning | CA fit did not clear its acceptance gates |

Invocation line:

```text
scanny-boy flatfield create --reference FILE --name NAME
                            [--calibration FILE [FILE ...]]

scanny-boy stitch --work DIR --roll DIR [--jobs N] [--overwrite]
                  [--allow-partial] [--negatives ID ...] [--flatfield ID]
```

`--calibration` is optional: a profile with a reference and no calibration
frames is exactly today's flat-field profile, and must keep working.

---

## 7. The Mac app

Single sheet, single process, per the user's requirement. All changes are in
`FlatFieldProfilesSheet.swift`, `FlatFieldModel.swift`,
`FlatFieldProfile.swift`, `CLIRunner.swift` and `CLIEvent.swift`.

**New Profile section** gains, below the existing reference picker:

- "Calibration Frames…" — an `NSOpenPanel` with `allowsMultipleSelection`,
  NEF only. Shows "n frames selected" and a "Clear" affordance. Optional:
  with none selected, the sheet behaves exactly as it does today.
- A short line of guidance matching the capture protocol: 16–20 frames,
  board rotated 0/45/90/135° and translated so the pattern reaches every
  quadrant and every image corner.

**Create** now runs for minutes. Replace the indeterminate spinner with a
determinate `ProgressView` driven by `flatfield_progress`, with the phase as
its label. Keep Create disabled and the sheet non-dismissable while it runs;
the existing `CLISession` cancellation path should stay wired.

**Profile rows** gain a second caption line summarising the calibration:
either "Flat-field only" or something like
"Distortion 4.7 px (0.13%) · CA corrected (scale)". A rejected fit reads
"Distortion: not applied" with the reason available on hover or in a
disclosure — the user has to be able to see that a correction was dropped
and why, or the automatic gates become invisible.

`FlatFieldProfile` gains the summary fields from section 6 and stays a
straight decode of what the event carried — no computation in Swift.

Format detection is automatic (section 2), so no format picker.

---

## 8. Testing

Follow the existing convention: `*_test.py` beside each module,
`synthetic_scene_support.py`-style fixtures for anything needing pixels.

**`charuco_test.py`**
- Board constants match the PDF's stated marker counts and corner grids.
- Row/column/diagonal grouping from `charucoIds` is correct for both boards.
- Format auto-detection picks the right board on a rendered board image and
  raises on a blank frame.

**`geometry_fit_test.py`**
- Round trip: take a synthetic ChArUco corner set, distort it with known
  `(k1, k2, cx, cy)`, fit, and recover the parameters to a tight tolerance.
  This is the load-bearing test — everything else is plumbing.
- Held-out RMS falls when the injected distortion is real and does not when
  the input is already straight.
- Staged selection: a `k2`-free synthetic set selects stage `"k1"`.
- Acceptance gates reject a null fit and a wildly out-of-band magnitude.

**`ca_fit_test.py`**
- **Direction test.** Synthesise a per-channel pure scale, fit it, and prove
  that decoding with `chromatic_aberration=(red_scale, blue_scale)` removes
  it rather than doubling it. Section 3.3 exists because this is the one
  thing most likely to ship backwards.
- Normalised coordinates make half-size and full-size fits agree.
- `"maps"` mode is chosen when a radial term above `CA_SCALE_ONLY_PX` is
  injected, `"scale"` when it is not.

**`composite_test.py`**
- **Regression:** a profile with no geometry produces pixels identical to
  the current `warpAffine` path. Assert on exact array equality.
- Round trip: distort a synthetic frame with known coefficients, composite
  it through the band-map path, and recover the original to within
  interpolation error.
- `"maps"` mode leaves green untouched and moves red and blue by the
  predicted amount.
- `estimate_peak_bytes` grows by the section 5.3 terms and no others.

**`calibration_test.py`**
- Ordering: in `"scale"` mode the flat-field reference is decoded with the
  CA scales applied. Assert on the recorded `build_params`, not on pixels.
- A profile created without `--calibration` is byte-identical to one created
  by today's code path.
- The held-out split is deterministic across runs.

**`repo_test.py` / migration**
- A pre-0004 row reads back with four `None`s and drives every existing code
  path unchanged.
- `FLATFIELD_PROFILE_IN_USE` fires for a roll that names the profile only in
  `stitch_params.geometry`.

**`packaging_test.py`**
- The frozen bundle can `import scipy.optimize` and construct
  `cv2.aruco.CharucoDetector`.
- `opencv_availability_test.py` gains `aruco`, `undistortPoints` and
  `remap` to its symbol list.

**Schema**
- `schema_test_support.py` validates `flatfield_progress` and the extended
  profile object against `schema.json`.

---

## 9. Dependency and packaging changes

- `cli/pyproject.toml`: add `scipy>=1.14,<2`.
- `cli/packaging/scanny_boy.spec`: PyInstaller has a scipy hook, but confirm
  against the frozen bundle rather than trusting it — the spec's own
  docstring says every entry there fixes a runtime-only failure. Add
  `scipy.tests` and `numpy.tests` to `excludes` to hold the bundle down.
- `THIRD_PARTY_NOTICES.md`: add a SciPy section (BSD 3-Clause) and add
  `scipy` to the runtime-dependency list in the closing paragraph.
- `docs/ARCHITECTURE.md` and `docs/DECISIONS.md`: a "Geometric calibration"
  section covering the gauge convention, why the correction lives in the
  stitch warp, and the point-vs-image undistortion argument.
- `docs/punchlist.md`: the `--flatfield` rename, re-measuring
  `SCALE_DRIFT_*` / `MAX_PAIR_RMS_PX` / `MAX_GLOBAL_RMS_PX` against
  geometry-corrected rolls, and the detect-on-green question with
  `detection_channel_ca_px` attached.

---

## 10. Work order

Each chunk ends green — tests pass, nothing half-wired.

| # | Chunk | Touches |
| --- | --- | --- |
| G-1 | scipy dependency, packaging, notices, `opencv_availability_test` | `pyproject.toml`, spec, `THIRD_PARTY_NOTICES.md`, tests |
| G-2 | `charuco.py`: boards, detector, full-res detection, format auto-detect | new module + test |
| G-3 | `geometry_fit.py`: staged plumb-line fit, held-out eval, acceptance gates | new module + test |
| G-4 | `ca_fit.py`: half-size per-channel fit, mode decision, acceptance | new module + test |
| G-5 | Migration 0004, model/repo fields, `rolls_using_flatfield` union | `library/`, tests |
| G-6 | `calibration.py`: orchestration, ordering, report, `flatfield_progress` | new module, `flatfield.py`, tests |
| G-7 | CLI: `--calibration`, `stitch --flatfield`, `run` passthrough, new codes, protocol 7 | `cli.py`, `events.py`, `run_pipeline.py`, contract, schema |
| G-8 | Convert-stage CA `"scale"` path | `raw_decode.py`, `pipeline.py`, tests |
| G-9 | `registration.py`: undistort matched points | `registration.py`, `stitch_pipeline.py`, tests |
| G-10 | `composite.py`: banded remap, CA maps, memory formula | `composite.py`, tests |
| G-11 | Swift: command args, event kinds, profile fields, sheet, progress, summaries | `mac/` |
| G-12 | Docs: ARCHITECTURE, DECISIONS, punchlist | `docs/` |

G-2 through G-4 are independent of G-8 through G-10 and can be built in
either order. G-9 is the smallest correctness win in the whole plan — it
improves registration residuals at zero resampling and zero memory cost —
so if the change has to be split across releases, G-1…G-9 is a coherent
first half and G-10 the second.

---

## 11. Explicitly out of scope

- Renaming the `flatfield` command family or the `--flatfield` flag.
- Switching feature detection to the green channel. Measured, not acted on.
- Tangential distortion (`p1, p2`). The plumb-line objective can constrain
  it, but nothing observed suggests it is needed on this rig, and adding two
  more free parameters to a fit this well-conditioned invites overfitting.
- Per-negative or per-image correction toggles. A profile applies to a whole
  roll by construction, same as flat-field.
- Retuning `SCALE_DRIFT_*`, `MAX_PAIR_RMS_PX`, `MAX_GLOBAL_RMS_PX` or
  `MAX_OVERLAP_MAD`. All become loose once geometry is corrected; they get
  re-measured at the same user gate as the outstanding photometric-gain
  thresholds.
- Non-NEF calibration frames.
