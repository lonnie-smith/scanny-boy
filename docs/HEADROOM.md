# Highlight headroom: giving the tone curve something to recover

Three changes to the display render, in order. First a **display domain**
that reaches past 1.0, so the encode's reserved highlight headroom survives
into the curve instead of being clipped at the door. Then the **render's
clips** — the three `1 - val` sites and the post-matrix linear clip — moved
or widened to that domain. Then the **curve's knees**: `toe` and `shoulder`
become knee-point controls with a real rolloff, in place of the
ceiling-lowering softplus that flattens the top end into a plateau.

No pixel in any published TIFF changes. No stitch is re-run. This is
entirely about the path from published codes to display pixels — the same
path `docs/EXPORT_PLAN.md` §4 describes — and both the preview and the
export get the change together, because they share
`render.render_positive_float` and must not drift (EXPORT_PLAN §4.7).

This plan follows the conventions of `docs/EXPORT_PLAN.md` and
`docs/PROFILE_HONESTY_PLAN.md`: every constant lives in exactly one module
and is derived rather than guessed where a derivation exists, and every
rejected alternative is written down beside the one that was taken so a
later reader does not "fix" a deliberate choice.

Two things this plan assumes:

- **The `tone` op's nine-key schema does not change.** `repo.TONE_OP` is
  all-nine-or-all-`None` (`repo.validated_tone_params`), and every added
  key would be a compatibility break for a gain the reshaped controls
  already deliver. §4 spends the existing keys harder instead of adding
  new ones.
- **`NORMALIZED_HEADROOM_LOW` stays at 0.15 for now.** Raising it is a
  `NORMALIZE_FORMAT_VERSION` bump and a re-stitch of every roll (§5.1).
  §1.4's evidence says it is probably too small, but that is a separate,
  larger change and it is worthless until the render stops discarding the
  0.15 it already has.

---

## 0. The complaint, and what it turned out to be

The reported symptom: highlights look blown, the tone panel's controls do
not recover them, and the look got worse when the preview switched to
soft-proofing Adobe RGB (`040760d "Soft-proof when previewing"`).

Three independent defects, each sufficient on its own to produce the
symptom. They compound, and **fixing any one alone changes almost
nothing** — which is why they are one plan and not three.

### 0.1 Defect A — the render discards the headroom the encode reserves

`normalization.encode_normalized` reserves `NORMALIZED_HEADROOM_LOW = 0.15`
at the dense end, and says why:

> Values at `-NORMALIZED_HEADROOM_LOW` and `1 + NORMALIZED_HEADROOM_HIGH`
> survive; beyond them they clip — documented, not accidental: those are
> exactly the speculars and deepest shadows the creative-edit stage's tone
> curve will want, and the headroom keeps them representable.

Normalized `0.0` — the white point — lands at **code 7864**. Codes 0–7863
are scene highlights held *above* display white on purpose: **12.0% of the
published code space.**

Three sites then throw all of it away:

| Site | Expression |
| --- | --- |
| `render.py:238` (`_positive_values`) | `np.clip(1.0 - decode_normalized(codes), 0.0, 1.0)` |
| `render.py:199-201` (`_linear_lut_from_codes`) | `np.clip(1.0 - (norm + offset), 0.0, 1.0)` |
| `tone.py:291-293` (`build_channel_tables`) | `np.clip(1.0 - (norm + offset), 0.0, 1.0)` |

Every one of those 7864 codes becomes display `1.0`. The tone curve is
never handed a value above 1.0, so no tone control can distinguish a
diffuse white from a specular. The low clip in the same expression is
load-bearing and stays — it is what renders the fill sentinel black
(MONOCHROME_PLAN §3.4). Only the high clip is loss.

### 0.2 Defect B — the curve's shoulder is a clip, not a knee

`tone._curve_raw:201-216`. `a_base = KNEE_SHARPNESS * max(slope, 1.0)` is
`9.0 x 1.55 = 13.95` at the reference grade, so the softplus knee is
~`1/a` ≈ 0.07 wide: a hard clip wearing a knee's clothes. Measured, neutral
params, display-in → 8-bit out:

```
0.80 → 238    0.85 → 247    0.90 → 252    0.95 → 254    1.00 → 255
```

The top fifth of the input range occupies the top 7% of the output.

Worse, `shoulder >= 0` lowers a *ceiling*
(`shoulder_ceil = 1.0 - shoulder * SHOULDER_HEIGHT`) rather than extending a
rolloff, so raising the control converts white into flat grey without
recovering any separation at all:

```
                 0.70 0.75 0.80 0.85 0.90 0.95 1.00
neutral           206  223  238  247  252  254  255
shoulder=0.5      201  212  219  222  223  223  223
shoulder=1.0      185  189  191  191  191  191  191   <- plateau
shoulder=1,w=5    173  180  184  187  189  190  191   <- still a plateau
```

`toe` has the identical defect at the other end (`toe=1.0` gives a flat
`46 46 46 46`). `highlight_density` is the only control that keeps tones
apart, and its entire authority is
`HIGHLIGHT_DENSITY_MAX x ZONE_DENSITY_SCALE = 0.5 x 0.28 = 0.14` in v-units,
spent in a region the shoulder has already flattened.

This is why §0.1 alone is not enough. Fed over-range input the present
curve still collapses it:

```
raw curve, inputs 0.90 … 1.15
neutral       0.9877 0.9956 0.9985 0.9995 0.9998 0.9999
shoulder=1.0  0.7496 0.7499 0.7500 0.7500 0.7500 0.7500
```

### 0.3 Defect C — the matrix clips in linear light, before the curve

`export_matrix` is row-normalized, so a neutral survives the gamma sandwich
exactly (the `assert` in `render.export_matrix`). But it strongly amplifies
channel *differences* — a representative Nikon matrix comes out as

```
[ 1.2553 -0.0949 -0.1604]
[-0.3217  1.9659 -0.6442]
[ 0.0065 -0.1951  1.1885]
```

whose green row roughly doubles channel separation. `render.py:337` then
clips the result **per channel, in linear light, before the tone curve**:

```python
linear = linear @ np.asarray(matrix, dtype=np.float32).T
linear = np.clip(preclip, 0.0, 1.0)     # unrecoverable, and hue-shifting
```

Measured through the sandwich:

```
base 0.80 + 0.04 tint → [218, 207, 192]   no clip
base 0.90 + 0.08 tint → [255, 235, 205]   CLIPPED
base 0.95 + 0.04 tint → [255, 245, 230]   CLIPPED
saturated red [217, 89, 76] → [238, 0, 76]   green clipped to zero
```

A mildly warm highlight that was fine in the pre-`040760d` flat preview now
pins a channel at 255, and because the clip is per-channel it skews hue
toward the clipped channel. `_clipped_fractions` already measures exactly
this and writes it to the export's XMP; nothing acts on it.

### 0.4 The evidence this is not theoretical

From `negatives.normalization` in the live library — `headroom_clipped_highlights`
is the fraction of pixels past even the `-0.15` encode rail, and
`observed_min` the pre-clip extreme, per channel:

| negative | clipped highlights | observed_min |
| --- | --- | --- |
| r1-negative-01 | 2.9% / 2.9% / 3.0% | −1.89 / −1.65 / −2.60 |
| 92441b-negative-01 | 3.5% / 3.4% / 3.3% | −4.61 / −3.61 / −2.55 |
| 92441b-negative-02 | 5.7% / 5.5% / 5.3% | −5.33 / −4.29 / −3.17 |
| 92441b-negative-03 | 6.6% / 6.5% / 6.4% | −5.00 / −4.02 / −3.00 |

`HEADROOM_CLIP_WARN_FRACTION` is `0.001`; these are 30x to 66x it. The
cause is structural, not a bad scan: `analyze_bounds` reads **block
medians** (`ANALYSIS_BLOCK_PX`, `BASE_LUMA_CLIP = 0.01`) while
`encode_normalized` runs on **full-resolution pixels**, whose extremes lie
far below any block median. That gap is precisely what the headroom exists
to hold, and it is larger than 0.15.

*(Unrelated, noticed in passing and worth its own look:
r1-negative-01 records `headroom_clipped_shadows = 0.633` on blue.)*

---

## 1. The display domain

One new constant, in `render.py`, **derived** from the encode rather than
chosen:

```python
# The display value the encode's dense-end headroom reaches: normalized
# -NORMALIZED_HEADROOM_LOW inverts to 1 + NORMALIZED_HEADROOM_LOW. Every
# stage between the inversion and the tone curve carries values on
# [0, DISPLAY_CEILING] rather than [0, 1]; the curve is what brings them
# back (docs/HEADROOM.md §3).
DISPLAY_CEILING = 1.0 + normalization.NORMALIZED_HEADROOM_LOW   # 1.15
```

It is derived, so raising `NORMALIZED_HEADROOM_LOW` later (§5.1) needs no
edit here.

**Why the ceiling is 1.15 and not the matrix's true worst case.** With the
linear clip removed, the matrix can reach
`DISPLAY_CEILING ** GAMMA_ADOBE x max_row_positive_gain` = `1.3598 x 1.9659`
= `2.6733` linear, i.e. **display 1.5638** for the matrix above. A ceiling
that high would be lossless but conflates two different things:

- The **headroom** is real, recoverable highlight *detail*, and must reach
  the curve.
- The **matrix excursion** is out-of-gamut *colour*. Adobe RGB cannot
  represent it; discarding it is correct.

So the ceiling is pinned at the headroom, and the post-matrix excursion is
clipped to it (§2.2) — the same thing the code does today, at 1.15 instead
of 1.0 and after the useful range rather than in the middle of it. As a
side benefit the LUT keeps `65536 / 1.15` ≈ 57k levels over `[0, 1]`, which
a ~1.6 rail would cut to 41k.

---

## 2. The render's clips

### 2.1 The three `1 - val` sites: clamp low only

`render.py:199`, `render.py:201`, `render.py:238` and `tone.py:291-293`
become a low-side clamp:

```python
positive = np.maximum(1.0 - (norm + offset), 0.0)
```

The low clamp stays and keeps its comment: it is what takes the fill
sentinel (`NORMALIZED_FILL`, above 1.0 decoded) to black without
special-casing. The high clip goes.

`_linear_lut_from_codes` then raises values up to `DISPLAY_CEILING` to
`GAMMA_ADOBE`, reaching `1.3598` linear. That is fine — it is float32, and
nothing downstream assumes a `[0, 1]` linear range once §2.2 lands.

### 2.2 The linear clip: to the ceiling, and only on the high side

`render.py:337`:

```python
linear = np.clip(preclip, 0.0, DISPLAY_CEILING ** GAMMA_ADOBE)
```

The **low** clamp at 0.0 must stay: `np.power` of a negative base with a
fractional exponent is `NaN`, and negative linear is genuinely out of
gamut. `_clipped_fractions` continues to report against the new bounds, so
the export's XMP provenance keeps meaning what it says — it now reports
only the genuinely out-of-gamut fraction rather than counting recoverable
headroom as a gamut failure, which will make the recorded numbers *drop*.
That drop is the fix working, not a regression; note it where the field is
documented.

### 2.3 The uint16 gather: rescale the domain

`render.py:341` is a live overflow the moment §2.1 lands —
`rint(1.15 * 65535) = 75365`, which wraps to `9829` as uint16 and would
produce a hard, wrong band in the highlights. The display index and the
curve LUT it addresses must share the extended domain:

```python
# render.render_positive_float
j = np.rint(display / DISPLAY_CEILING * MAX_CODE).astype(np.uint16)

# render._curve_lut_from_display_codes
display_codes = np.arange(MAX_CODE + 1, dtype=np.float64) / MAX_CODE * DISPLAY_CEILING
```

Both sites, together, in one commit. Neither is correct without the other,
and the failure mode if they drift is a silently wrong image rather than an
exception — so §7 pins the round trip with a test.

### 2.4 What stays clipped: the flat and negative paths

`render._flat_positive` / `_flat_positive_lut`,
`previews.NORMALIZED_DISPLAY_LUT` and `previews.NEGATIVE_DISPLAY_LUT` keep
their `[0, 1]` clip, and `_curve_lut_from_display_codes` keeps returning a
clipped identity ramp when `tone_obj is None`.

The rule: **the headroom is opened only where a curve exists to absorb
it.** With no tone op there is nothing to compress the extra range into, so
un-clipping would just move the clip to the end of the chain while making
every no-op render darker for no gain. This also preserves two anchors
worth keeping — `render_test.py:232`'s
`preview_lut(None) == previews.NORMALIZED_DISPLAY_LUT`, and the negative
view's honest densitometer reading (`previews.py`'s module docstring).

---

## 3. The curve's knees

`tone._curve_raw:201-216` is replaced. `toe` and `shoulder` stop moving a
floor and a ceiling and start moving a **knee point** — the value at which
compression begins — with the asymptote fixed at 0.0 and 1.0.

### 3.1 The rolloff

```python
def _roll_high(v, knee, width):
    """Compress everything above `knee` toward 1.0. C1-continuous at the
    knee (slope 1 on both sides), monotone, and asymptotic — never
    reaching 1.0, which is what leaves the endpoint rescale something to
    normalize."""
    if knee >= 1.0:
        return np.minimum(v, 1.0)
    head = (1.0 - knee) * (width / WIDTH_REFERENCE)
    return np.where(v <= knee, v, knee + head * (1.0 - np.exp(-(v - knee) / head)))
```

and its mirror `_roll_low` toward 0.0. `head` scales with `*_width`, so the
two width sliders keep meaning something: how far above the knee the curve
reaches before flattening.

Both need `_softplus`'s numerical care — a small `head` overflows `np.exp`
— so guard the exponent the same way, not with `np.errstate`.

### 3.2 The knee mappings

Three-point interpolation on the existing `[-1, 1]` slider range, so the
neutral stays at `0.0` (which `ToneAdjustment.neutral` and `repo`'s reset
both depend on) and the *whole* slider is useful:

```python
# shoulder: -1 = no rolloff at all, 0 = the mild default, +1 = heavy
# highlight compression. Read in post-grade v, not in input display value.
SHOULDER_KNEE = (DISPLAY_CEILING, 0.85, 0.50)
TOE_KNEE = (0.0, 0.06, 0.35)
```

`SHOULDER_HEIGHT`, `TOE_HEIGHT`, `KNEE_SHARPNESS` and `KNEE_SHARPEN` are
deleted. No production module reads them; two tests do, and §7.1 replaces
both.

### 3.3 The endpoint anchor

`tone.curve_values:243-258` reads its `high` anchor at input `1.0`. It must
read at `DISPLAY_CEILING`, or the recovered range lands above 1.0 and is
clipped straight back off:

```python
high = float(_curve_raw(np.array([DISPLAY_CEILING]), neutral_tone, ...)[0])
```

`_neutral_shaping` keeps zeroing the shaping params, which under §3.2 means
the anchor curve carries the *mild default* rolloff rather than none. That
is required, not incidental: the anchor has to span the whole input domain
for the rescale to be meaningful.

### 3.4 Measured result

Composed with the real grade, density and zone stages, 8-bit out:

```
  input            0.30  0.50  0.70  0.80  0.90  1.00  1.05  1.10  1.15
  proposed neutral   48   128   207   238   249   253   254   255   255
  shoulder = -1.0    48   128   207   247   255   255   255   255   255
  shoulder = +0.5    48   128   201   221   234   242   245   247   249
  shoulder = +1.0    48   128   187   205   219   228   232   236   238
  shoulder=1,w=5.0   ..    ..   196   223   246   255   255   255   255

  TODAY  neutral     49   127   206   238   252   255   255   255   255
  TODAY  shoulder=1  49   127   185   191   191   191   191   191   191
```

Two things to read off it:

- **The neutral look barely moves** (48/128/207/238 against 49/127/206/238).
  Existing edits render essentially as they do today, which is the point of
  putting the default knee at 0.85 rather than lower.
- **The control now has leverage.** `shoulder = +1.0` spreads
  `0.70 … 1.15` across 187–238 where today it fuses all of it to a flat
  191. That is the recovery the complaint asked for.

The shadow end behaves the same way — `toe = +1.0` grades `15 19 23 29 36 57`
where today it is a flat `46 46 46 46 48 60`.

### 3.5 Monotonicity

The curve must stay monotone; it is a display transfer and a fold would
show as a posterized band. Swept `shoulder x toe x shoulder_width x
toe_width x grade_r x density x shadow_density x highlight_density` at
7x7x4x3x3x3x3x3 = **47,628 combinations**, 1501 samples each over
`[0, DISPLAY_CEILING]`: **0 non-monotone**. §7 keeps a reduced version of
that sweep as a test.

`shoulder_width = 0.1` degenerates to a plateau at the knee (a hard
shoulder). That is the honest extreme of the control, not a bug, and it is
still monotone.

---

## 4. Widening `highlight_density`

Cheap, independent of §3, and worth doing in the same pass:
`HIGHLIGHT_DENSITY_MAX` of 0.5 against `ZONE_DENSITY_SCALE` of 0.28 gives
the control 0.14 of authority. Once §3 stops flattening the region it acts
on, that authority becomes visible, and the measured
`shoulder=1, highlight_density=0.5` row is a usable combination. Re-measure
before changing the constant: **§3 may be sufficient on its own**, and
widening a control that has just become effective is how a slider ends up
unusable at its extremes. If it is widened, `repo._tone_param_bounds`
reads `tone.HIGHLIGHT_DENSITY_MIN/MAX` directly, so the validation follows
for free — but any recorded edit at the old rail keeps its number and its
meaning, which is why widening is safe where reshaping (§6) is not.

---

## 5. What this plan deliberately does not do

### 5.1 Raise `NORMALIZED_HEADROOM_LOW`

§0.4's evidence says 0.15 is too small — 3–6.6% of pixels clip past it. But
the constant is inside `normalization.build_params`, which rides in
`processing_params` as a **roll invariant**; changing it is a
`NORMALIZE_FORMAT_VERSION` bump from 5 to 6 and makes every existing roll
unstitchable, exactly as `DECISIONS.md`'s "The transfer, the bounds, and
the headroom" says.

Do §1–§4 first and re-measure. The render currently discards 100% of the
headroom it has, so *no* value of the constant is observable today; tuning
it before the render can show the difference would be tuning blind.

### 5.2 Make the gamut clip hue-preserving

§0.3's `[217, 89, 76] → [238, 0, 76]` is per-channel clipping shifting hue
on the most saturated pixels. Scaling all three channels by the max
excursion instead would preserve hue at the cost of luminance, and
`render.py`'s docstring already accepts per-channel clipping as "the
ordinary, accepted behaviour of every matrix-based render".

Moving the clip to `DISPLAY_CEILING` (§2.2) removes most of the *occurrences*
without changing the *rule*, which is the large majority of the benefit for
none of the argument. Record the rest on `punchlist.md` as "hue-preserving
gamut clip in `render_positive_float`", with the note that
`_clipped_fractions` is the measurement that says whether it is worth it.

### 5.3 Add a `tone` param

See the second assumption above. Nine keys, unchanged.

---

## 6. The compatibility break, and how loud it is

Recorded `tone` ops keep their values; those values change meaning:

- `shoulder = 0.0` and `toe = 0.0` — the overwhelming majority of recorded
  edits, and the reset — render **essentially unchanged** (§3.4).
- A nonzero `shoulder` or `toe` renders differently: it now compresses
  where it used to lower a ceiling. The old rendering is not recoverable by
  any new parameter value, because the old shape (a flat plateau) is not in
  the new family.

Under the project's no-migration decision this is acceptable, and it is not
a *silent* difference — it is the visible fix the plan exists to make. Do
**not** add a `tone` op version field or a compatibility branch: it would
put two curve families in `_curve_raw` permanently to preserve a rendering
whose flatness is the defect.

The roll invariants are untouched. No published TIFF, no ICC profile, no
`processing_params` entry moves, so nothing needs re-stitching and no
existing roll fails `check_roll_invariants`.

---

## 7. Tests

### 7.0 Domain and clips (§1–§2)

- `decode_normalized(0)` inverts to `DISPLAY_CEILING`, and the positive
  LUT built by `tone.build_channel_tables` is strictly monotone across
  codes 0–7864 rather than constant — the direct regression test for §0.1.
- The uint16 gather round trip: for display values spanning
  `[0, DISPLAY_CEILING]`, `j` stays in range and
  `curve_lut[j] ≈ curve_values(display)` to within a quantization step. The
  overflow this catches (`75365 → 9829`) is silent otherwise.
- A neutral `(x, x, x)` still survives the gamma sandwich unchanged, at
  `x = 1.0` **and** at `x = DISPLAY_CEILING`. `export_matrix`'s row-sum
  assertion guarantees it; this pins that the extended domain did not break
  it.
- A warm highlight that clips today (`base 0.90 + 0.08 tint`) no longer
  clips, and its channel ordering is preserved.
- `_clipped_fractions` reports zero for an in-gamut image whose values run
  into the headroom — the fraction now means "out of gamut", not "bright".

### 7.1 Two existing tests pin the defect and must be rewritten

Both are in `tone_test.py`, and both currently pass by asserting exactly
the behaviour §3 removes. Neither should be deleted — each is testing a
real property, just stating it in terms of the old shape:

- `test_toe_lifts_the_black_and_shoulder_holds_the_white:110`. Its
  endpoint assertions read `TOE_HEIGHT` and `1.0 - SHOULDER_HEIGHT`, i.e.
  they assert the plateau. Rewrite as the property underneath: `toe = 1.0`
  raises the black **and keeps the shadows ordered**, `shoulder = 1.0`
  lowers the white **and keeps the highlights ordered**. Keep both
  midtone assertions unchanged — `curve_values(0.5)` stays at 0.5 under
  the new knees (measured: 128 at neutral and at `shoulder = 1.0` alike),
  and a control that silently moved the midtone would be a real bug.
- `test_negative_toe_and_shoulder_sharpen_without_moving_bounds:121`.
  "Sharpen" is the old softplus `KNEE_SHARPEN` vocabulary. Under §3.2 a
  negative `shoulder` moves the knee *out* to `DISPLAY_CEILING` rather
  than sharpening a softplus, so restate it as: negative values approach
  the un-rolled-off ramp without moving the endpoints. Same intent, and
  it is the test that keeps the "off" position honest.

### 7.2 New and extended coverage

**Curve shape (§3)**

- Monotone over `[0, DISPLAY_CEILING]` across a reduced version of §3.5's
  sweep. Keep it under a second; mark it `slow` if it is not.
- The plateau regression, stated as a property: with `shoulder = 1.0`,
  `curve_values` at 0.90, 1.00 and 1.15 are **strictly increasing**. This
  is the test that fails today and is the whole point.
- `curve_values(0.0) == 0.0` and `curve_values(DISPLAY_CEILING) == 1.0` at
  neutral — the endpoint anchor of §3.3.
- C1 continuity at the knee: the numerical derivative either side of
  `knee` agrees to a small tolerance, for several `shoulder` values.
- `shoulder = -1.0` reproduces the un-rolled-off ramp (the "off" position
  is genuinely off).
- No overflow warning at `shoulder_width = 0.1` / `toe_width = 0.1`
  (`filterwarnings("error")`).

**Preview / export agreement**

- EXPORT_PLAN §4.7's existing anchor still holds: `render_export` at 16
  bits and `encode_positive_uint8` at 8 agree after scaling, with a tone op
  whose `shoulder` is nonzero and with source codes inside the headroom.
  This is the test that catches §2.3 landing in one of the two paths only.

---

## 8. Order of work

1. **§1 + §2.3 together.** `DISPLAY_CEILING`, the rescaled gather, the
   rescaled curve-LUT domain. No visible change yet — the clips are still
   at 1.0 — but the overflow landmine is disarmed before anything can
   reach it.
2. **§3.** The knees, the anchor, the monotonicity sweep. Visible on the
   `shoulder`/`toe` controls immediately; still no headroom, because §2.1
   has not landed.
3. **§2.1 + §2.2.** Open the three `1 - val` sites and move the linear
   clip. This is the step where the headroom appears, and it appears into
   a curve that can already absorb it.
4. **§4.** Re-measure `highlight_density` and decide whether to widen it.
5. **Swift: nothing.** `EditStageView`'s ranges, steps and reset values
   are unchanged, `ToneAdjustment` keeps its nine fields, and the two
   sliders' help strings already read "Shadow roll-off" and "Highlight
   roll-off" — the UI has been describing §3's behaviour all along. The
   panel needs no edit; confirm `EditModelTests` still passes and move on.
6. **Docs.** `DECISIONS.md`'s "The transfer, the bounds, and the headroom"
   gains a paragraph: the headroom is now *used*, not merely reserved, and
   the display domain is `[0, DISPLAY_CEILING]` from the inversion to the
   curve. `EXPORT_PLAN.md` §0.2's chain diagram gains the domain
   annotation and loses "clip to [0, 1]". `tone.py`'s module docstring
   loses the softplus toe/shoulder description and gains the knee one.

Steps 1–3 are the plan; each is independently landable and testable, and
none is useful alone.

---

## 9. Out of scope

- **The blue-channel shadow clip on r1-negative-01** (§0.4's 63%). Almost
  certainly a normalization-side issue, not a render-side one, and it
  belongs with `docs/BLACK_POINT_REFINEMENT.md` rather than here.
- **`auto_tone`'s solves.** `solve_density` and `solve_grade` read
  `base_slope_and_pivot`, which §3 does not touch; they keep working. Once
  the headroom is visible, an auto *shoulder* solve from
  `headroom_clipped_highlights` and `observed_min` becomes possible, and
  those numbers are already recorded per negative. Punchlist it as "auto
  shoulder from the recorded headroom clip".
- **The colour stage.** `color.cast_slopes` and `region_cmy` compose into
  the same `v` and inherit the wider domain for free. Nothing in
  `COLOR_PLAN.md` needs to move; verify with the existing colour tests
  rather than adding new ones.
