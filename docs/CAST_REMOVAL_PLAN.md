# Cast removal plan: chromatic-only filtration, a two-point neutral tie, and an auto solve

Three changes to the Edit tab's colour stage, all of them about the same
thing — making the colour controls correct *colour* and nothing else, and
giving the app enough measurement to solve the correction itself:

1. **Filtration becomes lightness-neutral.** Global and regional CMY are
   mean-removed, so moving them changes hue and never density. Print
   Density and the zone controls keep sole ownership of lightness.
2. **Cast removal gets a second tie point.** Today's single shadow tie
   rotates each channel's line about the anchor — one degree of freedom,
   which can correct a crossover but cannot correct a plain per-channel
   offset. A highlight tie frees the pivot and makes the per-channel
   correction a genuine affine (gain *and* offset), which is darktable
   `negadoctor`'s `wb_high` / `wb_low` pair in our coordinates.
3. **An Auto Cast Removal button.** A new stitch-time meter — a port of
   darktable's grey-surfaces illuminant detector — records the frame's
   residual neutral offset; a closed-form solve turns it into global CMY
   slider values, in the same style as Auto Density and Auto Grade.

This plan follows the conventions of `docs/COLOR_PLAN.md` and
`docs/DENSITY_PLAN.md`: numbered chunks, each independently green, every
constant in exactly one module, every calibration named and justified where
it lives. **`COLOR_PLAN.md` must be read first** — this plan changes its
§1.2, §1.3 and §1.4 and assumes everything else in it stands.

---

## 0. Why this shape

### 0.1 What this plan does not do

Not in scope, and not to be "finished" by an implementer:

- **Anchoring the thin end on the measured film base.** That is
  `docs/REBATE_ANCHORING.md`'s job — a *normalization* change with its own
  measurement gate, and the only one of the two plans that changes published
  pixels. **It is a prerequisite: this plan assumes its B-0 through B-7 have
  landed** (§0.6).
- **Changing the published pixels.** Everything in this plan is
  preview-only, exactly as the `tone` and `color` ops already are. The
  published TIFF and `export` never see any of it.
- **A confidence term on cast removal.** `COLOR_PLAN.md` §1.4 declined it
  for want of a signal saying whether the dense-end neutral set was
  trustworthy. §3.2 now records exactly that signal — `highlight_refs` is
  `null` when `_same_pixel_color_floor_refs` fell back — and this plan still
  declines to multiply the sliders by it. Note it as future work in
  `docs/punchlist.md`; do not build it here.

### 0.2 The three changes are independent but share one param object

Item 1 lands entirely inside `color.cmy_offsets` and `color.region_cmy`.
Item 2 adds one field to `ColorParams` and rewrites `color.cast_slopes`.
Item 3 adds one stitch-time meter, one solver module, and one CLI flag.

They meet in three places and nowhere else: the `color` op's param list
(which grows from twelve keys to thirteen), the `normalization` record
(which grows two meters), and the Colour panel.

### 0.3 Item 1 changes how already-recorded edits render, and that is accepted

Mean removal changes the meaning of every recorded non-zero CMY value: a
frame that was dialled to `+0.4 cyan` will now print at the same density it
did before the slider was touched, instead of darker. Recorded numbers are
not migrated and the op is not versioned.

This is accepted because the `color` op is preview-only — it never reached a
published TIFF or an export — so the blast radius is "some previews look
slightly different, and better". Record the decision in
`docs/DECISIONS.md` (§7.4) so a future reader does not treat the changed
render as a regression.

### 0.4 Why the highlight reference is not just "another percentile"

`measure_shadow_refs` takes a plain per-channel percentile
(`SHADOW_NEUTRAL_PERCENTILE = 98.0`) at the **thin** end. That is
defensible there and `analyze_bounds` says why: density on real film is
bounded below by base, so the thin end is physically anchored and the three
channels' percentiles land on comparable content.

The **dense** end is the opposite case, and `analyze_bounds` already
handles it: independent per-channel percentiles at the dense end read *a
different scene object per channel*, so coloured highlight content
masquerades as film cast. That is exactly why
`_same_pixel_color_floor_refs` exists — a shared, chroma-gated,
same-pixel set with a two-pass provisional refinement.

So the highlight reference **reuses that function** rather than adding a
mirrored percentile. It returns `None` when the band holds no trustworthy
neutrals, and that `None` is load-bearing information: it says the dense end
was *not* tied on measured neutrals, which is precisely when a user-driven
highlight tie has the most to do.

### 0.5 Why Auto sets global CMY and not the tie strengths

The two ties are anchored on this negative's own percentile references and
are fully determined by them: "auto" for a tie is just strength = 1, which
is the slider's own maximum and needs no button.

The grey-surfaces estimator measures something different — a *global*
neutral residual over the whole tonal range, weighted toward structured,
low-chroma regions, and independent of any percentile. That is a filtration
correction: a CMY pack, which is what a printer dials. So Auto writes
`wb_cyan / wb_magenta / wb_yellow` and touches nothing else.

The two corrections overlap — both remove part of the same offset — so the
solve subtracts the tie's own midtone contribution before converting to
sliders (§3.3 step 3). That compensation is exactly zero whenever the
anchor is pinned, which is every legacy one-point solve, so Auto's
behaviour on today's state is unaffected.

### 0.6 `docs/REBATE_ANCHORING.md` lands first

**Assume B-0 through B-7 of that plan are merged before R-0 starts**,
including its §11 measurement gate. That is a decision, not a guess: it
removes every ordering question below, and it means the thin end is already
anchored on a measured film base by the time any of this runs.

Four consequences, all of them load-bearing.

**1. The protocol number is 14, not 13.** `REBATE_ANCHORING.md` §9 takes
12 → 13. This plan takes **13 → 14** throughout — `events.PROTOCOL_VERSION`,
the `CONTRACT.md` paragraph, and the `protocol_version` assertions in
`events_test.py`.

**2. `ROLL_MANIFEST_FORMAT_VERSION` does not move.** `REBATE_ANCHORING.md`
takes it 7 → 8 because it adds a stored `film_base` *field*. This plan adds
two optional keys inside the existing `normalization` block plus one derived
`color_*` field — exactly what `MONOCHROME_PLAN.md`'s `mono` sub-block and
`COLOR_PLAN.md`'s thirteen `color_*` fields did without a bump. Leave it at
8. `NORMALIZE_FORMAT_VERSION` *does* move here, 2 → 3 (§5); that plan
explicitly does not touch it.

**3. `_thin_end_refs` is an extraction, not a new function.** B-6 will have
already replaced `analyze_bounds`' `c_ceils` percentile loop with the
conditional in `REBATE_ANCHORING.md` §4.1. R-1 lifts that conditional —
unchanged in behaviour — into a module-private helper so
`measure_highlight_refs` can call it too:

```python
def _thin_end_refs(
    values: np.ndarray,
    channels: int,
    base_refs: tuple[float, ...] | None = None,
) -> list[float]:
    # The thin-end per-channel colour references: the roll's measured film
    # base when it has one (REBATE_ANCHORING §4), else plain per-channel
    # percentiles of scene content. Lifted verbatim out of `analyze_bounds`;
    # the fallback branch and the `len(base_refs) == channels` guard are
    # that plan's, not this one's. (Write this as a real docstring.)
    if base_refs is not None and len(base_refs) == channels:
        return [float(v) for v in base_refs]
    return [
        _percentile(values[:, channel], 100.0 - BASE_COLOR_CLIP)
        for channel in range(channels)
    ]
```

`analyze_bounds` keeps its `base_refs` argument and calls the helper;
`composite.py` already has `base_refs` in scope (B-6 threads it in to build
`bounds`) and passes the same value to `measure_highlight_refs`. **B-6's
regression lock — `analyze_bounds` with `base_refs=None` returns exactly
what it returned before — must still pass after the extraction.** Run it;
it is the cheapest proof the lift was behaviour-preserving.

**4. The highlight tie becomes the more valuable of the two, and the plan
does not change shape for it.** `COLOR_PLAN.md` §0.6 justified a single
shadow tie by saying the dense end was tied on measured neutrals while the
thin end rested on plain per-channel percentiles, leaving the residual
crossover at the *shadow* end. B-6 inverts that: the thin end is now tied on
a measured film base, strictly better than any percentile, while the dense
end still rests on `_same_pixel_color_floor_refs` and its fallback. So the
shadow tie should have materially less to do, and the highlight tie should
be the one that earns its keep.

Both still ship — the solve needs both references either way (§2.3) — but
two things follow. §9's calibration checks are written for this expectation.
And `DECISIONS.md` (§7.4) must record that `COLOR_PLAN.md` §0.6's reasoning
is **superseded**, not merely extended; leaving two sections that disagree
about which end carries the residual is how the next reader picks the wrong
one.

A smaller consequence in the same direction: §3.1's neutral-residual meter
normalizes the grid by `bounds`, which are now base-anchored, so what it
measures is genuinely "the cast left over after a physically anchored
inversion" rather than "the cast left over after a percentile stretch".
Better-conditioned, not different.

---

## 1. Item 1 — filtration that changes hue and not density

### 1.1 Global CMY

`color.cmy_offsets` today:

```python
offset_ch = slider_ch * CMY_MAX_DENSITY / metering.ranges[ch]
```

New:

```python
raw_ch    = slider_ch * CMY_MAX_DENSITY / max(metering.ranges[ch], 1e-6)
offset_ch = raw_ch - mean(raw)          # arithmetic mean over the three
```

**Why the mean is removed *after* the range division, not before.** The
offsets are added to the normalized input `u`, and the curve applies the
same slope to every channel near the pivot, so the mean display shift is
`-slope · mean(offset)`. Zeroing `mean(offset)` — the mean of the
*post-division* values — is what makes the display's channel mean, and
therefore its lightness, invariant. Removing the mean of the *sliders*
before dividing would make an equal three-slider move an exact no-op but
would leave a lightness shift on every unequal move, which is the case that
actually matters.

**A consequence to state in the docstring, not to fix.** With the mean
removed after the division, an equal move of all three sliders is *not* a
no-op when the three recorded ranges differ — it is a pure hue move at
constant lightness. That is correct: a neutral-density filter on an image
whose channels have each been separately stretched genuinely has a
chromatic effect. Lightness is what we promised to hold, and it is held.

**Arithmetic mean, not Rec.709-weighted.** Equal weights match
`spektrafilm`'s `exposure_factor` (a geometric mean of the three print
exposures, which is an arithmetic mean in log — the space we are in) and
keep the three sliders symmetric with each other. A luma-weighted mean
would hold *perceived* lightness slightly better and would make a symmetric
C+M+Y move asymmetric in the sliders; symmetry wins.

**Mono.** Unreachable — `build_channel_tables` sets `apply_color = False`
when `channels == 1` and never calls this. Keep the guard `if
len(sliders) != len(metering.ranges): return (0.0,) * len(sliders)` anyway,
so a malformed record cannot index out of range.

### 1.2 Regional CMY

`color.region_cmy` today returns the two raw slider triples. New: each
triple is returned mean-removed.

```python
def _mean_removed(triple):
    m = sum(triple) / 3.0
    return tuple(v - m for v in triple)
```

These are added to the display value `v` directly (no range division), and
`_curve_raw` blends them with complementary weights `w_sh + w_hi == 1`. A
mean-zero triple therefore contributes a mean-zero display shift *at every
tone*, so the region controls become purely chromatic and stop competing
with `shadow_density` / `highlight_density`.

Unlike the global case this makes an equal three-slider move an **exact**
no-op, because there is no per-channel range in the path.

`tone._curve_raw` does not change for this item.

### 1.3 What must stay true

The neutral-is-byte-identical invariant is untouched: at every slider zero,
`raw` is all zeros, its mean is zero, and both functions return what they
return today.

---

## 2. Item 2 — the two-point tie

### 2.1 Why one point is not enough

Today's solve (`color.cast_slopes`) sets `anchor = pivot_in` and then
computes `pivot_ch = anchor - (slope/slope_ch)·(anchor - pivot_in)`, which
is identically `pivot_in`. The pivot never moves. So the correction is a
**pure rotation about the anchor**: one free parameter, `slope_ch`.

A rotation can tie one reference. It cannot correct a cast that is a
constant per-channel offset — a channel sitting uniformly high across the
whole tone scale — because nulling such an offset requires moving the line
*without* rotating it. In that case the one-point solve ties the shadow
reference and pushes the highlight end further out.

Freeing the pivot gives the second parameter, and two references determine
the line exactly. This is `negadoctor`'s structure: `wb_high` is a
multiplicative gain in log density and `wb_low` an additive offset, two
orthogonal handles per channel, with the module's own tooltips telling the
user to set the shadow one first.

### 2.2 The solve

All of §2 is in **display coordinates**, `x = 1 - u`. Green (channel 1) is
the reference channel and is never modified: `slope_G = slope`,
`pivot_G = pivot_in`.

```
g_s = 1 - shadow_refs_norm[1]          # green's print-shadow reference
g_h = 1 - highlight_refs_norm[1]       # green's print-highlight reference
r_s = 1 - shadow_refs_norm[ch]
r_h = 1 - highlight_refs_norm[ch]

t_s = g_s + clamp(strength_s * (r_s - g_s), -CAST_MAX_OFFSET, +CAST_MAX_OFFSET)
t_h = g_h + clamp(strength_h * (r_h - g_h), -CAST_MAX_OFFSET, +CAST_MAX_OFFSET)
```

`strength_s` is `params.cast_removal`; `strength_h` is the new
`params.cast_removal_highlights`. Both use the same `CAST_MAX_OFFSET = 0.1`.

We require channel `ch` to print at `t_s` what green prints at `g_s`, and
at `t_h` what green prints at `g_h`. With
`v(x) = pivot_out + slope_ch·(x - pivot_ch)` this is two linear equations,
and subtracting them gives:

```
slope_ch = slope * (g_h - g_s) / (t_h - t_s)
pivot_ch = t_s - (slope / slope_ch) * (g_s - pivot_in)
```

Derivation to keep in the docstring: the first line is the difference of
the two constraints; the second is the shadow constraint re-solved for the
pivot once the slope is known.

### 2.3 Guards, in this order

1. `params.cast_removal <= 0 and params.cast_removal_highlights <= 0`
   → achromatic (today's first guard, widened).
2. `metering.shadow_refs_norm is None` or its length is not 3
   → achromatic. The two-point solve needs the shadow reference as its
   anchor equation; without it there is nothing to solve.
3. `metering.highlight_refs_norm is None`, or its length is not 3, **or**
   `params.cast_removal_highlights == 0`
   → **run today's one-point solve, unchanged, driven by
   `params.cast_removal`.** This branch must be byte-for-byte the current
   behaviour; do not let the two-point formula "degenerate into" it, because
   it does not (at `strength_h = 0` the two-point formula gives
   `slope_ch = slope·(g_h - g_s)/(g_h - t_s)`, which is a different number).
4. `abs(t_h - t_s) < 1e-6` → fall back to the one-point solve. The two
   references have collapsed onto each other and the line is undetermined.
5. `slope_ch` is clamped to `[tone.SLOPE_MIN, tone.SLOPE_MAX]`. **After
   clamping, re-solve `pivot_ch` from the clamped slope** using the same
   formula, so that the *shadow* tie still holds exactly and only the
   highlight tie degrades. The shadow end has the better-anchored reference
   (§0.4), so it is the one that keeps its guarantee.
6. `abs(slope_ch) < 1e-6` → `pivot_ch = pivot_in`.

`strength_s == 0` with `strength_h > 0` is legal and meaningful: the shadow
end is pinned to green's own reference and the highlight end is tied. Do not
special-case it.

### 2.4 The anchor is no longer pinned, and that is the point

In the two-point branch, R and B no longer print unchanged at the anchor.
That is not a regression to be patched out — it is the second degree of
freedom being used, and it is what lets an offset cast be corrected at all.

Exposure stays anchored because **green is untouched**, and green carries
0.7152 of the Rec.709 luma. A correction that moves R and B is supposed to
move the image's colour; the residual lightness change it brings is the
0.28 the other two channels are worth.

### 2.5 What must stay true

`COLOR_PLAN.md` §1.6 is unchanged and must be restated in the code comment
so nobody "fixes" it: **the endpoint rescale anchors are read once, on the
achromatic curve — grade and snap only, every density, colour and shaping
control at rest — and the same `(low, high)` pair rescales all three
channels.** A per-channel rescale would undo exactly the colour difference
this solve just created.

`pivot_out` stays `0.5` for every channel. Do not make it per-channel.

---

## 3. Item 3 — the neutral estimator and the auto solve

### 3.1 The meter

A new `normalization.measure_neutral_residual(grid_log, keep, bounds)`,
called from `composite.py` beside the other meters. It is a port of
darktable's `DT_ILLUMINANT_DETECT_SURFACES` weighting
(`src/iop/channelmixerrgb.c:_auto_detect_WB`) into our coordinates, and it
is **recorded, never acted on** by the stitch stage — same status as
`shadow_refs` and `anchor`.

It runs on the block-median grid, which is already computed, already
dust-free and already resolution-invariant, and it runs on the grid
**normalized by the same bounds the published image gets**, so the
per-channel stretch is already removed and what is left is exactly the
residual the auto solve wants.

```
NEUTRAL_RESIDUAL_P_NORM   = 8.0     # darktable's Minkowski p, unchanged
NEUTRAL_RESIDUAL_MIN_CELLS = 64     # below this there is no estimate
NEUTRAL_RESIDUAL_EPS      = 1e-6    # darktable's NORM_MIN, same role
```

Algorithm — all of it vectorised, no per-cell Python loop:

1. Return `None` immediately when `grid_log.shape[-1] != 3`.
2. `norm = normalize_log_image(grid_log, bounds)`.
3. The two chroma coordinates, zero on a neutral:
   `a = norm[..., 0] - norm[..., 1]`, `b = norm[..., 2] - norm[..., 1]`.
4. Local statistics over a 3×3 neighbourhood, `cv2.BORDER_REPLICATE` on
   every filter:
   - `a_bar`, `b_bar` — the B-spline blur darktable uses, kernel
     `[[1,2,1],[2,4,2],[1,2,1]] / 16`, via `cv2.filter2D`.
   - `var_a = box3(a*a) - box3(a)**2`, likewise `var_b`, and
     `cov_ab = box3(a*b) - box3(a)*box3(b)`, where `box3` is
     `cv2.boxFilter` with a 3×3 normalised kernel.
5. `w = maximum(var_a * var_b * cov_ab, 0.0)`.
   **This clamp is a deliberate deviation from the port** — darktable lets a
   negative covariance subtract from the accumulation; on our grid a
   negative weight can flip the sign of the estimate on a noisy frame, and a
   patch whose two chroma coordinates are anti-correlated is not evidence
   about the illuminant either way. Say so in the comment.
6. `p_norm = (|a_bar|**P + |b_bar|**P) ** (1/P) + EPS` — darktable's
   Minkowski regularisation, which downweights strongly-coloured patches.
7. Accumulate over `keep_eroded = keep & erode(keep, ones(3,3))` — cells
   whose whole 3×3 neighbourhood is inside the analysis region, so a
   withheld rebate or dense border never leaks into a neighbourhood. This is
   the same idiom `_region_border` already uses.
8. `W = sum(w/p_norm)`; return `None` when `W <= 0` or fewer than
   `NEUTRAL_RESIDUAL_MIN_CELLS` cells contributed a non-zero weight.
9. Return `(sum(a_bar*w/p_norm) / W, sum(b_bar*w/p_norm) / W)` — the
   residual `(R−G, B−G)` in normalized units.

### 3.2 The highlight reference meter

A new `normalization.measure_highlight_refs(grid_log, keep, base_refs=None)`,
returning `tuple[float, ...] | None`.

It returns `None` for `grid_log.shape[-1] != 3`. Otherwise it assembles
exactly the arguments `analyze_bounds` assembles and delegates to the
existing `_same_pixel_color_floor_refs` (§0.4):

- `g_flat = grid_log.reshape(-1, 3)`, `keep_flat = keep.reshape(-1)`,
  `lum_full = luma_of_log(grid_log).reshape(-1)`
- `base = np.asarray(_thin_end_refs(g_flat[keep_flat], 3, base_refs))` —
  §0.6's shared helper, lifted out of `analyze_bounds` in this chunk and
  called from both. After B-6 that resolution prefers the roll's frozen film
  base, so the highlight reference is measured against the same physically
  anchored thin end the published pixels use.

Returns `_same_pixel_color_floor_refs(...)` unchanged — which is `None`
when the band held no trustworthy neutrals, and that `None` is recorded as
`null`.

`composite.py` passes the same `base_refs` it passes to `analyze_bounds` —
B-6 already threads it into scope — so the call is
`measure_highlight_refs(grid, keep, base_refs)`. On a roll frozen
`"legacy"` or `"absent"` that value is `None` and the helper falls back to
percentiles, exactly as `analyze_bounds` does.

### 3.3 The solve — `cli/src/scanny_boy/auto_color.py`

A new module, mirroring `auto_tone.py`'s docstring and style: closed-form
solves from a negative's recorded `normalization` block, no image I/O, no
metering pass.

```python
def solve_cmy(
    record: dict | None,
    params: color.ColorParams,
    slope: float,
    pivot_in: float,
) -> tuple[float, float, float] | None:
    """Global CMY slider values that null the recorded neutral residual."""
```

Returns `None` when `record` is missing, has no `neutral_residual`, or the
value is not a two-element list of finite numbers. Never raises.

**Step 1 — read the residual.** `a, b = record["neutral_residual"]`.

**Step 2 — the mean-removed offsets that null it.** A global CMY offset adds
`o_ch` to the normalized `u_ch`. Nulling the residual means
`o_R - o_G = -a` and `o_B - o_G = -b`; combined with `o_R + o_G + o_B = 0`
(the mean removal of §1.1) that solves in closed form:

```
o_G = (a + b) / 3
o_R = (b - 2a) / 3
o_B = (a - 2b) / 3
```

Verify the sum is zero in a test; it is, by construction.

**Step 3 — subtract what the ties already do at the anchor.** Call
`color.cast_slopes(params, metering, slope, pivot_in)`; for each channel the
tie's display value at the anchor `x = pivot_in` is
`pivot_out + slope_ch·(pivot_in - pivot_ch)`, and green's is `pivot_out`.
The difference is

```
d_ch  = slope_ch * (pivot_in - pivot_ch)
tie_ch = -d_ch / slope          # the equivalent input offset, since Δv = -slope·o
```

Take `o_ch -= tie_ch`, then re-remove the mean of the three.

This term is **exactly zero in every one-point branch**, because there
`pivot_ch == pivot_in`. So Auto's behaviour on today's state is unchanged,
and the compensation only appears once a highlight tie is dialled in. It is
a first-order proxy — the tie's effect is evaluated at the anchor while the
residual is a whole-image average — and the docstring must say so rather
than implying an exact cancellation.

**Step 4 — convert to sliders and clamp.** `cmy_offsets` maps
`slider → slider·CMY_MAX_DENSITY/range`; the inverse is

```
slider_ch = clamp(o_ch * ranges[ch] / CMY_MAX_DENSITY, CMY_MIN, CMY_MAX)
```

Because the target `o` is already mean-zero, `cmy_offsets` will reproduce it
exactly (its own mean removal is then a no-op). Clamping can break that;
that is accepted, and a clamped solve is a saturated one.

Return `(slider_R, slider_G, slider_B)` = `(wb_cyan, wb_magenta,
wb_yellow)` — cyan is the red channel's dye, magenta green's, yellow
blue's, matching `COLOR_PLAN.md` §1.3.

### 3.4 Where `slope` and `pivot_in` come from

`solve_cmy` needs the tone curve's base slope and input pivot. Those are
computed inside `tone._curve_raw` today. **Extract them** into

```python
def base_slope_and_pivot(tone_params: ToneParams) -> tuple[float, float]:
    """The achromatic straight-line slope and input pivot — the two
    quantities every per-channel colour solve is defined against."""
    slope = grade_slope(tone_params.grade_r)
    pivot_in = 0.5 + (tone_params.density - DENSITY_REFERENCE) * DENSITY_PIVOT_SHIFT
    return slope, pivot_in
```

and have `_curve_raw` call it. One definition, no drift.

`edits.run_edit_color` builds the `ToneParams` from
`repo.net_edit_state(...).tone` (or `tone.NEUTRAL` when there is no tone
op) and passes the pair down.

---

## 4. Chunk R-0 — `color.py` and `tone.py`

Pure functions and their tests. No persistence, no CLI, no meters. Ships
**inert** for item 2: the new field exists and defaults to 0, and
`read_metering` will always report `highlight_refs_norm = None` until R-1
records it, so the one-point branch is what actually runs.

### Changes

`cli/src/scanny_boy/color.py`:

- `CAST_REMOVAL_HIGHLIGHTS_MIN = 0.0`, `CAST_REMOVAL_HIGHLIGHTS_MAX = 1.0`.
- `ColorParams` gains `cast_removal_highlights: float = 0.0`, placed
  immediately after `cast_removal`.
- `COLOR_PARAM_KEYS` gains `"cast_removal_highlights"` after
  `"cast_removal"`. **Also add `COLOR_PARAM_KEYS_V1`** — the original
  twelve, frozen, in their original order — with a comment saying it exists
  only so `repo._parse_color_op` can recognise an op written before this
  plan (R-2 §6.1). Nothing else may read it.
- `_color_param_bounds()` gains the new pair.
- `Metering` gains `highlight_refs_norm: tuple[float, ...] | None = None`.
- `read_metering` normalizes `record["highlight_refs"]` exactly as it
  normalizes `shadow_refs` — same guards, same `(ref - floor)/span`, same
  "anything wrong → `None`" rule. It still never raises.
- `cmy_offsets` — §1.1's mean removal.
- `region_cmy` — §1.2's mean removal.
- `cast_slopes` — §2.2's solve behind §2.3's guards. Keep the existing
  one-point body verbatim as the fallback branch; do not rewrite it.

`cli/src/scanny_boy/tone.py`:

- `base_slope_and_pivot` (§3.4), called from `_curve_raw`.

### Tests (`color_test.py`, `tone_test.py`)

1. **Neutral is identical.** Every slider at rest: `cmy_offsets` and
   `region_cmy` return all zeros; `build_display_lut` is bit-identical to
   the pre-change table; the three rows of `build_channel_tables` are equal.
2. **Global CMY is lightness-neutral.** For a set of random slider triples
   and unequal `ranges`, `sum(cmy_offsets(...)) == 0` to 1e-12.
3. **Global CMY still bites.** A cyan-only slider still moves the red
   table; the magnitude still scales with `1/range_R`.
4. **Global CMY changes hue, not mean.** With unequal ranges, an equal
   three-slider move leaves `sum(offsets) == 0` but the individual offsets
   non-equal — the §1.1 consequence, asserted so it cannot be "fixed" by
   accident.
5. **Regional CMY is lightness-neutral and exactly cancels.** Each returned
   triple sums to 0; an equal three-slider move on a region returns all
   zeros; the display shift's channel mean is 0 at `v = 0.1, 0.5, 0.9`.
6. **One-point parity.** With `highlight_refs_norm = None`, or with it set
   and `cast_removal_highlights = 0`, `cast_slopes` returns *exactly* what
   the pre-change implementation returned for the same inputs. Pin this with
   a table of hand-computed expected values, not by calling the old code.
7. **Two-point ties both ends.** With a hand-built `Metering` whose red
   shadow and highlight refs both sit off green's, and both strengths at 1:
   the red table at `r_s` equals the green table at `g_s`, and the red table
   at `r_h` equals the green table at `g_h`, both to 1e-9.
8. **Two-point corrects a pure offset.** Refs where `r_s - g_s == r_h -
   g_h` (a constant offset cast): the two-point solve gives
   `slope_ch == slope` and a shifted pivot; the one-point solve gives a
   changed slope. Assert both.
9. **Both ends bounded.** References 10× past `CAST_MAX_OFFSET` give the
   same tables as references exactly at the limit, on each end
   independently.
10. **Shadow-only and highlight-only.** `strength_s = 0, strength_h = 1`
    runs the two-point branch and ties the highlight end; `strength_s = 1,
    strength_h = 0` runs the one-point branch.
11. **The slope clamp keeps the shadow tie.** Refs chosen so `slope_ch`
    saturates at `SLOPE_MAX`: the shadow tie still holds to 1e-9, the
    highlight tie does not, and nothing raises.
12. **Green is never touched**, in every branch.
13. **Degenerate refs.** `t_h == t_s` falls back to the one-point solve;
    `shadow_refs_norm = None` is inert for any strengths.
14. **Monotone and in range.** Extend the existing whole-box sweep with the
    new corner (`cast_removal_highlights` at 0 and 1); every table monotone
    non-increasing in the code, every value in `[0, 1]`.
15. **`base_slope_and_pivot`** reproduces the numbers `_curve_raw` used
    before the extraction, across the density range.

---

## 5. Chunk R-1 — the two new meters

`normalization.py`, `composite.py`, `stitch_pipeline.py`. Green in the
CLI's own suite; run `--slow` at the end of this chunk, because it changes
what a real stitch writes.

### Changes

`cli/src/scanny_boy/normalization.py`:

- `_thin_end_refs(values, channels, base_refs=None)` lifted out of
  `analyze_bounds`' post-B-6 body (§0.6) and called from both it and
  `measure_highlight_refs`. Re-run `REBATE_ANCHORING.md` B-6's
  `base_refs=None` regression lock afterwards.
- `measure_highlight_refs(grid_log, keep)` (§3.2).
- `measure_neutral_residual(grid_log, keep, bounds)` (§3.1) and its three
  constants.
- `build_params()` gains `neutral_residual_p_norm`,
  `neutral_residual_min_cells` and `highlight_neutral_source`
  (the string `"same_pixel_color_refs"`, so the record says *which*
  reference the highlight refs came from without a reader having to know
  the code). Bump `NORMALIZE_FORMAT_VERSION` 2 → 3.
  `upgrade_normalize_params` needs **no change** — it already injects the
  live defaults for any key a stored block lacks and is idempotent, which
  is exactly this case.

`cli/src/scanny_boy/composite.py`:

- `CompositeResult` gains
  `highlight_refs: tuple[float, ...] | None` and
  `neutral_residual: tuple[float, float] | None`.
- In the meter block (immediately after `shadow_refs = measure_shadow_refs(
  grid, keep)` and **before** `del grid, keep`):

  ```python
  highlight_refs = measure_highlight_refs(grid, keep)
  ```

  and, after the clamp has settled `bounds` and **before** `del img_log`:

  ```python
  neutral_residual = measure_neutral_residual(grid, keep, bounds)
  ```

  The residual must be measured against the **clamped** bounds — the ones
  the published pixels are actually stretched by — so move the `del grid,
  keep` below the clamp block rather than measuring against the unclamped
  pair.

`cli/src/scanny_boy/stitch_pipeline.py`:

- `_normalization_record` gains `"highlight_refs"` (a list or `null`) and
  `"neutral_residual"` (a two-element list or `null`).

`shared/contract/roll-manifest.schema.json`:

- Both keys under `definitions/normalization/properties`, **not** in
  `required` (older manifests are still valid). `highlight_refs` is
  `oneOf: [null, channelArray]`; `neutral_residual` is `oneOf: [null,
  {type: array, items: number, minItems: 2, maxItems: 2}]`. Give each a
  `description` naming this plan's section.

### Tests (`normalization_test.py`, `composite_test.py`, slow tier for the
stitch)

- **Synthetic neutral frame.** A grid built from a neutral ramp with
  structured (non-flat) patches: `measure_neutral_residual` returns
  approximately `(0, 0)`.
- **Synthetic cast frame.** The same ramp with a known constant offset
  added to the red channel *before* normalization: the recovered `a`
  matches the offset's normalized size to within 10%. State that tolerance
  in the test's name — it is an estimator, not an identity.
- **Flat frame.** A perfectly flat grid: every local variance is 0, so
  `W == 0` and the result is `None`.
- **Too small.** A grid with fewer than `NEUTRAL_RESIDUAL_MIN_CELLS`
  eligible cells returns `None`.
- **The erosion works.** A grid with a withheld border stripe carrying a
  wild cast produces the same estimate as the same grid without it.
- **Mono.** A single-channel grid returns `None` from both new meters.
- **`measure_highlight_refs` agrees with `analyze_bounds`.** On a grid
  whose neutral band is trustworthy, the returned triple is exactly the
  `c_floors` list `analyze_bounds` uses; on a grid whose band is not, both
  return the fallback signal (`None` here, plain percentiles there).
- **The record round-trips.** A real stitch writes both keys; the manifest
  validates against the updated schema; a manifest *without* them still
  validates and still loads.
- **`upgrade_normalize_params` is still idempotent** and still compares a
  v2 block equal to a fresh v3 build.

---

## 6. Chunk R-2 — the thirteenth param

`library/repo.py` only, plus its tests.

### 6.1 The forward shim is the whole risk of this chunk

`_parse_color_op` today requires **every** key in `COLOR_PARAM_KEYS` to be
present, and returns `None` otherwise. Adding a thirteenth key would make
every already-recorded colour op parse as `None` — silently discarding real
user state. This must not happen.

```python
def _parse_color_op(params: dict) -> dict[str, float] | None:
    from scanny_boy import color

    # Gate on the ORIGINAL twelve: an op written before
    # docs/CAST_REMOVAL_PLAN.md has no thirteenth key and is still a
    # complete colour state. Newer keys fall back to their neutral
    # defaults, which is what "this op predates the control" means.
    if not all(key in params for key in color.COLOR_PARAM_KEYS_V1):
        return None
    if all(params.get(key) is None for key in color.COLOR_PARAM_KEYS_V1):
        return None
    merged = _color_neutral_defaults()
    for key in merged:
        value = params.get(key)
        if value is None:
            if key in color.COLOR_PARAM_KEYS_V1:
                return None      # a null among the twelve is still a reset
            continue             # a missing newer key keeps its default
        ...
```

Keep the rest of the body — the bool/number checks and `_color_in_range` —
as it is.

### 6.2 The rest

- `_color_neutral_defaults()` gains `"cast_removal_highlights": 0.0`.
- `validated_color_params`: the `missing` check runs against
  `COLOR_PARAM_KEYS_V1`, not the full list; any newer key absent from
  `params` is filled from `_color_neutral_defaults()` before validation.
  The all-set/all-`None` rule and the range loop are otherwise unchanged.
- `edits._merge_color_params`: build `base` from
  `repo._color_neutral_defaults()` and overlay the recorded dict, instead
  of indexing the recorded dict directly. Today's
  `{key: base[key] for key in COLOR_PARAM_KEYS}` raises `KeyError` on a
  twelve-key recorded state the moment the list grows.
- Update `COLOR_OP`'s module comment: "twelve keys" → "thirteen keys, of
  which the original twelve are the compatibility floor".

### Tests (`repo_test.py`, `edits_test.py`)

- A stored twelve-key op parses to a thirteen-key state with
  `cast_removal_highlights == 0.0` and every other value preserved.
- A stored twelve-key op that is all `None` still reads as a reset.
- A thirteen-key op round-trips.
- `validated_color_params` accepts a twelve-key dict and returns thirteen;
  rejects an out-of-range `cast_removal_highlights`; still rejects a
  mixed set/`None` dict.
- `_merge_color_params` with a twelve-key recorded state and a one-key
  update returns thirteen keys with the other twelve untouched.
- Coalescing, reset and the rotate-between-ops cases from `COLOR_PLAN.md`
  §4 all still pass.

---

## 7. Chunk R-3 — the CLI

`auto_color.py`, `edits.py`, `cli.py`, `events.py`, and the contract.

### 7.1 `auto_color.py`

New module, §3.3. Its only public function is `solve_cmy`.

### 7.2 `edits.run_edit_color`

Two additions:

- A keyword-only `auto_cast: bool = False`.
- When `auto_cast` and not `reset`, after the merge and **before**
  validation:

  ```python
  tone_state = state.tone
  tone_params = tone.ToneParams(**tone_state) if tone_state else tone.NEUTRAL
  slope, pivot_in = tone.base_slope_and_pivot(tone_params)
  meter = color.read_metering(negative.normalization)
  solved_cmy = auto_color.solve_cmy(
      negative.normalization,
      color.ColorParams(**solved),
      slope,
      pivot_in,
  )
  ```

  On `None`, emit `Code.TONE_METERING_UNAVAILABLE` with
  `f"{negative.negative_id}: no neutral estimate recorded; filtration left "
  "unchanged"` and record the merged state as-is. On a solve, write the
  three values into `solved["wb_cyan"] / ["wb_magenta"] / ["wb_yellow"]`.

  `--auto-cast` therefore composes with explicit flags exactly as
  `--auto-density` does: the auto result wins over a recorded value and
  loses to nothing, because a caller that wants both would be contradicting
  itself.

- The existing `cast_removal` metering warning is widened to fire when
  *either* strength is non-zero and the metering it needs is missing —
  `shadow_refs_norm is None` for `cast_removal`, `highlight_refs_norm is
  None` for `cast_removal_highlights`. One warning per negative, not two.

**Reuse `TONE_METERING_UNAVAILABLE`; do not rename it.** `COLOR_PLAN.md`
§7.2 proposed renaming it to `METERING_UNAVAILABLE` *before it shipped*. It
has shipped. Renaming a live contract code costs more than the wart.
Record that in `DECISIONS.md`.

### 7.3 `cli.py`

- `--cast-removal-highlights V` — `0..1`, help
  `"highlight-end cast removal strength, 0..1 (0 neutral)"`. Add
  `"cast_removal_highlights": "cast_removal_highlights"` to
  `_color_flag_updates`' mapping.
- `--auto-cast` — `action="store_true"`, help
  `"solve global filtration from this negative's recorded neutral estimate"`.
  Passed through to `run_edit_color(..., auto_cast=args.auto_cast)`.
- `--auto-cast` with `--reset` is a usage error, in
  `_validate_color_temperature_args` (rename it
  `_validate_color_args`). `--auto-cast` with an explicit `--cyan`,
  `--magenta` or `--yellow` is also a usage error — the auto owns all three.

### 7.4 Protocol 14, contract, docs

- `events.py`: `PROTOCOL_VERSION = 14` (§0.6 — `REBATE_ANCHORING.md` took
  13), with a comment block in the existing style — protocol 14 keeps 13's
  roll model and film-base block and adds
  `--cast-removal-highlights` and `--auto-cast` on `edit color`, the
  `color_cast_removal_highlights` field on `roll info`, and the
  `highlight_refs` / `neutral_residual` meters in the `normalization`
  block.
- `roll_manifest.py` + `roll-manifest.schema.json`: the
  `color_cast_removal_highlights` derived field beside the other
  `color_*` ones, `["number", "null"]`, marked "Derived, not stored".
- `shared/contract/CONTRACT.md`: a "Protocol version 14" paragraph; the two
  new flags in the `edit color` section with their ranges, the
  `--auto-cast` exclusivity rules, and a sentence saying the auto reads a
  stitch-time meter so it is unavailable on rolls stitched by an older
  build.
- `shared/contract/schema.json`: check whether anything constrains flag
  names or op params; do not assume it does not.
- `docs/DECISIONS.md`, a new "Cast removal and filtration" section covering
  exactly seven things:
  1. CMY is mean-removed so filtration owns hue and Print Density owns
     lightness (§1.1), and why the mean is removed after the range
     division rather than before;
  2. that this changes how already-recorded colour ops render, and that it
     is accepted because the op is preview-only (§0.3);
  3. why the tie has two points and what the second one buys — a pure
     offset cast is uncorrectable with one (§2.1);
  4. that in the two-point branch the anchor is no longer pinned for R and
     B, that green is the exposure anchor, and that this is deliberate
     (§2.4);
  5. that the highlight reference reuses `_same_pixel_color_floor_refs`
     rather than mirroring the shadow percentile, and why the dense end
     needs a chroma-gated same-pixel set (§0.4);
  6. that **`COLOR_PLAN.md` §0.6 is superseded** — with the thin end now
     anchored on the measured film base, the residual crossover has moved
     from the shadow end to the dense end, so the highlight tie is the one
     doing the work and the shadow tie is the trim (§0.6 point 4). Say
     "superseded", not "extended"; two sections disagreeing about which end
     carries the residual is how the next reader picks the wrong one;
  7. that `TONE_METERING_UNAVAILABLE` is reused for a colour-only condition
     rather than renamed (§7.2).

### Tests (`cli_test.py`, `edits_test.py`, `events_test.py`, `auto_color_test.py`)

- `--cast-removal-highlights` round-trips through `roll info`.
- A single `--cast-removal-highlights` leaves the other twelve values alone.
- `--auto-cast` on a negative with a recorded `neutral_residual` writes
  three CMY values whose `cmy_offsets` sum to ~0 and whose sign nulls the
  recorded residual.
- `--auto-cast` on a negative whose `normalization` is `None`, or whose
  `neutral_residual` is `null`, warns once and records the state unchanged.
- `--auto-cast` with `--reset`, and with `--cyan`, are usage errors.
- **The tie compensation is inert in the one-point branch**: with
  `cast_removal_highlights = 0`, `solve_cmy`'s result is independent of
  `cast_removal`. With it non-zero and a highlight ref present, the result
  moves.
- `solve_cmy` returns `None` for a malformed residual (wrong length,
  non-finite, non-numeric) and never raises.
- A non-zero `cast_removal_highlights` on a negative with no recorded
  `highlight_refs` warns and still records.
- The published TIFF is untouched and `export` output is unchanged with
  every new state recorded.
- Emitted lines carry `protocol_version: 14`.

---

## 8. Chunk R-4 — the Mac app

### 8.1 The value type

`ScannyBoy/Model/ColorAdjustment.swift`:

- `castRemovalHighlights: Double`, after `castRemoval`, in the struct, the
  memberwise `init`, and `.neutral` (at `0`).
- `RollManifest.Negative.colorAdjustment` maps
  `colorCastRemovalHighlights ?? 0`. The `guard colorWbMagenta != nil`
  presence gate is unchanged — absence still means "no op", and an older
  CLI's payload degrades to neutral on the new field, matching how the
  twelve already behave.
- `RollManifest.Negative` gains `colorCastRemovalHighlights: Double?`.

`ScannyBoy/CLIBridge/CLIRunner.swift`:

- `editColor` appends `--cast-removal-highlights`.
- A `ColorAutoFlags` option set — `static let cast` — mirroring
  `ToneAutoFlags` exactly, appended as `--auto-cast`. When the flag is set,
  **omit** `--cyan`, `--magenta` and `--yellow` from the command (§7.3's
  exclusivity), and omit `--temperature` if the region is `global`.

`ScannyBoy/Model/EditModel.swift`:

- `commitColor` / `setColor` / `scheduleColor` gain
  `auto: ColorAutoFlags = []`, copying the tone path's signatures verbatim.
- **`renderGeneration` must hash `castRemovalHighlights`.** Miss it and a
  highlight-tie change leaves stale 1:1 region crops on screen. This is the
  single most likely bug in this chunk.

### 8.2 The panel

`ScannyBoy/Views/EditStageView.swift`, `ColorAdjustmentPanel`:

- The **Correction** section becomes two sliders plus one button:

  | control | range | step | format | reset |
  |---|---|---|---|---|
  | Cast Removal — Shadows | 0…1 | 0.05 | `%.2f` | 0 |
  | Cast Removal — Highlights | 0…1 | 0.05 | `%.2f` | 0 |

  Help text on the shadows slider: "Balances each layer against the frame's
  own shadow greys." On the highlights slider: "Balances each layer against
  the frame's own highlight greys. Needs a highlight reference; inactive on
  rolls stitched before it was measured."

- The **Auto** button goes in the *Balance* section, under the C/M/Y
  sliders, labelled `Auto`, with help "Solve the filtration from this
  negative's own neutral estimate". It calls
  `onCommitNow(values, [.cast])`. It is a one-shot commit, not a mode —
  the returned values land on the sliders through the normal
  `syncFromModel` path and the user can then move them.

- `ColorAdjustmentPanel`'s `resetValue(for:)` switch (currently at
  `EditStageView.swift:976`) gains `"Cast Removal — Shadows"` and
  `"Cast Removal — Highlights"` → `0`. Update the existing `"Cast Removal"`
  case rather than leaving a dead string.

- `Region Reset` and `Reset All` are unchanged. Neither touches the tie
  sliders — they are not regional.

### Tests (`EditModelTests`, `CLIEventTests`, `CLICommandTests`)

- `setColor` records and reflects all thirteen values.
- A partial commit leaves the other twelve at their recorded values.
- `renderGeneration` differs for two negatives differing only in
  `castRemovalHighlights`.
- `CLICommand.editColor` with `[.cast]` emits `--auto-cast` and emits
  neither `--cyan` nor `--magenta` nor `--yellow`.
- `CLICommand.editColor` without the flag emits
  `--cast-removal-highlights`.
- A `roll info` payload without `color_cast_removal_highlights` but with
  the other twelve yields an adjustment with `castRemovalHighlights == 0`,
  not `nil`.
- The tone panel's tests still pass; a colour edit leaves the tone state
  alone.

---

## 9. Order, verification, and the open calibrations

```
REBATE_ANCHORING B-0 … B-7   →   R-0 → R-1 → R-2 → R-3 → R-4
```

R-0 is pure and testable alone, and ships item 1 live and item 2 inert.
R-1 turns item 2 on and is where the real stitch changes, so run `--slow` at
its end — including `REBATE_ANCHORING.md` B-6's own tests, which R-1's
`_thin_end_refs` extraction must not disturb (§0.6 point 3). R-2 is
persistence only. R-3 is the first chunk that changes the wire. R-4 is the
only chunk that needs Xcode.

```bash
cd cli && uv run pytest                       # fast tier, per chunk
cd cli && uv run pytest --slow                # after R-1, and again after R-3
cd mac && xcodegen generate && xcodebuild test -scheme ScannyBoy -destination 'platform=macOS'
```

### The open calibrations

Everything in items 1 and 2 is either an existing constant reused unchanged
or falls out of an inversion. Item 3 introduces three numbers, two of them
darktable's own.

| constant | value | basis |
|---|---|---|
| `CMY_MAX_DENSITY` | 0.2 | unchanged; mean removal does not rescale it — firm |
| `CAST_MAX_OFFSET` | 0.1 | unchanged, now applied to both ends — firm |
| `NEUTRAL_RESIDUAL_P_NORM` | 8.0 | darktable's Minkowski p, dimensionless — firm |
| `NEUTRAL_RESIDUAL_EPS` | 1e-6 | darktable's `NORM_MIN`, same role — firm |
| `NEUTRAL_RESIDUAL_MIN_CELLS` | 64 | chosen, not measured — **check** |
| `cast_removal_highlights` default | 0.0 | conservative: the new branch is opt-in — **check on real rolls** |

**After R-1 and before R-4**, render a handful of real frames from rolls
that have a frozen `"measured"` film base, and confirm:

- **`highlight_refs == null` rates first.** Count them across the roll
  library before touching a slider — it is the direct evidence for §0.6
  point 4, and it is free. A high rate means the dense-end neutral set is
  falling back often, the highlight tie has real work to do, and the whole
  ordering of this plan's value is confirmed. A near-zero rate means the
  neutral set is reliable and the highlight tie is a trim after all.

- **The two ties against each other.** Run `--cast-removal 1` and
  `--cast-removal-highlights 1` separately on the same frames. The
  expectation under §0.6 point 4 is that the *highlight* tie is now the
  larger, more useful move and the shadow tie is small, because B-6 anchored
  the thin end on a measured base. **If the shadow tie is still the larger
  move, §0.6 point 4 is wrong and it must be corrected in this plan and in
  `DECISIONS.md` before R-4** — do not quietly ship the panel with the
  ordering reversed from the reasoning.

- **`NEUTRAL_RESIDUAL_MIN_CELLS`**: how many frames in a roll return `None`.
  If a well-exposed frame with ordinary content ever fails to produce an
  estimate, the floor is too high. If a nearly-monochrome frame (fog, a
  wall, snow) produces a confident and wrong estimate, it is too low.

- **Auto against manual.** Dial a frame by eye, then press Auto, and compare
  the two CMY triples. Systematic disagreement in one direction means the
  sign or the mean removal in §3.3 step 2 is wrong; scattered disagreement
  is the estimator being an estimator. Do this on base-anchored rolls only —
  on a `"legacy"` roll the residual includes whatever the percentile stretch
  left behind, and the comparison says nothing about the estimator.

Only the constants move; the shapes are settled. Any retune must keep the
neutral-is-byte-identical property and the one-point parity property, which
R-0's tests 1 and 6 assert.
