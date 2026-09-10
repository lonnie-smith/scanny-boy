# Narrow feather plan: one exponent on the separable ramp

One change to the compositing path: raise the feather's normalised ramp
product to a power before it is floored, so the crossfade between two
frames occupies a band around the overlap midline instead of the whole
overlap. Nothing about registration, layout, or the placement model
changes. This does not remove one pixel of misregistration — it stops the
misregistration that is already there from being painted across half the
picture.

This plan follows the conventions of `docs/STITCH_QUALITY_PLAN.md`,
`docs/GRID_STITCH_PLAN.md`, and `docs/RECTIFICATION_PLAN.md`: every
constant lives in exactly one module, every threshold that shapes an
output is recorded in the roll manifest so a record can be read without
knowing which build wrote it, and no threshold is changed without a
measurement the user has approved.

It is the promised revisit of `docs/STITCH_QUALITY_PLAN.md` section 1.5's
first deferred alternative ("a band around the overlap midline"), reached
by a route that does not need the pair overlap geometry the accumulate
pass declines to carry.

---

## 0. The measurement that motivates this

Measured on `_DSC5207` of the `Six7-after-headroom-adj` roll (4x2 grid,
8 frames, 6064x4040 into a 10137x11705 canvas), from the library record
and from the solved placements replayed through `composite._feather_weight`:

**Registration is not the thing to fix first.** All 16 accepted pairs fit
at 1.27-1.94 px RMS with 55-3585 inliers at ratio 0.47-0.83. Detection and
matching are healthy. But `global_rms_px` is **3.70** — the same inlier
correspondences scored against the solved global placement instead of each
pair's own transform (`layout.global_rms`), so the comparison is exact.
The global solve costs 2.2x the median pairwise residual. No single
rigid + isotropic-scale layout satisfies all 16 pairs at once; the
residual is model error, and the rig-tilt rectification only recovers
23.6% of it (`rms_before_px` 3.13 -> `rms_after_px` 2.39), against
`rectification_fit.MIN_RELATIVE_IMPROVEMENT`'s note that real tilts
measure 40-90%.

Removing that residual is a placement-model question and is **out of scope
here** (section 8).

**How much of the image that residual is smeared across is a blend
question, and it is enormous.** Reducing the solved weights to a per-pixel
`balance = max_frame_weight / sum_of_weights` (1.0 = one frame owns the
pixel and no ghost is possible; 0.5 = a straight two-way average and the
ghost is at full strength):

| balance | share of covered canvas |
|---|---|
| 0.50-0.55 | 5.4% |
| 0.55-0.70 | 15.3% |
| 0.70-0.90 | 18.8% |
| 0.90-0.99 | 8.1% |
| 0.99-1.00 | 49.0% |

**42.9% of the image is more than 10% blended and 8.9% is within 0.05 of a
straight 50/50 average.** The ramp runs from a frame's own edge to its
centre, so the crossfade is as wide as the overlap — about 2400 px on this
geometry, with the 0.1-to-0.9 transition spanning 1920 px of it. That is
why 3.7 px of residual reads as soft doubling scattered across arbitrary
parts of the frame rather than as a seam you can point at.

Sampling 6000 patches and comparing high- to mid-frequency spectral energy
*within* each patch (a ratio, so subject matter cancels), near-50/50
regions run ~1.3 dB down in high frequencies against unblended ones.
Treat that as corroborating and not decisive: averaging two frames costs
~3 dB of grain variance even at perfect registration, so part of that dip
is expected regardless. The balance histogram is the load-bearing number.

---

## 1. What changes

### 1.1 The one line that matters

`composite._feather_weight` currently ends the two-axis path with

```python
weight = np.maximum(product, _FEATHER_FLOOR_FRACTION)
```

It becomes

```python
weight = np.maximum(product, _FEATHER_FLOOR_FRACTION)
np.power(weight, FEATHER_EXPONENT, out=weight)
```

with a new module constant beside `FEATHER`:

```python
# The exponent applied to the normalised ramp product, narrowing the
# crossfade to a band around the overlap midline. **Unmeasured starting
# value** (section 6): 1 reproduces the pre-existing full-extent ramp
# exactly. Recorded in the roll manifest's stitch params as
# `feather_exponent`.
FEATHER_EXPONENT = 4
```

### 1.2 Why an exponent, and why it is the right knob

Take two frames overlapping along one axis, and let `u` run 0 to 1 across
the overlap. The two ramps are proportional to `1 - u` and `u`, so the
normalised contribution of the first frame is

```
w(u) = (1-u)^p / ((1-u)^p + u^p)
```

Three properties, all of which the plan depends on:

- **The crossover does not move.** `w(0.5) = 0.5` for every `p`. The seam
  stays exactly where the geometry puts it — the overlap midline — so no
  pair's blend is biased toward either frame and nothing about which frame
  "wins" a region changes.
- **The transition narrows as `1/p`, roughly.** The band where
  `0.1 < w < 0.9` has width `1 - 2/(1 + 9^(1/p))` in units of the overlap.
- **Separability survives exactly.** `(r_x * r_y)^p = r_x^p * r_y^p`, so
  the two-axis product stays a product of two independent per-axis ramps.
  The guarantee `docs/GRID_STITCH_PLAN.md` bought — that a pixel's
  crossfade across a vertical seam is the same at the top of the canvas as
  in the middle — is preserved for every `p`, because the ratio between
  two frames' weights along one axis is untouched by the other axis's
  ramp both before and after the power.

That third property is the reason this works without the pair overlap
geometry. `docs/STITCH_QUALITY_PLAN.md` section 1.5 rejected the
overlap-midline band because the accumulate pass does not carry which
pairs overlap where. The exponent needs none of that: it is a pointwise
function of a weight each frame already computes alone, and the midline
falls out of the arithmetic.

### 1.3 Predicted effect, on the real negative

Replaying `_DSC5207`'s solved placements through the powered weights:

| `p` | >10% blended | >25% blended | near 50/50 | 0.1-0.9 band |
|---|---|---|---|---|
| 1 (today) | 42.9% | 29.0% | 8.9% | 1920 px |
| 2 | 28.3% | 16.2% | 4.1% | 1200 px |
| 3 | 20.3% | 11.0% | 2.5% | 842 px |
| **4** | **15.7%** | **8.3%** | **1.8%** | **643 px** |
| 6 | 10.7% | 5.5% | 1.2% | 435 px |
| 8 | 8.1% | 4.2% | 0.8% | 328 px |

`p = 4` is the proposed starting value: it cuts the blended fraction by
nearly two thirds and the worst-case region by a factor of five, while
leaving a 643 px crossfade — still far too wide for a 3.7 px step to
register as an edge. Section 6 is the gate that confirms or moves it.

### 1.4 The floor, and why the power goes after it

`np.maximum` **must** run before `np.power`, not after. The floor's
purpose is the invariant *covered implies weight > 0*; its side effect is
that wherever two frames both sit on the floor their weights tie and the
blend goes 50/50 there — the same collapse `docs/STITCH_QUALITY_PLAN.md`
section 1 removed from the borders. Flooring first keeps the floored
region **byte-identically the same set of pixels it is today**, for every
`p`: the predicate is `product < _FEATHER_FLOOR_FRACTION`, which the power
never sees. Powering first would instead floor wherever
`product < FLOOR^(1/p)` — at `p = 4` that is `product < 0.18`, which is a
large fraction of every frame, and it would reintroduce the exact defect
this feather exists to prevent.

The cost is that the floored region's weight becomes `FLOOR^p`. In
float32 that bounds the exponent: with `_FEATHER_FLOOR_FRACTION = 1e-3`,
`p = 8` gives 1e-24 (a normal float32, comfortable), `p = 12` gives 1e-36
(normal, marginal), and `p = 13` gives 1e-39 — subnormal, and the
invariant starts to erode. **`FEATHER_EXPONENT` is therefore bounded to
`1 <= p <= 8`**, asserted at module scope, with the arithmetic above in
the comment so the next reader who wants `p = 16` knows they must move the
floor first.

### 1.5 The one-axis path is unified onto the same formulation

`_axis_ramp` returns *pixel-valued* weights floored at `_FEATHER_FLOOR =
1.0` px, deliberately kept byte-identical to the pre-grid build's. A
pixel-valued ramp cannot be powered safely — a 3000 px ramp at `p = 8` is
6.5e27, and at `p = 12` it overflows float32 — and leaving the strip path
unpowered would mean the same defect gets fixed for grids and left in
place for strips, on two divergent code paths.

So `_axis_ramp` is folded into the two-axis formulation: divide by the
half-span, floor at `_FEATHER_FLOOR_FRACTION`, power, exactly as the
product path does. `_FEATHER_FLOOR` is deleted; `_feather_weight`'s
`len(axes) == 1` branch becomes the same code as the general case with one
factor in the product.

`rectification_fit.py`'s `MAX_WEIGHT_EXCURSION` comment names
`_FEATHER_FLOOR` as the constant whose role it shares ("a sanity bound,
not a measured threshold"). Deleting the constant leaves that comment
pointing at nothing — repoint it at `_FEATHER_FLOOR_FRACTION` in the same
chunk. It is the only reference to the name outside `composite.py`.

**This changes strip output pixels, and that is intended.** At `p = 1` the
unified path is not quite identical to today's: two frames of equal extent
share a half-span so the normalisation cancels in the ratio, but the floor
does not — today's floor is `max(ramp_px, 1.0)`, the unified one is
effectively `max(ramp_px, 1e-3 * half_span)`, about `max(ramp_px, 3.0)` on
this geometry. The difference is confined to a 3 px sliver at a frame's
along-axis extreme instead of a 1 px one. Say so in the docstring; do not
try to preserve byte-identity, and regenerate any golden fixture that
compares strip output.

### 1.6 Memory accounting does not change

`np.power(..., out=weight)` is in place on an array `np.maximum` has
already allocated, so no bbox-sized buffer is added and
`estimate_peak_bytes` is untouched. Say this explicitly in the commit
message — the reviewer's first question about a new array op in the
accumulate pass will be whether section 1.4 of `STITCH_QUALITY_PLAN.md`
needs revisiting, and the answer is no.

---

## 2. What is recorded

`stitch_pipeline._stitch_params` gains one key beside `feather`:

```python
"feather_exponent": composite_module.FEATHER_EXPONENT,
```

`FEATHER` itself stays `"axis-separable"` — the model is still a separable
product of per-axis ramps, and unifying the one-axis path makes that
string *more* accurate, not less.

**Existing rolls are invalidated, by the mechanism already in place.**
`stitch_params` is a roll invariant (`roll_manifest.check_roll_invariants`),
so any roll stitched by an earlier build refuses new runs with
`ROLL_INVARIANT_MISMATCH` — "this run's stitch settings differ from the
roll's". This is the correct outcome and needs no new code: output pixels
change, so a negative stitched before this lands and one stitched after
must not sit in the same roll. It is the same handling
`shared/contract/CONTRACT.md` records for roll manifest format version 7
("there is no migration, and the remedy is to delete the old roll
folders"). Do not add a forward shim and do not add a dedicated error
code; do write it into the contract's version note so the message is
explicable.

`stitch_params` is `{"type": "object"}` in
`shared/contract/roll-manifest.schema.json` with no enumerated keys, so
**no schema edit is required**. Bump `ROLL_MANIFEST_FORMAT_VERSION`
(8 -> 9), the schema's `manifest_format_version` const, and
`events.PROTOCOL_VERSION` anyway, and describe the new key in
`CONTRACT.md`'s version note: a reader must be able to tell a manifest
written before this change from one written after without consulting the
build, and an absent `feather_exponent` key is not a reliable signal on
its own.

---

## 3. Tests (`composite_test.py`)

Existing tests that must still pass unchanged in their assertions, even
where their numbers move:

- `test_reconstructs_a_known_scene`
- `test_reconstruction_is_order_independent`
- `test_feather_weights_sum_to_one_inside_coverage`
- `test_no_output_value_is_negative_or_clipped_high`
- `test_uncovered_pixels_are_exactly_fill_color`
- `test_feather_contribution_is_constant_across_the_strip` — this is the
  section 1.2 separability claim, and it must hold for the shipped
  exponent, not only for `p = 1`. Parametrise it over `p in (1, 2, 4, 8)`.

New:

- **The crossover does not move.** For a two-frame overlap, the position
  where the normalised contribution crosses 0.5 is the same for every
  `p in (1, 2, 4, 8)` to within a pixel. This is the property that makes
  the exponent safe; if it ever fails, the seam is being dragged.
- **The transition narrows monotonically.** Measure the width of the
  `0.1 < contribution < 0.9` band for `p in (1, 2, 4, 8)` and assert it is
  strictly decreasing, and that `p = 4` is at least 2.5x narrower than
  `p = 1`. Ties the shipped constant to the section 1.3 table.
- **The floored region is invariant to `p`.** Build a mask whose product
  goes below `_FEATHER_FLOOR_FRACTION` somewhere, and assert the set of
  pixels sitting exactly on the floor's image is identical for
  `p in (1, 4, 8)`. This is section 1.4's whole argument, and it is the
  regression that fires if someone ever reorders the `maximum` and the
  `power`.
- **`p = 1` reproduces the two-axis weights exactly.** Byte-identical to
  the current build for the grid path, so the exponent is provably the
  only behavioural change.
- **The exponent bound is enforced.** `FEATHER_EXPONENT` is an int in
  `[1, 8]`; assert it, so a later edit cannot quietly walk into the
  subnormal region section 1.4 bounds.
- **Separability under the power.** Two frames in the same grid row: the
  ratio of their weights along the seam axis is the same at the top row of
  the canvas, the middle, and the bottom, for `p in (1, 4, 8)`. The
  two-axis analogue of the strip test above.

`layout_test.py` needs no change — `Layout.feather_axes()` is untouched.

---

## 4. Documentation

- **`README.md`**, "How frames are registered and blended": the Blending
  paragraph currently describes the strip-axis ramp only and predates the
  grid work — it says "ramped along the strip axis only", which has not
  been true since the separable feather landed. Rewrite it for the
  separable product *and* the exponent in one pass, and move the
  overlap-midline band out of the "considered and set aside" list, since
  this is that idea arriving by another route. The hard seam and the
  multi-band Laplacian blend stay as the named deferred alternatives.
- **`docs/DECISIONS.md`**: amend the blending bullet under "Colour,
  resampling, and blending" (it too still says "ramped along the strip
  axis only"), and add a short section in the same style as "The feather
  is a separable product of two ramps" recording *why* an exponent rather
  than a real midline band — section 1.2's third property is the whole
  argument and it belongs where the next reader will look for it.
- **`docs/STITCH_QUALITY_PLAN.md`** section 1.5: mark the first bullet as
  delivered, pointing here. Do not rewrite the section — it is the record
  of what was decided at the time.
- **`shared/contract/CONTRACT.md`**: the version note described in
  section 2.

---

## 5. Chunks

Each is independently reviewable and leaves the tree green.

1. **The weight** (sections 1.1, 1.4, 1.5) — `composite.py`:
   `FEATHER_EXPONENT`, its bound, the reordered floor-then-power, the
   `_axis_ramp` unification, `_FEATHER_FLOOR` deleted, docstrings. Tests
   from section 3. Regenerate golden fixtures and say so in the commit
   message.
2. **The record** (section 2) — `_stitch_params`, the three version bumps,
   the schema const, `CONTRACT.md`.
3. **The prose** (section 4) — `README.md`, `DECISIONS.md`,
   `STITCH_QUALITY_PLAN.md`. *Write the `DECISIONS.md` amendment first,
   not last.*

Chunk 1 changes output pixels; chunk 2 changes the manifest. There is no
ordering constraint between 1 and 2 beyond landing both before any roll is
stitched for the section 6 gate.

---

## 6. The measurement gate

`FEATHER_EXPONENT` is an **unmeasured starting value** and must be treated
like `GRID_PITCH_RATIO_MIN`, `GRID_ALIGNMENT_RATIO_MAX`, and
`_FEATHER_FLOOR_FRACTION`: recorded in `stitch_params`, listed in
`DECISIONS.md`'s "Unmeasured constants awaiting a real-scan gate", and
revisited at a user gate before it is treated as settled.

Add `scripts/measure-feather-exponent.py`, following
`scripts/measure-stitch-quality.py`'s discipline — **import the production
modules, never reimplement them**, and change nothing. For a given roll it
should, for each `p` in `(1, 2, 3, 4, 6, 8)`:

- Composite each negative at that exponent and write the TIFF to a
  scratch directory.
- Report the balance histogram of section 0 (the prediction is cheap and
  needs no pixels — it is a function of the solved placements alone, so it
  can be reported for every `p` in one pass).
- **Do not expect `overlap_mad` to move, and do not use it as the gate.**
  `composite._pair_overlap` compares the two *warped frames* directly, over
  the intersection of their bounding boxes, before any weight is applied —
  it is blind to the feather by construction. Confirmed empirically: the
  `Six7-after-feather-adjustment` roll reproduces the pre-change roll's
  `overlap_mad` to four decimal places. It stays a useful per-pair
  alignment measurement, but it measures registration, not blending.
- Report the high-to-mid frequency energy ratio in near-50/50 regions
  against unblended regions, as section 0 measured it. The ~1.3 dB deficit
  should shrink toward the ~0 dB that perfect registration plus grain
  averaging alone would predict.

**What the gate decides.** The exponent that minimises visible doubling
without making the seam itself visible. There is a real ceiling here: at
some `p` the 3.7 px step stops being spread and starts being a line, and
that is the point past which the honest fix is a placement-model change or
a seam cut, not more exponent. Expect the answer to sit between 3 and 6.
Do not ship a value the user has not looked at 100% crops of.

`_DSC5207` is the right test negative — it is the worst of its roll on
every metric (`global_rms_px` 3.70 against 2.89 and 2.43; tilt improvement
23.6% against 53.6% and 31.5%) and it is where the doubling was first
noticed.

---

## 7. Rejected alternatives

- **A true overlap-midline band.** What
  `docs/STITCH_QUALITY_PLAN.md` section 1.5 named. Rejected for the same
  reason it was rejected then — the accumulate pass does not carry the
  pair overlap geometry — and now additionally because the exponent
  reaches the same place without it. Section 1.2 shows the midline is
  already where the crossover sits; a band would only be re-deriving it
  from data the compositor would have to be taught to carry.
- **A smoothstep or logistic on the normalised ramp instead of a power.**
  Gives a genuinely bounded band rather than an asymptotic one, which is
  slightly nicer, but it does not survive the product: `f(r_x * r_y)` is
  not `f(r_x) * f(r_y)` for any `f` except a power, so separability — the
  property section 1.2 leans on and `GRID_STITCH_PLAN.md` paid for — would
  be lost. The power is not merely convenient here; it is the only
  pointwise function that keeps the feather separable.
- **Applying the exponent only to the grid path.** Leaves the identical
  defect in place for strips and creates two divergent formulations of one
  idea. Section 1.5 takes the pixel cost instead.
- **Raising `_FEATHER_FLOOR_FRACTION` so larger exponents stay
  representable.** Widens the floored region, which is the 50/50 collapse.
  If `p > 8` is ever wanted, the floor is the thing to redesign, and that
  is its own plan.
- **Multi-band (Laplacian) blending.** Still deferred, still for the same
  reasons: it hides misalignment at the cost of grain and a much heavier
  compositing stage. Narrowing a linear feather is the cheaper experiment
  and it is reversible by setting one constant to 1.

---

## 8. Explicitly out of scope

- **Anything that reduces `global_rms_px`.** The section 0 measurement
  says the residual is model error — pairwise fits at 1.7 px that no
  rigid + isotropic-scale layout can reconcile below 3.7 px. Fixing that
  means a richer placement model (per-frame homography, or a local
  non-rigid correction over the overlaps) and is a change to
  `docs/STITCH_QUALITY_PLAN.md` section 6's standing exclusions. It is the
  more important problem and it is not this plan.
- **Tightening `MAX_GLOBAL_RMS_PX`, `MAX_PAIR_RMS_PX`, or
  `RANSAC_REPROJ_PX`.** All three passed comfortably on a negative with
  visible doubling, so they are miscalibrated for this defect — but moving
  a measured gate needs its own measurement and its own user gate.
- **A `global_rms_px / median pairwise rms` model-mismatch gate.** The
  ratio (2.2 on `_DSC5207`) is the number that actually diagnosed this and
  it is nearly free to compute from data already in the manifest. Worth
  doing; not here.
- **A seam cut.** Stays the named next step if the section 6 gate finds
  that the narrowed transition shows a step small enough to cut through
  rather than fade.
- **Per-pair ECC or any other refinement of the pairwise fits.** They are
  already at 1.27-1.94 px with thousands of inliers. Polishing them cannot
  help, because the error is in reconciling them, not in any one of them.
- **Re-fitting the rectification, or reopening the calibration profile.**

---

## 9. Risks and known failure modes

- **The exponent trades a wide soft defect for a narrow sharp one.** This
  is the point of the change, and it is also the way it can go wrong: 3.7
  px of residual concentrated into a 643 px band is more locally visible
  per pixel than the same residual spread over 1920. The section 6 gate
  exists precisely to find where that trade turns. If no exponent is
  acceptable, that is a real result and it argues directly for the
  placement-model work in section 8.
- **Grain texture becomes spatially non-uniform in a new way.** A wide
  feather costs ~3 dB of grain variance across 43% of the frame; a narrow
  one costs the same 3 dB across 16%. The transition between blended and
  unblended grain gets *sharper* even as it gets rarer. Watch for it in
  smooth areas — sky is where it will show, and sky is most of these
  frames.
- **No recorded metric measures this change.** `overlap_mad` is blind to
  the feather (section 6), `global_rms_px` and the per-pair residuals are
  fixed before compositing runs, and nothing else in the manifest looks at
  blend weights. The balance histogram of section 0 is a prediction from
  the solved placements, not a measurement of output. **The gate is
  therefore the user's eyes on 100% crops**, and that is a real weakness:
  a regression here would not be caught by the test suite or by any number
  the pipeline records. Adding a recorded blend-balance summary per
  negative would fix that and is worth considering alongside chunk 2.
- **Golden fixtures.** Both the strip and grid paths change output.
  Anything comparing stitched pixels byte-for-byte must be regenerated,
  and the commit message must say so — otherwise the next bisect blames
  this change for a fixture drift it merely revealed.
