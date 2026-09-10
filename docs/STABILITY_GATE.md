# Stability gate plan: accept a distortion fit that repeats, not one that flatters

One change to `geometry_fit.py`'s acceptance decision. The staged plumb-line
fit itself, the stages, the coefficients, the gauge, and every consumer of
the fitted geometry stay exactly as they are. What changes is the question
the gate asks:

- **Today:** *did undistorting visibly straighten held-out lines?*
- **Proposed:** *do independent subsets of the calibration frames agree on
  the same coefficient?*

The first question is only answerable when the residual floor is dominated
by error the correction can remove. On this rig it is dominated by the
target instead, and the gate cannot see a distortion the fit is in fact
recovering correctly.

This plan follows the conventions of `docs/STITCH_QUALITY_PLAN.md` and
`docs/NARROW_FEATHER.md`: every constant lives in exactly one module, every
threshold that shapes an output is recorded so a record can be read without
knowing which build wrote it, and no threshold is changed without a
measurement the user has approved.

---

## 0. The measurement that motivates this

### 0.1 The rejection that started it

The `35mm` profile's stored `calibration_report`:

```
distortion.accepted        false
corner_displacement_px     14.51        (0.398% of the half-diagonal)
heldout_rms_px_before      2.5372
heldout_rms_px_after       2.5378
rejection_reason           "held-out RMS improved 2.537px -> 2.538px (-0.0% relative...)"
corners_detected_median    32           (the 6x9 board offers 96)
```

Read plainly: the fit's own point estimate is **14.5 px of corner
displacement**, and the gate could not confirm it because the held-out
straightness floor is **2.54 px**. At the scanning rig's ~168 px/mm that
floor is **15 µm on the board** — printed-target accuracy, not lens error,
and not something undistortion can remove.

### 0.2 The estimate is right

An entirely independent measurement agrees with it. Fitting a single
frame-centred radial field **shared across all ten negatives** of the
`Six7-after-feather-adjustment` roll — 2 parameters, against 153,520 stitch
correspondences, touching no calibration target at all — gives **16.8 px**
(0.46%), and removes 35.9% of the roll's global registration residual.
Two instruments with no shared data, no shared objective and no shared
failure mode, landing 15% apart.

### 0.3 The gate cannot pass, at any board

Synthesising ChArUco detections at the real magnification, pushing them
through a known 15 px corner distortion, adding corner noise, and running
the production `collinear_sets` and `fit_geometry`:

| square | corners in frame | k1 recovered @0.5px | @1.0px | @2.5px |
|---|---|---|---|---|
| 4.0 mm (the `6x9` board) | 40 | 14.8 px | 14.8 px | 14.9 px |
| 2.0 mm | 187 | 14.9 px | 14.8 px | 14.5 px |
| 1.2 mm | 551 | 15.0 px | 15.0 px | 15.4 px |

Truth is 15.0. **Every one of those recovers the coefficient, and every one
is rejected.** What the gate actually measures moves like this:

| true corner | square | corner noise | held-out RMS | relative |
|---|---|---|---|---|
| 15 px | 4.0 mm | 2.5 px | 2.116 → 2.064 | 2.4% |
| 15 px | 2.0 mm | 2.5 px | 2.282 → 2.211 | 3.1% |
| 15 px | 2.0 mm | **0.5 px** | 0.707 → 0.442 | **37.4%** |

A five-fold increase in corner count moves the gate metric from 2.4% to
3.1%. A five-fold reduction in corner noise moves it to 37.4%. **The
binding constraint is target accuracy, not board density** — which is why
this is a gate problem and not a board problem, and why
`calibration/lens_calibration_targets.pdf` is worth printing but will not on its own
change the outcome.

### 0.4 Why straightness is the wrong instrument here

Radial displacement along a line is mostly absorbed by that line's own
best fit; only the curvature survives into a perpendicular residual. A
0.4% distortion therefore contributes a small fraction of a pixel to a
residual whose floor is 2.5 px of target error. The signal is real, the
estimator finds it, and the test is being asked to see it inside noise
forty times its size.

---

## 1. What changes

### 1.1 The statistic

Leave one calibration frame out, refit, and repeat over every frame. The
spread of the resulting corner displacements is the jackknife standard
error:

```
SE = sqrt( (n - 1) / n * sum_i (theta_i - theta_bar)^2 )
```

where `theta_i` is the corner displacement fitted with frame `i` held out.
The gate is on the **relative** standard error, `SE / theta_bar`.

Jackknife rather than bootstrap, for one reason that matters in this
codebase: it is deterministic. There is no seed, no resampling draw, and
two runs over the same frames return the same number — the same property
`layout_test.py` asserts for the layout solve and `rectification_fit.py`'s
docstring claims for the tilt fit.

The `(n-1)/n` factor is not decoration. Leave-one-out estimates are
strongly correlated, so the raw standard deviation of `theta_i` understates
the true spread by roughly `sqrt(n-1)`; without the factor the threshold
would silently depend on how many frames were shot.

### 1.2 Why this works where straightness does not

Target error is *random across frames* — a different imperfection at every
corner, every board position, every exposure. It inflates any single
frame's residual, but it averages out across subsets, so the fitted
coefficient sits still. Lens distortion is *fixed across frames* —
identical in every one. Agreement between subsets separates the two
without ever requiring the lens signal to dominate a single measurement.

That is the whole idea, and it is why the statistic is nearly independent
of the noise floor that defeats the current gate.

### 1.3 The signature has to carry frames

`fit_geometry(train_sets, heldout_sets, frame_width, frame_height)` takes
two **flat** lists of collinear sets. `calibration.py` builds them by
`extend`-ing one frame's sets at a time (`calibration.py` around line 385),
so frame identity exists at the call site and is discarded on the way in.
A jackknife needs it back.

Change the two parameters to `list[list[np.ndarray]]` — one inner list per
frame. `fit_geometry` flattens them for the existing staged fit, so the
fitted coefficients and both held-out RMS numbers stay **bit-identical**;
the grouping is used only by the new statistic. The call site becomes an
`append` instead of an `extend`.

This is the only structural change in the plan, and it is the reason
chunking matters (section 5): land the signature and prove the fit is
unchanged, then add the gate.

### 1.4 The decision

```python
accepted = (
    MAGNITUDE_HARD_MIN_PERCENT <= percent <= MAGNITUDE_HARD_MAX_PERCENT
    and relative_standard_error <= GEOMETRY_MAX_RELATIVE_SE
)
```

The improvement numbers are **still computed and still recorded** — they
are a genuine diagnostic, and on a good target they will be large. They
simply stop being the acceptance criterion. Anything that clears the old
improvement gate clears the stability gate too (section 6 confirms this
rather than assuming it), so this is a strict widening, not a swap.

`GEOMETRY_MIN_IMPROVEMENT_FRACTION` and `GEOMETRY_MIN_IMPROVEMENT_PX` stay
in the module as the reported diagnostic's constants. Do not delete them
and do not leave them referenced by the acceptance branch.

### 1.5 The magnitude band needs widening too

`MAGNITUDE_EXPECTED_MIN_PERCENT = 0.03` / `MAGNITUDE_EXPECTED_MAX_PERCENT
= 0.2` flags anything outside as `suspect`. This lens measures 0.398% by
ChArUco and 0.46% by the stitch correspondences, so it would be flagged
suspect on every future calibration — a warning that fires on the known
truth is a warning that gets ignored.

Raise `MAGNITUDE_EXPECTED_MAX_PERCENT` to **0.6**, which contains both
independent measurements with margin and stays well inside the 1.0% hard
bound. The hard bounds do not move.

### 1.6 Cost

The jackknife is `n` extra staged fits, and each staged fit is the three
`least_squares` solves the current call already does once — so calibration
gets roughly `n+1` times slower, with `n` the surviving frame count (16 on
the recorded run). Calibration is a deliberate, occasional operation, so
this is acceptable, but it must be **measured and reported in the commit
message**, not assumed. If it proves intolerable, the fallback is a
fixed-count jackknife over frame *groups* rather than individual frames;
do not reach for it before measuring.

---

## 2. What is recorded

`calibration.py`'s report block gains, beside the existing distortion keys:

```python
"jackknife_corner_px_mean": ...,
"jackknife_corner_px_se": ...,
"jackknife_relative_se": ...,
"jackknife_frames": ...,
```

and `_stitch_params`-style discipline applies: `GEOMETRY_MAX_RELATIVE_SE`
goes into the report so a stored profile records the threshold it was
judged against.

**No manifest or contract version changes.** `calibration_report` is a
profile-level diagnostic blob, not part of the roll manifest, and
`stitch_params`'s geometry bucket is excluded from the roll-invariant
comparison (`ROLL_PROFILE_STITCH_PARAMS_KEYS`) already. A profile
recalibrated under the new gate can be attached to an existing roll exactly
as one recalibrated under the old gate can.

**Existing profiles are unaffected and stay rejected.** Nothing
retroactively accepts a stored fit — `geometry` is null on both current
profiles and stays null until the user recalibrates. Say so in the CLI's
output rather than silently changing behaviour under them.

---

## 3. Tests (`geometry_fit_test.py`)

Existing tests must still pass; the fit is unchanged, so any test asserting
coefficients, stages, or held-out RMS values should need no edit. If one
does, that is a signal the flattening in section 1.3 is not faithful — fix
the code, not the test.

New:

- **The grouped signature does not move the fit.** Same frames fed as one
  flat group and as per-frame groups produce bit-identical `k1`, `k2`,
  `cx`, `cy`, `heldout_rms_before` and `heldout_rms_after`. This is the
  regression that protects section 1.3's claim.
- **Determinism.** Two calls on the same input return the same
  `jackknife_relative_se` exactly, and reordering the frames does not
  change it beyond float tolerance.
- **A real distortion is accepted.** Synthesised at the section 0.3
  conditions that the current gate rejects (4.0 mm board, 2.5 px corner
  noise, 15 px true displacement), the fit is accepted and the recovered
  displacement is within tolerance of truth.
- **No distortion is rejected.** Same conditions, `k1 = 0`: rejected, and
  for the stability reason rather than incidentally by the magnitude floor.
  Assert the rejection reason names the spread — a gate that passes this
  test only because `MAGNITUDE_HARD_MIN_PERCENT` caught it is not the gate
  this plan describes.
- **The old regime still passes.** Low corner noise, where the improvement
  gate would have accepted: the stability gate accepts too (section 1.4's
  widening claim).
- **`(n-1)/n` is applied.** The reported SE for a known set of leave-one-out
  estimates matches the closed-form jackknife SE, not their raw standard
  deviation. Cheap, and it pins the one piece of arithmetic that is easy to
  get quietly wrong.

---

## 4. Documentation

- **`docs/DECISIONS.md`**: a new section in the style of "The feather is a
  separable product of two ramps", recording *why* the acceptance question
  changed — section 0.4 is the argument and it belongs where the next
  reader will look. Record the two independent 14.5 px / 16.8 px
  measurements; they are what justify trusting a fit the old gate refused.
- **`README.md`**: the calibration paragraph, if it describes the
  acceptance rule.
- **`calibration/lens_calibration_targets.pdf`** and its generator
  (`cli/tools/generate_charuco_board.py`) are already in the tree; the
  DECISIONS entry should note that the finer board tightens the estimate
  (section 6) but was *not* what unblocked the gate, so nobody later
  concludes the board was the fix.

---

## 5. Chunks

1. **Grouped signature** (section 1.3) — `geometry_fit.fit_geometry`,
   `calibration.py`'s call site, plus the bit-identity test. No behaviour
   change; the fit is provably the same.
2. **The statistic and the gate** (sections 1.1, 1.4, 1.6) —
   `GEOMETRY_MAX_RELATIVE_SE`, the jackknife, the new acceptance branch,
   the tests. Measure and report the runtime change.
3. **The record and the band** (sections 1.5, 2) — report keys, the
   `MAGNITUDE_EXPECTED_MAX_PERCENT` move, `DECISIONS.md`. *Write the
   DECISIONS amendment first, not last.*

Chunk 1 must land before 2. Chunk 3 may land with either.

---

## 6. The measurement gate

`GEOMETRY_MAX_RELATIVE_SE` is an **unmeasured starting value** until this
runs, and must be listed in `DECISIONS.md`'s "Unmeasured constants awaiting
a real-scan gate" alongside `GRID_PITCH_RATIO_MIN` and friends.

Add `scripts/measure-stability-gate.py`, following
`scripts/measure-stitch-quality.py`'s discipline — **import the production
modules, never reimplement them**, and change nothing. It should sweep
true corner displacement (including **zero**) against board pitch and
corner noise, and report the jackknife relative SE for each cell, so the
threshold is chosen from a curve that shows both what it accepts and what
it rejects.

### 6.1 What the sweep already shows

Run ahead of the plan, on synthetic detections at the rig's magnification,
16 frames, 2.5 px corner noise (the rig's measured floor), jackknife
relative SE:

| true corner | 4.0 mm board (`6x9`) | 2.0 mm board |
|---|---|---|
| 0 px | 174.3% | 95.3% |
| 2 px | 77.5% | 23.4% |
| 5 px | 42.2% | 11.0% |
| **15 px** | **17.0%** | **4.0%** |
| 30 px | 8.9% | 2.0% |

The statistic separates cleanly where the improvement metric does not:
across the same range that moved the old gate from 2.4% to 3.1%, this moves
from 174% to 9%.

**Proposed starting value: `GEOMETRY_MAX_RELATIVE_SE = 0.25`.** It accepts
the rig's real distortion on both boards and rejects the null on both.

Two things the table says that the threshold alone does not:

- **On the current 4.0 mm board the margin is thin.** A genuine 15 px
  distortion sits at 17.0% against a 25% line. On the 2.0 mm board it sits
  at 4.0%. The finer target is not what unblocks the gate (section 0.3),
  but it is what gives the gate room to work — an independent reason to
  print `calibration/lens_calibration_targets.pdf`.
- **The estimator is biased upward by noise.** At true zero it returns
  1.60 px on the 4.0 mm board and 0.65 px on the 2.0 mm one, so a fitted
  displacement carries roughly that much noise-induced inflation. The
  recorded 14.5 px may therefore be nearer 13 px of real distortion —
  still consistent with the stitch correspondences' 16.8 px, and another
  reason to prefer the finer board. Record this in the report's prose, and
  do **not** attempt to debias the coefficient; the bias is smaller than
  the disagreement between the two independent measurements.



**What the gate decides.** The threshold that separates a real distortion
from none at the rig's actual corner noise. It must be justified by the
*separation* in that table, not by the value that happens to admit this
particular lens. A threshold that accepts everything is worse than the gate
being replaced.

Then, and only then, recalibrate against the printed 2 mm target and
confirm the accepted coefficient still lands near 15-17 px — the number two
independent methods already agree on.

---

## 7. Rejected alternatives

- **Keeping the improvement gate and lowering its threshold.** Moving 30%
  down to 2% would admit this lens and also admit pure noise: section 0.3
  shows the metric barely separates 15 px of real distortion from zero at
  the rig's noise floor. The problem is not where the line sits, it is that
  the quantity does not discriminate in this regime.
- **A better target instead of a gate change.** A chrome-on-glass
  photolithographic target would cut corner noise enough for the existing
  gate to pass honestly, and it is the right answer if one is at hand. It
  is rejected as *this plan* because it is a purchase rather than a change,
  because the fit is already recovering the right coefficient without it,
  and because the gate would still be the wrong instrument for anyone else
  on a printed target.
- **Accepting the fit unconditionally and relying on the stitch
  cross-check.** The stitch estimate is the strongest evidence in this
  document, but it is available only after a roll has been stitched, and it
  cannot be part of a calibration-time gate without inverting the
  dependency between the two stages.
- **Bootstrap resampling instead of jackknife.** Equivalent statistically,
  and it needs a seed. Determinism is worth more here than the small gain
  in flexibility.
- **Cross-validating on held-out frames with a stability statistic derived
  from the existing split.** The 4 held-out frames are too few to estimate
  a spread from, and reusing the split would couple the gate to
  `HELDOUT_EVERY`.

---

## 8. Explicitly out of scope

- **The plumb-line fit itself** — stages, gauge, `K` convention,
  `cornerSubPix` window, `MIN_LINE_SET_MEMBERS`. Untouched.
- **The hard magnitude bounds** (`MAGNITUDE_HARD_MIN_PERCENT`,
  `MAGNITUDE_HARD_MAX_PERCENT`). Only the *expected* band moves.
- **Chromatic aberration's gates.** `chromatic_aberration.accepted` is
  false on the same profile for its own reasons (held-out misregistration
  0.72 → 0.72 px). Whether the same argument applies there is a real
  question and a separate plan; do not quietly generalise this one.
- **Anything downstream of a fitted geometry** — `registration`'s
  undistorter, `composite`'s band map, the profile's storage shape.
- **The layout solver's convergence**, which is a larger win than this one
  (median 3.66 → 2.09 px on the same roll with no new degrees of freedom)
  and belongs in its own plan.

---

## 9. Risks and known failure modes

- **A stability gate is blind to systematic target error.** If a printed
  board were uniformly stretched toward its edges, every subset would agree
  on a distortion that is the printer's, not the lens's, and the gate would
  accept it. The straightness gate is equally blind to this. The only real
  defence is an independent measurement, which here is the stitch
  correspondences agreeing at 16.8 px — and that defence is *not* part of
  the gate. Say so in `DECISIONS.md` rather than implying the gate proves
  more than it does.
- **Correlated frames.** The jackknife assumes frames are independent
  samples. Frames shot in one sitting share focus, board placement, and
  thermal state, so the SE is optimistic to an unknown degree. The
  section 6 sweep measures the statistic under the simulation's own
  independence assumption, which is the same optimism. Prefer a threshold
  with margin.
- **Frame count sensitivity.** The `(n-1)/n` factor makes the statistic
  comparable across `n` in expectation, but a jackknife on very few
  surviving frames is unstable regardless. Consider refusing the gate
  below some frame count rather than reporting a meaningless spread —
  `charuco.MIN_CORNERS_PER_FRAME` already drops frames, so `n` is not
  known until detection has run.
- **The runtime multiplier** (section 1.6) is unmeasured and could be worse
  than `n+1` if the staged fit's convergence differs on subsets.
