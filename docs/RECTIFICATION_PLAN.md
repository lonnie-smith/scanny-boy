# Tilt rectification plan: a measured rig homography in the stitch warp

One change to the stitch stage: before the layout solves, fit **one
rectifying homography per negative** — two parameters, shared by every pair —
that absorbs the measured tilt between the camera and the film plane, and
work the layout and the composite in rectified coordinates where the
inter-frame maps really are the similarities `layout.py` already solves.

This plan follows the conventions of `docs/STITCH_QUALITY_PLAN.md` and
`docs/GEOMETRIC_PLAN.md`: every constant lives in exactly one module, every
threshold that shapes an output is recorded in the roll manifest, a fit that
does not measurably help is dropped automatically, and no threshold changes
without a measurement the user has approved.

**This contradicts a locked decision in letter and requires the
`docs/DECISIONS.md` amendment in §8 written before the code lands.** Both
`docs/DECISIONS.md` ("Registration model": "never an affine or a
homography"; the protocol-8 amendment: "still never an affine and never a
homography") and `layout.py`'s module docstring say the stitch never uses a
homography. The amendment's substance: nothing is ever *placed* by a
homography — the pairwise fit stays rigid, the layout stays a similarity,
and the rectifying homography is a fixed, measured property of the rig
applied in the same slot as the radial undistortion. The measure script that
motivates and validates this plan is `scripts/measure-tilt.py`, which
imports the production modules and changes nothing.

---

## 0. The measurement

`scripts/measure-tilt.py` (run on the 37 NEFs of the user's
`scanny boy inputs` folder, nine negatives inferred from capture-time gaps)
fits four models to the *same inlier correspondences* `register_pair`
already returns, per pair: production's rigid fit, production's similarity
fit, an 8-DOF per-pair homography, and the restricted model this plan
implements — `H = W⁻¹ · S · W` with one globally shared
`W = [[1,0,0],[0,1,0],[l1,l2,1]]` (centred, full-resolution px) and a
per-pair similarity, plus a per-pair variant of the same restricted model
for consistency checks.

The script was validated on synthetic ground truth before it ran: a
1.2°/0.8° two-axis tilt is recovered as 1.194°/0.783°, true zero is
recovered as −0.019°/+0.011°, and the homography and restricted models tie
at the keypoint noise floor in both cases while the similarity diverges
under tilt. Per-pair homographies were additionally stress-tested at the
capture workflow's worst case (20% overlap, 120 inliers, 1 px noise) during
planning: chained per-pair homographies bow a straight film edge 6.0 px
where the 2-parameter global model holds it to 1.7 px — the reason a
per-pair homography is *not* the fix.

Results on the real scans (17 pairs whose similarity RMS is above the
keypoint noise floor; pairs already at the floor tie under every model and
were excluded by the script):

| negative (frames) | rigid RMS px | similarity RMS px | homography RMS px | shared-tilt RMS px | tilt (x, y) deg |
| --- | --- | --- | --- | --- | --- |
| 13-frame burst, Aug 1 (13) | 0.15–0.28 | ≈ rigid | ≈ rigid | 0.211 | +0.08, +0.14 |
| Aug 2 session, five negatives (2–4 each) | 0.6–2.6 | up to 9.8 | 0.31–1.00 | 0.59–1.64 | x −0.72…+0.10, y −0.38…−0.11 |
| appendix A negative 1, `_DSC4638–40` (3) | 1.25, 1.68 | 3.32, 5.33 | 0.77, 0.71 | 1.083 | −0.29, −0.20 |
| Aug 29 negative, `_DSC5071–73` (3) | 1.57–1.75 | 2.14–3.69 | 0.57–1.44 | 1.504 | +0.04, −0.15 |

What the numbers establish:

1. **The projective component is real and large.** Median
   similarity→homography RMS improvement over the identifiable pairs:
   **2.55 px**. The user's 1.0–2.0 px residuals are the similarity model
   losing to a homography-shaped truth, not keypoint noise.
2. **The tilt is consistent within a session and varies between sessions.**
   The across-strip component is negative in **all eight manually-shot
   negatives** (−0.10° to −0.38°, median −0.17°); the along-strip component
   is near zero (median −0.04°, one +2.02° outlier resting on 92- and
   394-inlier pairs). The 13-frame burst — 13 frames in ~2 s — sits at
   0.2 px RMS with *no* tilt. The rig re-settles per setup; it is not a
   permanent property of the lens or the stand. This is why the correction
   is fitted **per negative**, not measured once and hardcoded, and why a
   hardware shim is a complement rather than the fix.
3. **One tilt does not explain everything.** Per-pair homographies beat the
   shared-tilt model by a consistent ~0.21 px (worst on the 6,825-inlier
   `_DSC5071–73` pair: 0.567 vs 1.027 px). That residual is real per-pair
   variation — film height and settle changing between advances, i.e. a
   modest flatness contribution. The global model removes the bulk; the
   remainder is recorded, not corrected (§11).
4. **The size of what the pipeline absorbs today.** The restricted model and
   the production similarity fit, evaluated at the frame corners, diverge by
   8–70 px per pair on these negatives. That systematic error is what
   accumulates along a strip into the curved edges users see.

Mechanism, for the record: a tilt makes the film plane non-fronto-parallel,
so the true frame-to-frame map is a homography. Fitting a similarity to
homography-shaped data leaves a small systematic residual per pair — well
under `MAX_PAIR_RMS_PX = 6.0`, invisible per pair — that accumulates along
the chain. Across-strip tilt curves the strip and produces almost no scale
drift, so nothing in the current gates can see it; along-strip tilt is
mostly absorbed by the per-frame scale solve and surfaces as magnification
drift instead.

---

## 1. Locked decisions

- **The model is one rectifying homography per negative, two parameters.**
  `W(l) = [[1,0,0],[0,1,0],[l1,l2,1]]` in centred full-resolution pixels
  (centre = the frame centre; every frame of a negative is the same size,
  which `solve_layout` already requires). Under `W`, every inter-frame map
  of a rigidly-tilted rig is an exact similarity. `l` has no gauge freedom:
  the parameterisation fixes `W[2,2] = 1`.
- **Nothing is placed by a homography.** The pairwise fit stays
  `rigid_from_correspondences` plus `similarity_from_correspondences`, the
  layout stays three linear solves, the gates keep their meanings. The
  rectification is a re-parameterisation of image coordinates — the same
  slot, and the same justification, as the radial undistortion
  (`docs/GEOMETRIC_PLAN.md` §5.3: "points are undistorted, not images").
- **Automatic, with acceptance gates; no CLI flag.** The same discipline as
  the calibration fits (`docs/GEOMETRIC_PLAN.md` §4.5: "the fit that does
  not measurably help is dropped, automatically") and normalization (pinned,
  not exposed). A rejected fit is not an error: the negative stitches
  exactly as today and the roll manifest records that no rectification was
  applied.
- **Per negative.** The fit uses only the negative's own accepted pairs.
  Nothing is shared across negatives — §0's measurement shows the tilt
  changes between sessions.
- **The canvas is rectified space.** When a rectification is applied, the
  published TIFF's geometry is `W`-rectified: a physically straight film
  edge maps to a straight canvas edge up to the per-frame placement
  residual. `l` is tiny (§0: the homogeneous weight varies by ≤ ~0.002
  across the frame), so the canvas differs from today's by a few pixels at
  the extremes — the bow is what leaves.
- **SciPy stays out of `registration.py` and `layout.py`.** The fit is two
  parameters of `scipy.optimize.least_squares` in a new module; everything
  closed-form (the rectify maps, the per-pair re-fits, the corner helper)
  is NumPy and lives in `registration.py`, which `layout.py` and
  `composite.py` already import.
- **Degrees are not computed in production.** Turning `l` into degrees
  needs an effective focal length in pixels, which production does not
  have (the gauge `K` is deliberately not a real focal length,
  `docs/GEOMETRIC_PLAN.md` §1.1). The manifest records `l` in 1/px;
  degree conversion is a measure-script concern.

---

## 2. The model

Rectification maps image px to rectified px by dividing by the homogeneous
weight. With `q = p − centre`:

```
rectify:  q' = q / (1 + l·q)          # W applied to centred coords
unrectify: q = q' / (1 − l·q')        # W⁻¹, the compositing direction
```

For a pair (a, b) the model predicts

```
p_a = W⁻¹ · S_ab · W · p_b
```

with `S_ab` a similarity. Per pair that is 4 free parameters plus the 2
shared; the full homography has 8, so the model is falsifiable per pair and
identified globally — exactly the shape of constraint §0's discriminator
exploited.

**The fit** minimises, over `(l1, l2)` only, the RMS residual where each
pair's similarity is re-fit **in closed form** (Umeyama,
`similarity_from_correspondences`) inside the residual function on the
rectified points:

```python
def residual(params):
    blocks = []
    for src, dst in pairs:            # pass-1 accepted pairs' inliers
        src_rect = rectify(src, l, centre)
        dst_rect = rectify(dst, l, centre)
        sim, _ = similarity_from_correspondences(src_rect, dst_rect)
        blocks.append((src_rect @ sim[:, :2].T + sim[:, 2] - dst_rect).ravel())
    return np.concatenate(blocks)
```

`least_squares(residual, x0=[0, 0], method="lm")`. The closed-form inner fit
is what makes this a 2-parameter problem — no per-pair parameters are ever
handed to the optimiser, which is why the model holds where per-pair
homographies degrade. Pairs contribute their unweighted residuals; the
layout solves' `sqrt(inliers)/rms` row weighting is deliberately *not*
replicated here (the zero-recovery validation in §0 ran unweighted; a
weighted variant is a named alternative if a user gate ever shows one pair
dominating). Correspondence order is the canonical pair order, so the fit
is deterministic and independent of placement order.

`rms_before` / `rms_after` for the acceptance gate are the RMS over all
inlier correspondences of all accepted pairs under (a) each pair's own
pass-1 similarity fit and (b) the shared-`l` model — computed on the same
pass-1 inliers, so the comparison is apples to apples.

---

## 3. `rectification_fit.py`: the fit and its gates

New module, named after `geometry_fit.py` (the distortion fit it mirrors).
It imports `registration` (for `PairResult`,
`similarity_from_correspondences`, and the closed-form rectification core)
and `scipy.optimize.least_squares`. Nothing else imports SciPy.

```python
_MIN_ACCEPTED_PAIRS = 2
_MIN_RELATIVE_IMPROVEMENT = 0.15
_MAX_WEIGHT_EXCURSION = 0.02

def fit_rectification(
    pairs: list[PairResult], frame_size: tuple[int, int]
) -> Rectification | None
```

Inputs are the **accepted** pairs only (`pair.accepted`), whose
`inlier_points_a/b` are already undistorted full-resolution px when a
calibration profile is active — the rectification lives in undistorted
coordinates, so `l` from a profiled roll and an unprofiled roll are not
comparable and never need to be.

Gates, evaluated in order, each returning `None` (record "not applied"):

1. **Support.** Fewer than `_MIN_ACCEPTED_PAIRS` accepted pairs → `None`.
   A single pair's `l` is statistically weak (thin overlap band), and a
   negative with one accepted pair has no chain to bow.
2. **Excursion.** `max |l·(corner − centre)|` over the frame's four corners
   must not exceed `_MAX_WEIGHT_EXCURSION`. This keeps the rectification
   moderate — at this rig's geometry it bounds the equivalent tilt near a
   few degrees — and, more importantly, it is the numerical guard for the
   division in `unrectify` when compositing (a `w` approaching 0 would be
   catastrophic and silent). It is a sanity bound, not a measured
   threshold — the same role `_FEATHER_FLOOR` plays.
3. **Improvement.** `1 − rms_after / rms_before ≥ _MIN_RELATIVE_IMPROVEMENT`.
   From the §0 measurement: real tilts improve 40–90%+, true zero recovers
   as zero with no improvement, and 0.15 sits between with margin.
   **Provisional**: chosen from the `scanny boy inputs` run; confirm
   against the user's R1 roll (`_DSC5129–5170`, on the camera card, not yet
   re-measurable) at the user gate before locking, the way
   `MAX_OVERLAP_MAD`'s semantics were re-measured.

`Rectification` (defined in `registration.py`, §4) carries `l`, `centre`,
`frame_size`, `rms_before_px`, `rms_after_px`, and the number of pairs that
fed the fit — everything the roll manifest's per-negative block (§7) records
and everything `composite.py` needs to build the inverse map.

---

## 4. `registration.py`: the closed-form core, and the two-pass flow

### 4.1 What registration.py gains

Closed-form NumPy only — no SciPy, no fitting:

```python
@dataclasses.dataclass(frozen=True)
class Rectification:
    l: np.ndarray                    # (2,), 1/px, centred coords
    centre: np.ndarray               # (2,), px
    frame_size: tuple[int, int]      # (height, width)
    rms_before_px: float
    rms_after_px: float
    pair_count: int

def rectify(points, rectification) -> np.ndarray          # §2, image → rectified
def unrectify(points, rectification) -> np.ndarray        # §2, rectified → image
def rectified_frame_corners(rectification) -> np.ndarray  # W at the 4 frame corners
def rectified_pairs(pairs, rectification) -> list[PairResult]
```

`rectified_pairs` re-fits each pair's rigid and similarity transforms with
the existing closed-form helpers on the rectified inlier points and returns
new `PairResult`s whose `transform`, `similarity_transform`,
`similarity_scale`, `rms_residual_px`, `scale_drift`, and
`inlier_points_a/b` all live in rectified space. Acceptance is carried over
unchanged: gates were evaluated on pass-1 pairs and are not re-evaluated
here (§4.2 re-registers under the right model instead, which is what makes
re-evaluation unnecessary). Post-rectification `scale_drift` collapsing
toward 0 is expected and is recorded per negative as fit evidence; the
`STITCH_SCALE_DRIFT` warning keeps its §2.5 meaning ("how much magnification
the pair carries").

`undistorter_from_geometry` is untouched. The rectifier is a second point
map that composes with it at the call site.

### 4.2 Two passes over `register_pair` — registration.py itself does not change

The chicken-and-egg: the fit needs pairs, and pairs should be registered in
rectified coordinates. `stitch_pipeline._attempt_solve` therefore registers
twice, reusing detection and matching output:

1. **Pass 1 — exactly today.** `register_pair(features[i], features[j],
   undistorter)` with the profile's undistorter or `None`.
2. **Fit.** `fit_rectification(accepted pass-1 pairs, frame_size)`.
3. **Pass 2 — only when the fit returns a `Rectification`.**
   `register_pair(features[i], features[j], composed)` where
   `composed(p) = rectify(undistort(p))` — the rectifier in the undistorter's
   existing optional-parameter slot. RANSAC, the rigid fit, the similarity
   fit, `rms_residual_px`, `scale_drift`, and every acceptance gate then
   operate on rectified points under the model that actually fits them.
   Inlier sets can legitimately change at the margins: pass-1's
   `RANSAC_REPROJ_PX = 3.0` threshold was absorbing systematic tilt residual
   of the same order. `solve_layout` then runs on the pass-2 pairs.

When the fit returns `None` there is no pass 2 and the flow is
byte-for-byte today's.

Cost: pass 2 re-runs the ratio test and RANSAC per pair — detection is not
repeated. Measured `match_seconds` roughly doubles for the negative; at
`DETECTION_LONG_EDGE = 2000` on the measured scans that is low single-digit
seconds per negative, inside the existing `PipelineStep.MATCH` budget — no
new progress steps, `_STEPS_PER_FRAME`/`_STEPS_PER_NEGATIVE` unchanged. If a
user gate ever objects, the named refactor is splitting `register_pair`'s
matching stage out so pass 2 reuses the `raw_matches`; not built now.

The CLAHE retry (`_solve_negative`) re-runs the whole attempt including both
passes; no interaction beyond that.

Determinism: pass 2 re-runs `register_pair` on identical descriptors with a
deterministic composed map, so results are reproducible; the fit itself is
deterministic per §2.

---

## 5. `layout.py`: the canvas is rectified space

`solve_layout` is otherwise untouched — it receives pass-2 `PairResult`s
whose points and transforms are already rectified, so the three linear
solves, `global_rms`, and the strip-axis SVD run unchanged and unmodified in
meaning. **SciPy still does not enter `layout.py`; the module docstring's
"never a homography" sentence is amended, not deleted** (§8).

What does change is the **frame footprint**. A placement matrix is affine,
but under rectification the frame's footprint in canvas space is the
*quad* `s·R·W(corner) + t`, not the affine image of the raw rectangle. With
measured `l` the divergence is small (a few px at the frame corners) but
must be handled exactly in the three places that transform corners:

- `solve_layout`'s canvas-bounds block (the `corners_local @ rotation.T +
  translation` computation),
- `largest_valid_rect`'s `fillConvexPoly` corners,
- `composite._frame_bbox` (§6).

Add one shared helper to `layout.py`:

```python
def frame_corners(
    placement: FramePlacement,
    frame_size: tuple[int, int],
    rectification: Rectification | None,
) -> np.ndarray:   # (4, 2) canvas-space corners
```

which maps the raw corners through `registration.rectify` first when a
rectification is present, then through the placement matrix — and is
exactly today's computation when it is `None`. `solve_layout` and
`largest_valid_rect` gain an optional `rectification` parameter (default
`None`), passed by `stitch_pipeline` from the fit result. All three call
sites switch to the helper; with `rectification=None` their outputs are
bit-for-bit today's, which is the regression test.

`Layout` itself needs no new field — the rectification is not part of the
placement data, it is the space the placements live in, and it reaches the
composite through `stitch_pipeline` the same way `geometry` does.

---

## 6. `composite.py`: the W⁻¹ slot in the band map

### 6.1 The composed map

`_warp_bands`'s per-band recipe (`docs/GEOMETRIC_PLAN.md` §5.3) gains one
closed-form step between steps 1 and 2:

```
1.  q = R⁻¹ · ([u, v] − t) / s      # bbox output px → rectified frame px
1.5 qc = q − centre                 # centred rectified px
    w  = 1 − l·qc                   # the unrectify weight
    p  = centre + qc / w            # → undistorted frame px
2.  x = (p.x − cx) / fx             # normalise          (unchanged)
3.  CA, "maps" mode only            (unchanged)
4.  forward radial distortion       (unchanged)
5.  denormalise → source frame px   (unchanged)
6.  cv2.remap                       (unchanged)
```

Step 1.5 is one weight and one divide per band pixel — no new interpolation
pass, no new memory term. `w` is bounded away from 0 by the excursion gate
(§3.2). The validity mask keeps remapping with the green map at
`INTER_NEAREST`; erosion is unchanged.

`_warp_bands` gains an optional `rectification` parameter. When it is
present but `geometry` is `None` (the common case — the measured scans run
without a profile), steps 2–5 reduce to the identity and `map_x, map_y` are
`p` directly. The distortion steps stay conditional on `geometry` exactly as
the CA steps already are on `ca`.

### 6.2 Routing

`composite.composite` uses the plain `cv2.warpAffine` path only when
**neither** geometry nor rectification is present; any rectification routes
through `_warp_bands`, with or without a profile. `composite` gains the
`rectification` parameter (default `None`) and passes it through
`estimate_peak_bytes` → `run_stitch` supplies it per negative from the fit.

`estimate_peak_bytes` gains `rectification: bool = False`. When
rectification is active **without** geometry, the band-map terms that
geometry already accounts for (`3 * GEOMETRY_BAND_ROWS * bbox_width * 2 *
4`, plus the `"maps"`-mode source view term) now apply and must be added
under the same conditions; when geometry is already active there is no
additional term. `MEMORY_SAFETY_FACTOR` is unchanged; `concurrency.py`'s
per-worker budget is not re-measured, per the §1.4 precedent in
`docs/STITCH_QUALITY_PLAN.md` — but say so in the docstring, and update the
peak-estimate test the way that plan's §1.4 did.

`_frame_bbox` uses `layout.frame_corners` (§5) so the bbox covers the
keystone quad; with `rectification=None` it is today's bbox exactly.

---

## 7. Contract and manifest

- `stitch_pipeline._stitch_params` gains, unconditionally:
  `"rectification_model": "global-2-param"`,
  `"rectification_min_improvement": _MIN_RELATIVE_IMPROVEMENT`,
  `"rectification_max_excursion": _MAX_WEIGHT_EXCURSION`,
  `"rectification_min_accepted_pairs": _MIN_ACCEPTED_PAIRS`.
  Like `"layout_model"`, the model name records what was in force; whether a
  given negative actually carried a correction is per-negative below.
  `stitch_params` is a roll invariant: **existing rolls refuse new runs**
  (`ROLL_INVARIANT_MISMATCH` through the recorded mismatch) — the same
  documented breakage as the gain-normalization and geometry merges; start a
  new roll. There is no migration.
- `roll_manifest.NegativeRecord` gains
  `rectification: dict | None = None`, emitted in `to_dict` as
  `None` (fit rejected, negative failed before the fit, or pre-rectification
  build) or:

  ```json
  {"l": [3.1e-07, -3.8e-07], "centre": [3032.0, 2020.0],
   "frame_size": [4040, 6064],
   "rms_before_px": 1.41, "rms_after_px": 0.83,
   "relative_improvement": 0.41, "pair_count": 3}
  ```

  `l` is in 1/px, centred coordinates — self-describing with `centre` and
  `frame_size`, no focal length required to interpret. The per-pair records
  (`PairRecord.rms_residual_px`, `scale_drift`) keep their existing meaning
  and now carry pass-2 (rectified-space) values when a rectification was
  applied; the before/after pair is what the rectification block is for.
- `shared/contract/roll-manifest.schema.json`: the negative definition gains
  the optional `rectification` object (properties as above; `l` exactly 2
  numbers; `relative_improvement` between 0 and 1). Not in `required` — a
  fit-rejected negative legitimately omits it.
- `roll_manifest.ROLL_MANIFEST_FORMAT_VERSION` **6 → 7** (and the schema's
  `const`). Update `manifest_schema_test_support.py` /
  `roll_manifest_schema_test_support.py`.
- **No event changes, no `events.PROTOCOL_VERSION` bump.** The
  rectification block lives in the roll manifest; `NegativeDone`'s payload
  (`global_rms_px`, `max_overlap_mad`) is unchanged and is now computed in
  rectified space when a correction is active — its meaning ("the honest
  measured residual") is unchanged. `CONTRACT.md` documents the manifest
  change the way format version 6 was documented.
- **Swift, minimal:** `RollManifest.swift` decodes negative fields
  tolerantly (`fields[...]?.doubleValue` style); add the `rectification`
  block as an optional decode so mixed-version rolls read cleanly, and
  surface "tilt corrected: yes/no" in the Edit tab's quality metrics if it
  falls out for free. Nothing in the app decides anything from it — Python
  owns every decision.

---

## 8. Documentation

**The `DECISIONS.md` amendment — write it before the code lands** (the
§2.6 rule of `docs/STITCH_QUALITY_PLAN.md`). Add under "Registration
model":

> **Amendment (roll manifest format version 7): the stitch stage rectifies
> a measured rig tilt.** The pairwise fit is still rigid and the layout is
> still a similarity — both unchanged, both still what the acceptance gates
> measure. Before the layout solves, the stitch stage fits one rectifying
> homography `W = [[1,0,0],[0,1,0],[l1,l2,1]]` per negative, shared by
> every pair, from the accepted pairs' own inliers — two parameters,
> `scipy.optimize.least_squares` with each pair's similarity re-fit in
> closed form inside the residual. If it passes its acceptance gates
> (support, plausibility, measured improvement), all downstream geometry
> works in `W`-rectified coordinates and the canvas is rectified space.
> This is not a homographic placement: no pair and no frame is ever placed
> by a homography. `W` is a measured property of the rig applied in the
> same slot as the radial undistortion — a re-parameterisation of image
> coordinates under which the inter-frame maps really are the similarities
> the layout already solves.
>
> Why: the film plane is not fronto-parallel — measured at −0.10° to
> −0.38° across the strip on every manually-shot negative examined
> (`scripts/measure-tilt.py`), varying between sessions and absent in a
> burst — so the true frame-to-frame map is a homography, and a similarity
> fitted to it leaves a systematic residual per pair that accumulates along
> a strip into visibly curved film edges. The per-pair homography
> alternative was measured and rejected: eight free parameters per pair,
> fitted from a thin overlap band and extrapolated across the frame, degrade
> as overlap narrows, where the two-parameter rig model holds. The residual
> a single global tilt does not explain (~0.2 px, per-pair film-height
> variation) is recorded in the manifest, not corrected.

Amend `layout.py`'s module docstring the same way (the layout is still a
similarity; the canvas may be rectified space; SciPy still forbidden), and
update the README's "the geometric model is deliberately simple" sentence
to name the rectification slot beside the undistortion it already
describes.

---

## 9. Tests

New `rectification_fit_test.py` (fast tier, synthetic correspondences — no
NEFs):

- A scene built with a known two-axis tilt and per-pair similarities is
  recovered: `l` to a tight tolerance, `rms_after` at the injected noise
  floor, `rms_before` far above it.
- True zero tilt is rejected by the improvement gate and returns `None` —
  the fit must not invent a tilt (the §0 synthetic result, pinned).
- Gate ordering: one accepted pair → `None`; improvement below
  `_MIN_RELATIVE_IMPROVEMENT` → `None`; an `l` whose excursion exceeds
  `_MAX_WEIGHT_EXCURSION` → `None`.
- The fit is deterministic and independent of the input pairs' order.

`registration_test.py`:

- `rectify`/`unrectify` round-trip to float tolerance; `rectified_pairs`
  re-fits transforms so every pair's `rms_residual_px` lands at the noise
  floor and `scale_drift` collapses toward 0; acceptance flags and
  `good_matches` are carried through unchanged.
- `register_pair` itself is untouched: the existing rigid-fit bit-for-bit
  assertions still hold.

`layout_test.py`:

- `frame_corners` with `rectification=None` is bit-for-bit today's corner
  computation; with a rectification, the canvas bounds contain the
  `W`-mapped quad.
- Order-independence of the solved layout still holds with pass-2-style
  (rectified) pairs and a rectification passed.

`composite_test.py`:

- A two-frame scene whose ground-truth inter-frame map is `W⁻¹·S·W`
  reconstructs sharply with the rectification and shows the familiar
  seam smear without it.
- The no-geometry + rectification routing produces the same geometry as a
  zero-coefficient geometry profile with the same rectification (two code
  paths, one answer).
- `estimate_peak_bytes` includes the band-map terms when rectification is
  active without geometry, and is unchanged when neither is present.
- Every existing composite test passes unchanged with
  `rectification=None` — the additive guarantee.

`roll_manifest_test.py` / schema support: round-trip a negative record with
and without `rectification`; the schema rejects a malformed block; version
constant bumped.

Slow tier (`--slow`; staged per `AGENTS.md` — probe/convert a
`stage_samples` directory holding the six appendix A files, never the whole
fixtures directory):

- Both appendix A negatives solve with the rectification accepted;
  `global_rms_px` improves against a control solve with the fit forced to
  reject (test-local monkeypatch of the improvement gate); record both
  numbers in the assertion message, gate nothing new.

---

## 10. Work order

Four chunks, each one branch and one pull request, each green on
`uv run ruff check .` and `uv run pytest` from `cli/` before the next
begins; `--slow` for the chunks that touch the composite and the wiring:

1. **The amendment and the fit** — `DECISIONS.md` §8 text first, then
   `registration.py`'s closed-form core, `rectification_fit.py`, tests.
   Nothing calls the fit yet; no behaviour change.
2. **The wiring** — `stitch_pipeline` two-pass flow, `layout.frame_corners`
   + the two call sites, manifest/schema/version bump, Swift optional
   decode, `CONTRACT.md`, README. Behaviour changes: rectified negatives
   now publish rectified canvases; regenerate any golden fixtures and say so
   in the commit message.
3. **The composite** — `_warp_bands` step 1.5 both paths, routing,
   `_frame_bbox`, `estimate_peak_bytes`, tests.
4. **The measurement** — re-run `scripts/measure-tilt.py` against a
   post-change stitch of the same scans; the shared-tilt model should now
   tie the per-pair homography on the *published* geometry, and the
   before/after `global_rms_px` numbers land in the PR description.

The user's R1 roll (`_DSC5129–5170`) is the acceptance target: its card was
not mounted during the §0 measurement, so `_MIN_RELATIVE_IMPROVEMENT` gets
its user-gate confirmation there.

## 11. Explicitly out of scope

- **Per-pair homographies** — anywhere: as placements (forbidden) or as a
  per-pair rectification (measured, rejected: §0).
- **The ~0.2 px per-pair residual** a single tilt does not explain (film
  height/settle between advances). It is recorded in the manifest block and
  left to the gates; modelling it is film-curl territory and no global
  homography can fix it.
- **Retuning any gate.** `MAX_PAIR_RMS_PX`, `SCALE_DRIFT_WARN/FAIL`, and
  `MAX_GLOBAL_RMS_PX` keep their numbers; as with the distortion landing
  (`docs/GEOMETRIC_PLAN.md` §5.3) they become loose for rectified negatives
  — record the before/after numbers, change nothing without a measurement
  the user approves.
- **Degrees, tilt direction labels, or hardware advice in production
  output.** `l` in 1/px is the record; interpretation is
  `scripts/measure-tilt.py`'s job.
- **Correcting at convert time.** Pre-warping intermediates would resample
  every frame an extra time and move flat-field's per-sensor-pixel gain map
  onto the wrong pixels — the same argument that put distortion in the
  stitch warp (`docs/DECISIONS.md`, "Geometric calibration").
- **A CLI flag to disable the fit.** Automatic with gates, like
  normalization; a test-local gate override is the only off switch.
