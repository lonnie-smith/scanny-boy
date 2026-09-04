# Stitch quality plan: feather, per-frame scale, weighted layout

Three changes to the registration/layout/compositing path, in order, that
together remove the curved, smeared borders seen in stitched negatives.
They follow the geometric distortion correction already landed on
`feat/distortion-calibration` (`docs/GEOMETRIC_PLAN.md`, commit 6b24f72),
which is **step 1 and is done**. Nothing here may be started before that
correction is in place: every step below either measures or models a
residual that distortion was previously masquerading as.

This plan follows the conventions of `docs/GEOMETRIC_PLAN.md` and
`docs/FLATFIELD_PLAN.md`: every constant lives in exactly one module,
every threshold that shapes an output is recorded in the roll manifest so
a record can be read without knowing which build wrote it, and no
threshold is changed without a measurement the user has approved.

---

## 0. Why these three, in this order

The visible defect is a soft, curved band of doubled/smeared detail near
the long borders of a stitched strip. Three separate causes stack:

1. **Radial distortion** bent straight film edges and put a
   position-dependent error into every correspondence. *Fixed in step 1.*
2. **The feather is isotropic.** `cv2.distanceTransform` weights a pixel
   by its distance to the *nearest* edge of the frame's own eroded mask.
   Near the strip's long borders that nearest edge is the border, not the
   seam, so both frames' weights collapse together and the blend goes to
   50/50 — averaging two slightly misregistered copies of the same
   detail into a smear whose width grows toward the border. That is the
   curve. (§1)
3. **Scale is forced to exactly 1.** Film sits at a slightly different
   height above the stage frame to frame, so a strip is not one magnification.
   With scale locked, that mismatch is absorbed into rotation and
   translation, which is exactly the error that a 50/50 feather then hides
   rather than shows. (§2)

Step 3 (§3, row weighting) is a small statistical correction that makes the
solve in §2 honest.

**Ordering is mandatory.** §1 before §2, because §1 is what makes residual
misregistration *visible* as a step you can measure — without it, §2's
improvement is unfalsifiable. §2 before §3 (§3 weights rows §2 introduces).

---

## 1. Feather along the strip axis only

### 1.1 What changes

`composite.composite` currently builds a frame's blend weight as

```python
pair_weight = cv2.distanceTransform(eroded_mask, cv2.DIST_L2, 5)
```

Replace it with a one-dimensional ramp along the **strip axis**: the
distance, measured only along the direction the film strip runs, from the
frame's own along-axis extent. Across the strip (the short axis) the weight
is constant, so a pixel at the top border of the canvas gets exactly the
same crossfade profile as a pixel down the middle. The 50/50 collapse at
the borders disappears, and residual misregistration shows up as a step at
the seam — visible, and measurable by `overlap_mad`.

### 1.2 `layout.py`: publish the strip axis

`strip_spread_ratio` already computes the SVD of the mean-subtracted placed
frame centres. Its first right-singular vector *is* the strip axis. Refactor
so that one SVD produces both, and add the axis to `Layout`:

```python
@dataclasses.dataclass(frozen=True)
class Layout:
    ...
    strip_spread_ratio: float
    strip_axis: tuple[float, float] | None   # unit vector, canvas space
```

Return `None` when the axis is not defined or not trustworthy:

- fewer than two placements, or
- the largest singular value is 0 (coincident centres), or
- `strip_spread_ratio > STRIP_SPREAD_RATIO` — the layout is not
  strip-shaped, the warning for which already fires, and an axis fitted to
  a blob would feather along an arbitrary direction.

The weight formula below is symmetric under a sign flip of the axis, so no
sign canonicalisation is needed; say so in the docstring, because the next
reader will worry about it. (Order-independence of the solved layout is
asserted by `layout_test.py`; the axis must not break it.)

### 1.3 `composite.py`: the ramp

New module constants, beside `MASK_ERODE_PX`:

```python
FEATHER = "strip-axis"   # recorded in the roll manifest's stitch params
_FEATHER_FLOOR = 1.0     # px; every covered pixel keeps a positive weight
```

`_FEATHER_FLOOR` is a numerical guard, not a measured threshold — the same
role `_MIN_CHANNEL_MEAN` plays in `solve_gains`. `cv2.distanceTransform`
never returns less than 1.0 inside a mask, so this preserves the existing
invariant that *covered implies weight > 0*; without it, a pixel at a
frame's along-axis extreme that no other frame covers would land on
`FILL_COLOR`.

```python
def _feather_weight(mask, bbox_x, bbox_y, axis):
    """Blend weight for one warped frame, in its own bounding box.

    With a strip axis, weight ramps only along that axis: the distance from
    the nearer end of this frame's own along-axis extent, floored so a
    covered pixel always contributes. Constant across the strip, so the
    crossfade at the strip's long borders is identical to the crossfade
    down its middle — the isotropic distance transform's border collapse to
    50/50 is what smeared misregistration into a curve. Without an axis
    (a layout that is not a strip), falls back to the distance transform.
    """
    if axis is None:
        return cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    ax, ay = axis
    height, width = mask.shape
    s = ((np.arange(width, dtype=np.float32) + bbox_x) * ax)[np.newaxis, :]
    s = s + ((np.arange(height, dtype=np.float32) + bbox_y) * ay)[:, np.newaxis]
    covered = mask > 0
    if not covered.any():
        return np.zeros(mask.shape, dtype=np.float32)
    s_min = float(s[covered].min())
    s_max = float(s[covered].max())
    weight = np.maximum(np.minimum(s - s_min, s_max - s), _FEATHER_FLOOR)
    weight[~covered] = 0.0
    return weight.astype(np.float32)
```

`composite` passes `layout.strip_axis` and the frame's `bbox_x, bbox_y`.
Nothing else in the accumulate pass changes: the output is still the
weighted average, still normalised by the weight canvas.

### 1.4 Memory accounting

`estimate_peak_bytes` must grow by the ramp's scratch — two bbox-sized
float32 buffers (`s`, and the `minimum`/`maximum` temporary), live for one
frame at a time, so this is **one** additive term, not `frame_count` of
them:

```python
feather_scratch = bbox_pixels * 4 * 2
```

added inside the `max(...)`'s first branch alongside `geometry_bytes`, with
`MEMORY_SAFETY_FACTOR` unchanged. Update the docstring and
`composite_test.test_peak_estimate_counts_the_source_frame_and_the_safety_factor`.

### 1.5 Alternatives considered

Recorded because `docs/DECISIONS.md` and the README already name the
alternatives and will be amended (§1.7):

- **A band around the overlap midline.** Narrower still, and makes a step
  even easier to see. Rejected as the default because it needs the pair's
  overlap geometry at blend time, which the accumulate pass does not
  currently carry, and the strip-axis ramp already removes the border
  collapse for a fraction of the code.
- **A hard seam.** Preserves grain exactly. Kept in the README as the
  named next step if a measured step at the seam turns out to be small
  enough to cut through rather than fade.

### 1.6 Tests (`composite_test.py`)

- Existing `test_reconstructs_a_known_scene`,
  `test_reconstruction_is_order_independent`,
  `test_feather_weights_sum_to_one_inside_coverage`,
  `test_no_output_value_is_negative_or_clipped_high`, and
  `test_uncovered_pixels_are_exactly_fill_color` must all still pass. If
  the sum-to-one test asserts anything about the *shape* of the weight, fix
  the test, not the ramp.
- **New:** the normalised contribution of frame A at a fixed along-axis
  position is equal (to within float tolerance) at the top row, the middle
  row, and the bottom row of the overlap. This is the regression that
  fails on the old distance transform and is the whole point of the change.
- **New:** a two-frame scene with a deliberate 3 px translational
  misregistration produces a measurable step across the seam rather than a
  widening blur — assert the width of the transition region is bounded and
  does not grow toward the canvas border.
- **New:** `strip_axis is None` (single frame, coincident centres, or a
  non-strip layout) reproduces the distance-transform weights exactly.

### 1.7 Documentation

- `README.md` "How frames are registered and blended": rewrite the
  **Blending** paragraph. The existing text already calls the isotropic
  feather "deliberate, but provisional"; this is the promised revisit, so
  it is an update, not a contradiction.
- `docs/DECISIONS.md` "Colour, resampling, and blending": amend the
  blending bullet to describe the strip-axis ramp, keeping the hard seam
  and the multi-band blend as the named alternatives.
- `stitch_pipeline._stitch_params`: add `"feather": FEATHER`.

---

## 2. A per-frame scale in the layout solve

**This contradicts a locked decision and requires a `DECISIONS.md`
amendment (§2.6) written before the code lands.** `docs/DECISIONS.md`
"Registration model" and the README both state that scale is fixed at
exactly 1.

### 2.1 The model

Frame *i* places its own pixel `p` into canvas space as

```
x = s_i · R(theta_i) · p + t_i
```

A pair (a, b) contributes a **similarity**: `p_a = sigma_ab · R(phi_ab) · p_b + u_ab`.
Requiring the two routes into canvas space to agree gives three relations,
each linear in the right variable:

| unknown | relation | rhs |
| --- | --- | --- |
| `log s` | `log s_b - log s_a = log sigma_ab` | `log sigma_ab` |
| `theta` | `theta_b - theta_a = phi_ab` | `phi_ab` |
| `t` | `t_b - t_a = s_a · R(theta_a) · u_ab` | `s_a · R(theta_a) · u_ab` |

So it stays **three linear least-squares problems solved in order — scales,
then rotations, then translations** — and the translation step changes by
exactly one scalar factor. The log-scale system with an all-ones anchor row
at rhs 0 is structurally identical to `solve_gains`; copy that idiom,
including the geometric-mean-1 gauge, so no frame's magnification is
privileged. **SciPy stays out of `layout.py`.** (SciPy is now a runtime
dependency for the plumb-line calibration fit; that does not license a
nonlinear bundle adjustment here, and `layout.py`'s docstring forbids it.)

The model is a **similarity** — rigid plus one isotropic scale. It is still
not an affine and still not a homography; the amendment must say so.

### 2.2 `registration.py`

`rigid_from_correspondences` stays exactly as it is, and so does everything
that consumes it. Add its sibling:

```python
def similarity_from_correspondences(src, dst) -> tuple[np.ndarray, float]:
    """Closed-form Umeyama *with* scale, from the same SVD as the rigid
    fit. Returns (2x3 [R|t], scale). The rigid fit stays the one the
    acceptance gates measure against, so no gate constant changes meaning
    when this is added."""
```

`s = trace(diag(singular_values) @ d) / mean(||src - mu_src||^2)`, using the
same `d = diag([1, sign(det(u @ vt))])` reflection guard; `t = mu_dst - s·R·mu_src`.

`PairResult` gains two fields, both fitted from the **inliers only**, like
the rigid transform (not read off the RANSAC model matrix):

```python
similarity_transform: np.ndarray  # 2x3, rotation+translation, maps b -> a
similarity_scale: float
```

`transform`, `rms_residual_px`, and `scale_drift` keep their current
meaning and their current sources. Every early-return path in
`register_pair` must populate the two new fields (identity / 1.0 on the
failure paths) — there are five such returns; miss one and the dataclass
construction fails loudly, which is the desired behaviour.

### 2.3 `layout.py`

- `FramePlacement` gains `scale: float`; `matrix()` returns
  `hstack([scale * R, t])`. Everything downstream that reads
  `matrix[:, :2]` — `global_rms`, `strip_spread_ratio`,
  `largest_valid_rect`, `composite._frame_bbox`, `composite._warp_bands`'s
  `np.linalg.inv` — is already written against a general 2x2 block and
  needs **no change**. In `_warp_bands`, rename the local `R_inv` and fix
  the comment: it is now the inverse of a scaled rotation, not a rotation.
- `solve_layout` gains step 0, the log-scale solve, before the rotation
  solve; uses `pair.similarity_scale` for `sigma_ab`,
  `_angle_deg(pair.similarity_transform[:, :2])` for `phi_ab`, and
  `pair.similarity_transform[:, 2]` for `u_ab`.
- The translation rhs becomes `scales[index[pair.a]] * (rotation_a @ u_ab)`.
- `shifted_placements` must carry `scale` through — the canvas-origin shift
  touches translation only.
- Guard `sigma_ab <= 0` the way `solve_gains` guards a degenerate channel
  mean: drop the row, do not raise. A non-positive similarity scale means a
  reflected fit, which the reflection guard should already have excluded.

### 2.4 Data model and contract

- `roll_manifest.FrameRecord` gains `scale: float`, emitted in `to_dict`.
- `shared/contract/roll-manifest.schema.json`: add
  `"scale": { "type": "number", "exclusiveMinimum": 0 }` to the `frame`
  definition and to its `required` list.
- Bump `roll_manifest.ROLL_MANIFEST_FORMAT_VERSION` 5 → 6 (and the schema's
  `const`), and `events.PROTOCOL_VERSION` 7 → 8. Update
  `shared/contract/CONTRACT.md` the way protocol 7 was documented, and the
  Swift-side protocol-version constant (see commits ce3179a / be318e5,
  "Centralize protocol_version literal in Swift tests").
- **Drift spotted while planning, confirm with the user before touching:**
  `CONTRACT.md:264` and `CONTRACT.md:393` still say roll record "format
  version 4" while `roll_manifest.py:47` says 5. Fix it in the same pass if
  the user agrees it is stale documentation; do not silently change a
  documented contract number on your own judgement.
- `stitch_pipeline._stitch_params`: add `"layout_model": "similarity"` so a
  manifest written before and after this change is distinguishable without
  consulting the build.
- `stitch_pipeline` `record.frames`: pass `scale=placement.scale`.

### 2.5 The scale-drift gate

`SCALE_DRIFT_FAIL = 0.01` currently rejects a pair whose similarity scale
differs from 1 by more than 1%, on the grounds that under a scale-1 model
such a pair must be a bad fit. Under the new model that is no longer true:
those are exactly the pairs the layout can now represent.

For this step, **do not change the numbers.** Change only what they mean
and how they read:

- keep `SCALE_DRIFT_FAIL` as a plausibility gate — 1% is still far more
  magnification change than film on a stage can produce between frames, so
  a pair above it is still a bad fit;
- reword the `STITCH_SCALE_DRIFT` warning in `stitch_pipeline.py` so it no
  longer implies "this should be 1"; it now reports how much
  magnification the pair carries.

### 2.6 The `DECISIONS.md` amendment

Do not edit the "Registration model" section's existing text in place; the
file's convention (see "Amendments to the Phase 1 plan") is to record what
changed and why. Add a subsection under "Registration model":

> **Amendment (protocol version 8): the layout solves a per-frame scale.**
> The pairwise fit still produces a rigid transform, and that rigid fit is
> still what the acceptance gates measure. The *global layout* now places
> each frame with a similarity — rotation, translation, and one isotropic
> scale — solved from pairwise similarity scales as a log-space linear
> least-squares problem with a geometric-mean-1 anchor, structurally
> identical to `solve_gains`. Still three linear solves, still no SciPy in
> `layout.py`, still never an affine and never a homography.
>
> Why: film does not sit at a constant height above the stage, so a strip
> is not one magnification. With scale locked at 1 that mismatch was
> absorbed into rotation and translation, where it surfaced as residual
> misregistration at frame borders — the error the isotropic feather was
> hiding rather than showing. It could not be modelled honestly before
> radial distortion was corrected, because distortion produced a
> position-dependent apparent scale that a per-frame constant would have
> fitted wrongly.

Update the README's "the geometric model is deliberately simple: rigid
rotation plus translation, scale fixed at exactly 1" sentence to match.

### 2.7 Tests

`layout_test.py`:
- A synthetic scene built with known per-frame scales is recovered to a
  tight tolerance, and the solved scales have geometric mean 1.
- A scene with no scale variation still solves scales ≈ 1 and produces the
  placements the current code produces — the regression that proves this is
  additive.
- The existing order-independence assertion still holds, now including
  `scale`.
- Canvas size for a scaled layout matches the transformed corners.

`registration_test.py`:
- `similarity_from_correspondences` recovers a known scale, rotation, and
  translation, including the reflection guard.
- `rigid_from_correspondences` output is bit-for-bit unchanged.

`roll_manifest_test.py` / schema support: round-trip with `scale`, and the
schema rejects a frame record missing it.

---

## 3. Weight the layout rows

Mirror `solve_gains`, which already weights each row by `sqrt(shared_count)`
because a mean over N pixels has variance ∝ 1/N. A pairwise transform
estimated from N inliers with residual `rms` has parameter variance
∝ `rms² / N`, so the natural row weight is `sqrt(N) / rms`.

In `layout.py`:

```python
# Row weight for the layout solves: a pairwise transform from N inliers at
# residual `rms` has parameter variance proportional to rms^2 / N. The floor
# is a numerical guard, not a measured threshold — a synthetic fixture can
# fit to essentially zero residual, which without it would give one pair
# unbounded authority over the solve.
RMS_WEIGHT_FLOOR_PX = 0.1


def _row_weight(pair: PairResult) -> float:
    return math.sqrt(pair.inliers) / max(pair.rms_residual_px, RMS_WEIGHT_FLOOR_PX)
```

Apply it to all three systems: multiply the row and its rhs by the weight
(both the x and y rows of a translation pair get the same weight).

**The gauge anchors must be re-weighted to match.** `solve_layout`'s anchor
rows currently carry weight 1.0 against unit data rows. With weighted data
rows in the hundreds, a unit anchor stops pinning the gauge and the whole
layout is free to drift and rotate slightly — not wrong, but not
deterministic either. Use `solve_gains`' idiom:

```python
anchor_weight = math.sqrt(sum(w * w for w in row_weights))
```

for the rotation anchor, both translation anchors, and the scale system's
all-ones anchor.

`global_rms` stays **unweighted**. It is the honest measured residual the
gate checks, not part of the estimator.

Record it: `_stitch_params` gains `"layout_row_weight": "sqrt(inliers)/rms"`
and `"rms_weight_floor_px": RMS_WEIGHT_FLOOR_PX`.

**Test:** a layout containing one accepted-but-weak pair (few inliers, high
residual) alongside several strong ones. The solved placements move less
from the strong pairs' consensus than they do with unweighted rows — assert
the weighted solution's `global_rms_px` over the strong pairs is lower.

---

## 5. Work order

Four commits (five if §2 is split), each green on `uv run ruff check .` and
`uv run pytest` from `cli/` before the next begins:

1. **Feather** (§1) — `layout.py`, `composite.py`, tests, README,
   `DECISIONS.md`. No schema change, no contract change.
2. **Per-frame scale, the solver** (§2.1–2.3, 2.7) — `registration.py`,
   `layout.py`, `composite.py` comment fix, tests.
3. **Per-frame scale, the record** (§2.4–2.6) — manifest, schema, contract,
   protocol/format version bumps, Swift constant, `DECISIONS.md` amendment,
   README. *Write the amendment first, not last.*
4. **Weighted rows** (§3) — `layout.py`, tests, `_stitch_params`.

Steps 1–4 are behaviour changes to output pixels and to the manifest;
regenerate any golden fixtures the test suite compares against and say so
in the commit message.

## 6. Explicitly out of scope

- Any nonlinear bundle adjustment, or SciPy anywhere in `layout.py`.
- Affine or homographic placement; anisotropic per-frame scale.
- Multi-band (Laplacian) blending, and the overlap-midline band and hard
  seam of §1.5 — named, deliberately deferred.
- Renaming the stale `--flatfield` flag (`DECISIONS.md` already carries this
  as a cosmetic follow-up).
- Re-fitting or re-shaping the calibration profile; §1's step 1 is done and
  this plan does not reopen it.
