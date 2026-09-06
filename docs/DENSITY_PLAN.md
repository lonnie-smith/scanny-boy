# Density plan: print density, zone density, toe/shoulder and auto metering

Seven new controls and two buttons on the Edit tab's tone panel and on
`edit tone`, ported from NegPy's Exposure panel:

| control | range | neutral | effect |
|---|---|---|---|
| **Print Density** | 0.0–2.0 | 1.0 | overall print brightness; higher is denser (darker) |
| **Shadows Density** | ±0.9 | 0 | mid-sparing density offset on the quarter tone; positive is darker |
| **Highlights Density** | ±0.5 | 0 | the same on the three-quarter tone |
| **Toe** | −1.0…1.0 | 0 | shadow roll-off; positive lifts the black, negative crisps it |
| **Toe Width** | 0.1–5.0 | 2.5 | how far up the scale the toe reaches |
| **Shoulder** | −1.0…1.0 | 0 | highlight roll-off; positive holds the white back |
| **Shoulder Width** | 0.1–5.0 | 2.5 | how far down the scale the shoulder reaches |

plus **Auto Density** and **Auto Grade** buttons that solve a value from the
negative's own recorded metering and write it into the sliders.

They join `grade_r` and `snap_gamma` on the existing `tone` op
(`docs/DECISIONS.md`, "The preview's tone adjustment"). Everything that
decision established still holds: **the op is preview-only** — the published
TIFF and `export` never see it — and **it is a state, not a transform**,
coalesced in place as the latest op.

**Not in scope:** paper white (Dmin) and paper black (Dmax). Our curve's
endpoints are display black and white, not a paper's physical densities, so
those two NegPy toggles have nothing to act on here. Where NegPy's math
depends on `d_min` / `d_max`, this plan says so and re-derives without them.

This plan follows the conventions of `docs/GRID_STITCH_PLAN.md`: numbered
chunks, each independently green, every constant in exactly one module,
every calibration named and justified where it lives.

---

## 0. Why this shape

### 0.1 Know the domain before you touch the math

Three different value spaces meet here. Getting them straight is most of the
work:

- **Normalized log density `val ∈ [0, 1]`** — what the published TIFF holds.
  `normalize_log_image` maps `floors → 0`, `ceils → 1`, and log density is
  *negative and thinner-is-larger*, so **`floors` is the dense end**.
  Therefore **`val = 0` is the densest negative — the scene highlight — and
  prints white.**
- **Positive display value `v ∈ [0, 1]`** — `v = 1 - val`, higher is
  brighter. This is what `tone.py` curves.
- **NegPy's reflection density `D ∈ [0, 2.3]`** — higher is *darker*, and
  logarithmic.

NegPy's axis is flipped relative to ours *and* on a different scale, so its
formulas cannot be transcribed. What transfers is the **structure** — where
in the chain each control acts, and what shape it has — and the
**vocabulary**. That is exactly the boundary `docs/DECISIONS.md` already
drew for Grade: NegPy's R115 is a real paper grade, ours is a reference
slope chosen to look right, and "the numbers are a judgement aid, not a
calibrated paper". Everything added here inherits that status.

### 0.2 Keep NegPy's user-facing ranges verbatim

Every range in the table above is NegPy's own
(`negpy/desktop/view/sidebar/tone.py:121-122, 213-229`,
`docs/USER_GUIDE.md` §6.2). The internal constants absorb the domain
difference.

This is the established precedent, not a new one: `grade_r` kept 50–180 and
`snap_gamma` kept −0.5…0.5 for the same reason — so the Phase 4 print stage
"inherits the vocabulary without inheriting this implementation", and so a
user moving between the two apps reads the same numbers off the same
sliders. Do not "rationalise" the ranges into our own display units.

NegPy's zone asymmetry (±0.9 shadows vs ±0.5 highlights) comes from density
being log10 — the same ΔD reads smaller near paper black than near paper
white. That rationale does *not* survive the port to a linear display value.
Keep the asymmetry anyway, because it lands in the right place for a
different reason: the shadow control needs more travel than the highlight
one to reach a fully blocked shadow from the quarter tone. Say so in the
comment; do not repeat NegPy's log10 justification, which would be false
here.

### 0.3 Where each control acts

Mirroring `CharacteristicCurve.__call__`
(`negpy/features/exposure/logic.py:341-365`) exactly:

1. straight line about the pivot (Grade) — **Print Density offsets the input
   pivot here**
2. midtone gamma (Snap)
3. **zone density offsets**, weights read on the *post-Snap* value
4. **softplus toe and shoulder knees** — now parameterised
5. endpoint rescale

Step 3 reading the post-Snap value is what makes the zone controls
mid-sparing *as the grade moves the tone*: the quarter tone the shadow
slider grabs is the quarter tone of the print you are looking at, not of the
input. Do not hoist the weights onto `values`.

### 0.4 Auto Density and Auto Grade are buttons, not modes

This is the one place where our behaviour deliberately differs from NegPy's,
and it follows from the architecture rather than from taste.

NegPy's `auto_exposure` / `auto_normalize_contrast` are **checkboxes**: its
engine re-meters the image on every render, so the mode can stay on and keep
adapting. Our tone adjustment is a **1D display LUT with no knowledge of the
image** — that is the whole reason it costs one table lookup and the whole
reason the exporter can ignore it. A persistent auto mode is not
representable in it.

So ours **solve once and write a number**: press Auto Grade, the CLI reads
that negative's recorded metering, computes a `grade_r`, and records it as
an ordinary value in the tone op. The slider jumps to it and the user can
drag from there. This keeps the invariant that makes everything else work —
**a `tone` op's params are the complete tone state, never a mode and never a
delta** — and it is the honest UI: the number you see is the number that
rendered the preview.

Two consequences to document rather than hide:

- **A multi-negative selection solves per negative.** Each frame gets its
  own value, which is the entire point of auto metering. The panel's sliders
  sync to the anchor negative, as they already do.
- **Auto does not re-run.** Re-stitching a negative changes its metering but
  not its recorded tone op. That is correct — the user's chosen tone is not
  something a re-stitch should silently overwrite.

### 0.5 The metering already exists — do not write a meter

`stitch_pipeline.py:1789` already records a `normalization` block on every
published negative, and it already carries exactly what NegPy's two auto
features consume (`cli/src/scanny_boy/normalization.py:427-445`,
ported from NegPy in the first place):

- `anchor` — the P50 of the log luma over the analysis region
  (NegPy's `anchor_meter_percentile = 50.0`), in log10 density
- `textural_range` — the P90−P10 log-luma spread
  (NegPy's `textural_range_clip = 10.0`), in log10 density
- `floors` / `ceils` — the per-channel normalization bounds, in log10 density

So **Auto Density and Auto Grade are closed-form from the roll manifest**.
No image is decoded, no meter is written, no block-median prefilter is
needed (the recorded values already ran through one at stitch time). The
solve is instant, deterministic and unit-testable without a TIFF. Do not
add a metering pass.

---

## 1. The curve math

All of this lands in `cli/src/scanny_boy/tone.py`. Nothing else computes it.

### 1.1 Print Density: an input-pivot offset

NegPy's `compute_pivot` (`logic.py:875-889`) ends with
`base + (1.0 - density) * density_multiplier`: the density slider translates
the curve's pivot, so Grade still rotates about the reference tone and does
not double as a brightness control. Transfer it directly — both domains are
the normalized `[0, 1]` tone scale — with the sign flipped because ours is
the positive:

```
pivot_in = 0.5 + (density - 1.0) * DENSITY_PIVOT_SHIFT
v        = 0.5 + slope * (values - pivot_in)
```

The *output* pivot stays 0.5, so Grade's rotation centre is unmoved and
`density = 1.0` is byte-identical to today's
`v = pivot + slope * (values - pivot)`.

`DENSITY_PIVOT_SHIFT = 0.2` — NegPy's `density_multiplier`, unchanged. Full
travel is ±0.2 of the tone scale, about ±1.4 stops at the default grade.

Snap stays centred on the output pivot (0.5), *not* on the shifted input
pivot — matching NegPy, whose midtone gamma is centred on the fixed `v_star`
and is likewise independent of the density slider.

### 1.2 Zone density: mid-sparing sigmoids on the quarter tones

NegPy (`logic.py:357-360`):

```
w_zsh = σ(zone_k · (v − zone_sh_center))       # v is density: high = dark
w_zhi = 1 − σ(zone_k · (v − zone_hi_center))
v    += shadow_density · w_zsh + highlight_density · w_zhi
```

Ported, with the axis flipped so both terms **subtract** (positive adds
density = darker, in a domain where higher is brighter):

```
w_sh = σ(ZONE_SHARPNESS · (ZONE_SHADOW_CENTRE - v))
w_hi = σ(ZONE_SHARPNESS · (v - ZONE_HIGHLIGHT_CENTRE))
v    = v - ZONE_DENSITY_SCALE * (shadow_density * w_sh + highlight_density * w_hi)
```

**Centres.** Do not try to map NegPy's absolute density coordinates
(`anchor_target_density ± zone_density_*_offset`, on a 2.3-wide axis with
the anchor at 0.75) onto our scale — density → reflectance is `10^-D`, so
the mapping is not linear and the arithmetic would be false precision.
NegPy itself sanctions the alternative for exactly this case: with Normalize
off, "the centres are mapped by position on each curve's own scale, not by
raw density" (`docs/USER_GUIDE.md:543`). NegPy's offsets sit roughly **half
way from the midtone to each end** (0.75 of the 1.55 to d_max; 0.40 of the
0.75 to d_min), so on our scale:

```
ZONE_SHADOW_CENTRE    = 0.25   # the quarter tone
ZONE_HIGHLIGHT_CENTRE = 0.75   # the three-quarter tone
```

**Sharpness.** NegPy's `zone_density_sharpness = 4.0` on a 2.3-wide density
axis is a sigmoid transition width of `1/4.0 = 0.25` density units, i.e.
0.109 of the full scale. The same *fractional* width on our unit scale is
`4.0 × 2.3 ≈ 9.2`:

```
ZONE_SHARPNESS = 9.0
```

(Numerically equal to `KNEE_SHARPNESS`; that is a coincidence, keep them
separate constants with separate derivations.)

**Scale.** `ZONE_DENSITY_SCALE = 0.28` converts a ΔD slider unit into
display-value units. It is a calibration, not a conversion — pinned so that
full shadow travel just blocks the deepest shadows without touching the
midtone. At `shadow_density = 0.9`:

| tone             | `w_sh` | Δv     |
|------------------|--------|--------|
| deep shadow v=0  | 0.905  | −0.228 |
| quarter v=0.25   | 0.500  | −0.126 |
| midtone v=0.5    | 0.095  | −0.024 |
| highlight v=0.85 | ~0.005 | −0.001 |

At `highlight_density = 0.5` the mirror image is roughly half the travel,
matching NegPy's asymmetry. **See §11 — this constant wants one visual check
before it is final.**

### 1.3 Toe and Shoulder: parameterise the knees that are already there

`_curve_raw` already ends with a fixed softplus lower bound at 0 and upper
bound at 1 — the H&D knee shape, with no controls on it. NegPy's toe and
shoulder are the same two bounds with the bound *position* and the softplus
*sharpness* exposed (`logic.py:322-339, 361-363`).

Our domain makes this simpler than NegPy's, because our lower bound already
*is* the shadow. NegPy's `d_max_eff` (its toe) is an upper bound in density
space; ours is the lower bound in display space. No re-ordering is needed —
the existing lower-bound-then-upper-bound sequence is already toe then
shoulder.

```
a_base = KNEE_SHARPNESS * max(slope, 1.0)              # today's sharpness
a_toe      = a_base * WIDTH_REFERENCE / toe_width
a_shoulder = a_base * WIDTH_REFERENCE / shoulder_width

toe_floor     = toe * TOE_HEIGHT       if toe >= 0      else 0.0
a_toe        *= 1.0 - toe * KNEE_SHARPEN                if toe < 0
shoulder_ceil = 1.0 - shoulder * SHOULDER_HEIGHT if shoulder >= 0 else 1.0
a_shoulder   *= 1.0 - shoulder * KNEE_SHARPEN           if shoulder < 0

if shoulder_ceil < toe_floor + 0.1:                     # NegPy's collapse guard
    shoulder_ceil = toe_floor + 0.1

v = toe_floor     + softplus(a_toe      * (v - toe_floor))     / a_toe
v = shoulder_ceil - softplus(a_shoulder * (shoulder_ceil - v)) / a_shoulder
```

**Widths.** `WIDTH_REFERENCE = 2.5` — NegPy's `toeshoulder_width_ref`, which
is also its default width, so a width of 2.5 reproduces today's sharpness
exactly and the neutral curve is unchanged. NegPy's two different sharpness
bases (`toe_sharpness_base = 4.0` vs `shoulder_sharpness_base = 3.0`) exist
because *its* axis is logarithmic and the toe needs to be crisper to read
the same. Ours is linear in display value and already uses one sharpness for
both ends; keep `KNEE_SHARPNESS` shared, and say why in the comment.

**Heights.** Derived from where NegPy's full-travel knees actually land once
they are decoded through `10^-D`, black point compensation and the working
OETF:

- toe = 1: `d_max_eff = 2.3 − 0.85·0.90 = 1.535` → ≈ **0.19** display
- shoulder = 1: `d_min_eff = 0.85·0.35 = 0.2975` → ≈ **0.73** display

```
TOE_HEIGHT      = 0.18    # toe = 1 floors the blacks at 0.18
SHOULDER_HEIGHT = 0.25    # shoulder = 1 ceils the whites at 0.75
```

NegPy's global `toe_shoulder_strength = 0.85` is already folded into both —
do not apply it a second time.

**Negative values sharpen instead of moving the bound.** NegPy does this for
the toe (`a_sh = a_sh_base * (1.0 - toe_eff * 4.0)`), giving a crisper black
rather than a lift. `KNEE_SHARPEN = 3.4` is that `4.0` with the 0.85 folded
in, so `toe = −1` gives a 4.4× crisper knee, matching NegPy.

**We extend it to the shoulder, which NegPy cannot.** In NegPy a negative
shoulder is inert: `d_min_eff = max(0.0, d_min + shoulder·ts·height)`
clamps at the paper's physical Dmin. We are not modelling paper white, so
our ceiling is display white and there is nothing to clamp against — half
the shoulder slider's travel would otherwise do nothing. Apply the same
sharpening branch. **Flag this in `docs/DECISIONS.md` as a deliberate
deviation**, with the reason.

### 1.4 Monotonicity is a hard constraint on the constants

The zone terms are the only part of the curve that can un-monotone it — the
knees are softplus bounds and the density shift is a translation, both
monotone by construction. With `S = ZONE_DENSITY_SCALE`,
`k = ZONE_SHARPNESS`:

```
dv_out/dv = 1 + S·k·(shadow_density·σ'_sh − highlight_density·σ'_hi),   σ' ≤ 1/4
```

A conservative sufficient bound, which the current constants satisfy with
comfortable margin:

```
(|shadow|max + |highlight|max) · ZONE_DENSITY_SCALE · ZONE_SHARPNESS / 4 < 1
(0.9 + 0.5) · 0.28 · 9 / 4 = 0.882
```

**Assert this inequality in a test**, so any later retune of the scale, the
sharpness or the ranges trips rather than silently producing a non-monotone
LUT.

### 1.5 The endpoint rescale must read its anchors with every shaping control at rest

`curve_values` currently pins the endpoints by rescaling with
`low = _curve_raw(0)`, `high = _curve_raw(1)`, so even the softest grade
reaches full black and white.

**Every control in this plan moves the endpoints, and the rescale would undo
all of them.** Print Density would be nearly inert; Toe and Shoulder would be
*completely* inert, since moving the endpoints is precisely what they do.

The rule, which covers all seven at once: **the anchors are read with grade
and snap only.**

```python
neutral = dataclasses.replace(params, density=1.0, shadow_density=0.0,
                              highlight_density=0.0, toe=0.0, shoulder=0.0,
                              toe_width=WIDTH_REFERENCE,
                              shoulder_width=WIDTH_REFERENCE)
low  = float(_curve_raw(np.array([0.0]), neutral)[0])
high = float(_curve_raw(np.array([1.0]), neutral)[0])
```

The endpoint pin exists so that a *grade* still reaches black and white, so
its anchors belong to the grade. The trailing `np.clip(raw, 0.0, 1.0)` then
does what a print does when you overexpose it: blocks up. That is honest,
and the knees keep it gentle.

This also gives the property the whole port hangs on: **at neutral values
for all seven, the LUT is bit-identical to today's**. Existing previews do
not shift.

### 1.6 Nine parameters means a value object

`grade_r`, `snap_gamma`, `density`, `shadow_density`, `highlight_density`,
`toe`, `toe_width`, `shoulder`, `shoulder_width` threaded as loose floats
through `tone.py` → `previews.py` → `edits.py` → `repo.py` → `cli.py` is not
workable. Introduce one frozen dataclass in `tone.py` and thread it
everywhere:

```python
@dataclass(frozen=True)
class ToneParams:
    grade_r: float = GRADE_REFERENCE          # 115.0
    snap_gamma: float = 0.0
    density: float = DENSITY_REFERENCE        # 1.0
    shadow_density: float = 0.0
    highlight_density: float = 0.0
    toe: float = 0.0
    toe_width: float = WIDTH_REFERENCE        # 2.5
    shoulder: float = 0.0
    shoulder_width: float = WIDTH_REFERENCE

NEUTRAL = ToneParams()

def curve_values(values: np.ndarray, params: ToneParams) -> np.ndarray: ...
def build_display_lut(params: ToneParams) -> np.ndarray: ...
```

The field names are exactly the `tone` op's param keys, so `previews.py`
becomes `tone.build_display_lut(tone.ToneParams(**tone_params))` — one line,
because §6 guarantees the dict is always complete.

This is §3's own chunk, done first, as a pure refactor.

---

## 2. Auto Density and Auto Grade

A new module, `cli/src/scanny_boy/auto_tone.py`: pure functions over a
negative's `normalization` record, no I/O, no image. It keeps manifest
knowledge out of `tone.py` and metering knowledge out of `edits.py`.

```python
def solve_density(normalization: dict | None) -> float | None
def solve_grade(normalization: dict | None) -> float | None
```

Both return `None` when the record is missing, incomplete or degenerate;
the caller then leaves the value alone and warns (§2.4).

### 2.1 The shared read

```python
w = luma weights, one per published channel   # (0.2126, 0.7152, 0.0722), or (1.0,) on a mono roll
luma_floor = Σ w·floors      luma_ceil = Σ w·ceils
span = luma_ceil - luma_floor                 # > 0 by construction; guard < 1e-6 anyway
```

Reuse `normalization.LUMA_*` and mirror `luma_of_log`'s single-channel case
(`normalization.py:150-156`) — a mono roll's record carries one channel, and
a hardcoded 3-tuple would crash on it.

### 2.2 Auto Density

The recorded `anchor` is the frame's median in log10 density. Normalize it
through the same bounds the TIFF went through, then convert to the positive:

```
measured = clamp((anchor - luma_floor) / span, 0.0, 1.0)   # 0 = dense = prints white
```

NegPy's partial metering (`normalization.py:465-511`) is the whole point of
this control and must be ported, not simplified away: the anchor moves only
`anchor_meter_strength` of the way toward the measurement, hard-clamped to
`± anchor_meter_band`, "so a deliberately low-key or high-key scene keeps
most of its intended key instead of being forced to mid-gray, while gross
mis-exposure is still pulled toward correct".

We want the metered midtone to print at the curve's pivot, i.e.
`pivot_in = 1 - anchor`. Solving §1.1's `pivot_in` for `density` and folding
the partial pull in:

```
density = 1.0 + ANCHOR_METER_STRENGTH * (ANCHOR_ASSUMED - measured) / DENSITY_PIVOT_SHIFT
density = clamp(density, 1.0 ± ANCHOR_METER_BAND / DENSITY_PIVOT_SHIFT)
density = clamp(density, DENSITY_MIN, DENSITY_MAX)
```

```
ANCHOR_ASSUMED        = 0.5    # our pivot; see below
ANCHOR_METER_STRENGTH = 0.2    # NegPy's, unchanged
ANCHOR_METER_BAND     = 0.12   # NegPy's, unchanged
```

`ANCHOR_ASSUMED = 0.5`, not NegPy's 0.46. NegPy's 0.46 is a calibrated
"typical negative's normalized median" measured against its paper model; our
baseline is the flat mapping, whose pivot is 0.5 by construction. Using 0.5
is what makes a frame metering dead-centre solve to `density = 1.0` — Auto
Density is a no-op on a frame that needs nothing, which is the property that
makes the button trustworthy.

With strength and shift both 0.2 the coefficient is exactly 1, so
`density = 1.0 + (0.5 - measured)`, bounded to ±0.6. Note the coincidence in
a comment so nobody "simplifies" the constants out of the expression.

### 2.3 Auto Grade

NegPy's `effective_grade_range` (`logic.py:829-855`) damps the frame's
extremes-to-textural ratio toward a nominal negative:

```
textural = abs(textural_range)                       # log10 density, from the record
ratio    = span / textural                           # both in log10 density: directly comparable
effective = AUTO_GRADE_TARGET * (NOMINAL_RATIO + AUTO_GRADE_STRENGTH * (ratio - NOMINAL_RATIO))
```

NegPy then feeds `effective` to its own `grade_to_slope`, which is *not* our
grade mapping — so invert ours instead. With
`slope = GRADE_SLOPE_REF · GRADE_REFERENCE / grade_r` (`tone.grade_slope`)
and the nominal range `NOMINAL_RANGE = AUTO_GRADE_TARGET · NOMINAL_RATIO`
(NegPy's `default_grade_range()` = 1.2):

```
grade_r = clamp(GRADE_REFERENCE * NOMINAL_RANGE / effective, GRADE_MIN, GRADE_MAX)
```

```
AUTO_GRADE_TARGET   = 0.6    # NegPy's, unchanged
AUTO_GRADE_STRENGTH = 0.5    # NegPy's, unchanged
NOMINAL_RATIO       = 2.0    # NegPy's auto_grade_nominal_ratio
```

Which reduces to `grade_r = 230 / (1 + 0.5·ratio)`. Check the behaviour:

| ratio | meaning | `grade_r` |
|---|---|---|
| 2.0 | a nominal negative | **115** — Auto Grade is a no-op |
| 1.2 | textural range fills the scale: a contrasty scene | 144 (softer) |
| 4.0 | extremes far wider than the picture: a flat scene with speculars | 77 (harder) |

Landing exactly on the default at a nominal frame is the same trustworthiness
property as §2.2's, and it falls out of the derivation rather than being
tuned in. **Assert it in a test.**

Degenerate frames: `textural < 1e-6` takes NegPy's branch and uses its
`3.5` range cap, which the `[50, 180]` clamp then absorbs.

### 2.4 When metering is unavailable

`normalization` is `null` on a negative published by a build that predates
it. `edit tone` already requires a stitched negative, so this is rare — but
it must not fail the edit. New warning code:

```
TONE_METERING_UNAVAILABLE
```

Emitted per negative that could not be solved. The op still records, with
the explicitly-given value or the neutral default; auto simply did not
apply. Add it to `events.Code`, `schema.json`'s `code` enum and
`CONTRACT.md`'s code table.

---

## 3. Chunk D-0 — the value object

Pure refactor, no behaviour change, no new controls. Doing it first means
every later chunk is a small diff.

- `tone.ToneParams` and `tone.NEUTRAL` per §1.6, with the **existing five**
  fields only (`grade_r`, `snap_gamma` — the other seven arrive in D-1 with
  their defaults; add them all now if you prefer one churn instead of two).
- `curve_values(values, params)`, `build_display_lut(params)`.
- `previews._encode_display_png` builds `ToneParams(**tone_params)`.
- `repo.validated_tone_params(params: dict | None) -> dict | None` and
  `repo.append_tone_edit(roll_dir, negative_id, params)` take the dict (or
  `None` for the reset) rather than positional floats.
- `edits.run_edit_tone(roll_dir, negative_ids, params, *, emit)`.
- `cli.py` builds the dict.

Existing tests in `tone_test.py`, `edits_test.py` and `cli_test.py` call
these positionally; update them mechanically. **No assertion values change** —
that is the acceptance criterion for this chunk.

---

## 4. Chunk D-1 — `tone.py`

`cli/src/scanny_boy/tone.py` only. Pure functions, no persistence, no CLI.

- constants of §1.1–§1.3, plus a stable sigmoid beside `_softplus`:
  ```python
  def _expit(x):
      return 0.5 * (1.0 + np.tanh(0.5 * np.asarray(x, dtype=np.float64)))
  ```
- the seven new `ToneParams` fields
- `_curve_raw` gains steps 1, 3 and the parameterised step 4 of §0.3, in
  that order
- `curve_values` reads its rescale anchors at neutral (§1.5)
- update the module docstring's bullet list with **Density**, **Zone
  density** and a rewritten **Knees** entry

### Tests (`tone_test.py`)

1. **Neutral is identical.** `build_display_lut(NEUTRAL)` equals the
   pre-change LUT — capture the current table's key codes as literals so the
   assertion survives the refactor.
2. **Density darkens.** Midtone output strictly decreases as density sweeps
   0 → 2; `1.5` darker than `1.0`, `0.5` brighter.
3. **Density does not rotate.** The local midtone slope
   (`f(0.55) - f(0.45)`) is unchanged across densities 0.5/1.0/1.5.
4. **Shadows are mid-sparing.** At `shadow_density = 0.9`: |Δ| at v=0.15 is
   large (> 0.15), at v=0.5 small (< 0.03), at v=0.85 negligible (< 0.01).
   Mirror for `highlight_density = 0.5`.
5. **Signs.** Positive shadow/highlight density darkens; negative brightens.
6. **Toe lifts the black, shoulder holds the white.** `toe = 1.0` puts
   `f(0)` at ≈ `TOE_HEIGHT`; `shoulder = 1.0` puts `f(1)` at
   ≈ `1 - SHOULDER_HEIGHT`; both leave `f(0.5)` within 0.02 of neutral.
7. **Negative toe/shoulder sharpen without moving the bound.** `f(0)` stays
   0 and `f(1)` stays 1, but the curve is closer to the straight line near
   each end than at neutral.
8. **Width widens.** A larger `toe_width` reaches further up the scale — the
   deviation from the straight line at v=0.3 grows with the width — and
   `2.5` reproduces neutral exactly.
9. **Monotone and in range over the whole box.** Extend the existing sweep to
   the corners of all nine parameters.
10. **The constants satisfy §1.4's inequality.**
11. **LUT shape.** Monotone non-increasing at the extremes of density, toe
    and shoulder; endpoints still 255/0 at neutral.

---

## 5. Chunk D-2 — `auto_tone.py`

New module per §2. Pure, no I/O.

### Tests (`auto_tone_test.py`)

- A hand-built record whose anchor sits at the midpoint of `floors`/`ceils`
  solves to **exactly** `density = 1.0`.
- A record whose `span / textural_range` is **exactly 2.0** solves to
  **exactly** `grade_r = 115.0`.
- A dense-metering frame solves brighter, a thin one darker; the band
  clamp holds at ±0.6.
- A wide-extremes frame solves harder, a contrasty one softer; the
  `[50, 180]` clamp holds.
- `None`, `{}`, a record missing `anchor`, a zero span and a zero
  `textural_range` each return `None` rather than raising.
- A **single-channel** (mono roll) record solves without a shape error.

---

## 6. Chunk D-3 — persistence (`library/repo.py`)

**Invariant to state in the module comment and hold everywhere: a `tone`
op's params are the complete tone state, never a delta and never a mode.**
Nine keys always present, or nine explicit nulls for the reset. That is what
makes "latest op wins" coalescing sound, and what `CLIEvent.recordedTone`
and `ToneParams(**tone_params)` both rely on.

- Import the bounds from `tone.py` and delete repo's duplicate
  `TONE_GRADE_*` / `TONE_SNAP_*`. No import cycle exists, and nine
  hand-mirrored bound pairs is where that pattern stops paying.
- `validated_tone_params` — all nine set or all nine `None`; per-field range
  checks; same `ValueError` style.
- `append_tone_edit` — unchanged apart from the dict; coalescing untouched.
- `net_edit_state`, `TONE_OP` branch — **the backward-compatibility point.**
  Rows written before this change carry only `grade_r`/`snap_gamma`. They
  are valid, not malformed:
  - `grade_r`/`snap_gamma` missing or `None` → `tone = None` (reset), as today
  - each new key: `params.get(key)`; `None` or absent → its neutral default
  - out of range on any of the nine → `tone = None`, matching the existing
    "a malformed `tone` op degrades to no adjustment"
  - the returned dict always has all nine keys
- Update the `# tone params are ...` comment and `net_edit_state`'s docstring.

### Tests

- A legacy two-key row inserted directly into the ops log reads back with the
  seven neutral defaults, not `None`.
- Reset writes nine explicit nulls and reads back as `None`.
- Out-of-range values on each new field raise without recording.
- Coalescing still collapses repeated commits to one row.

---

## 7. Chunk D-4 — CLI surface, `edits.py`, protocol bump

### 7.1 `edit tone` flags (`cli.py:350`)

```
--density D            print density, 0.0-2.0 (1.0 neutral, higher is denser)
--shadow-density D     shadows density, -0.9..0.9 (positive adds density)
--highlight-density D  highlights density, -0.5..0.5 (positive adds density)
--toe T                shadow roll-off, -1..1 (positive lifts the black)
--toe-width W          toe extent, 0.1-5.0 (2.5 neutral)
--shoulder S           highlight roll-off, -1..1 (positive holds the white)
--shoulder-width W     shoulder extent, 0.1-5.0 (2.5 neutral)
--auto-density         solve the density from the negative's metering
--auto-grade           solve the grade from the negative's metering
```

Argparse mutually-exclusive groups: `--density` / `--auto-density`, and
`--grade` / `--auto-grade`.

Dispatch (`cli.py:612`): `--auto-grade` satisfies the existing
"grade is required" rule, so the error becomes
`edit tone needs --grade (or --auto-grade) and --snap together, or --reset`.
Everything else keeps its current behaviour — the new value flags are
optional and take their neutral defaults, `--reset` still wins over and
ignores every value flag, and `edit tone --grade 115 --snap 0` still works
and now records the complete nine-key state.

### 7.2 `edits.py`

`run_edit_tone(roll_dir, negative_ids, params, *, auto_density, auto_grade, emit)`.

The solve happens **per negative, inside the existing loop**, after
`_validated_negatives` and before `append_tone_edit`:

```python
solved = dict(params)
if auto_density or auto_grade:
    record = negative.normalization
    if auto_density:
        value = auto_tone.solve_density(record)
        ...  # None -> warn TONE_METERING_UNAVAILABLE, leave params["density"]
    if auto_grade:
        value = auto_tone.solve_grade(record)
```

Validation still runs on the *solved* values, so a solver bug cannot record
an out-of-range op. Warn once per negative, not once per solver.

### 7.3 `roll info` (`cli.py:574-583`)

Seven more derived fields per negative, `null` when flat: `tone_density`,
`tone_shadow_density`, `tone_highlight_density`, `tone_toe`,
`tone_toe_width`, `tone_shoulder`, `tone_shoulder_width`.

### 7.4 `previews.py`

One line: `_encode_display_png` (`previews.py:101`) is the only place that
builds the LUT, and D-0 already changed it. Fix the several docstrings in
the module that spell the params out as `{"grade_r", "snap_gamma"}`.

### 7.5 Protocol 10 → 11 (`events.py:29`)

Bump `PROTOCOL_VERSION` and add the comment block in the existing style:
protocol 11 keeps 10's roll model and adds the seven curve controls and the
two auto flags to `edit tone`, the matching seven `tone_*` fields on
`roll info`'s negatives, and the `TONE_METERING_UNAVAILABLE` warning code.

`EditRecorded` itself does **not** change — the tone state rides the
recorded op's `params`, exactly as it does today.

### Tests (`cli_test.py`, `edits_test.py`)

- All nine flags round-trip through `roll info`.
- `--auto-density` on a stitched fixture records a density derived from that
  negative's recorded metering, not 1.0.
- `--auto-grade` likewise, and the two are independent.
- Auto on a **multi-negative selection** records per-negative values.
- A negative with `normalization = None` warns `TONE_METERING_UNAVAILABLE`
  and still records.
- `--density` with `--auto-density` is a usage error.
- A toe-only change still regenerates the preview and changes its bytes —
  the coalescing path must not short-circuit.
- `edit render-region` honours the recorded curve (extend
  `test_render_region_applies_the_recorded_tone`).
- The published TIFF is still untouched; `export` still ignores the op.
- Emitted lines carry `protocol_version: 11` (`events_test.py`).

---

## 8. Chunk D-5 — contract and docs

- **`shared/contract/CONTRACT.md`** — a "Protocol version 11" paragraph at
  the top in the house style; update the usage line (`:166`) and the
  `edit tone` prose (`:387-399`) with the nine flags, their ranges, their
  sign conventions, the two auto flags and **the fact that auto solves once
  and records a value rather than setting a mode**; add
  `TONE_METERING_UNAVAILABLE` to the warning-code table.
- **`shared/contract/schema.json`** — add `TONE_METERING_UNAVAILABLE` to the
  `code` enum. Nothing else: `edit_recorded`'s `params` is an unconstrained
  object and `"edit tone"` is already in the `command` enum. Verify, don't
  assume.
- **`shared/contract/roll-manifest.schema.json`** — add the seven new
  `tone_*` fields beside `rotation_quarter_turns` in the `negative`
  definition, each `["number", "null"]` with a "Derived, not stored: present
  in `roll info` output only" description. **While you are there, add the two
  that are already missing**: `tone_grade_r` and `tone_snap_gamma` were never
  documented there. (The definition has no `additionalProperties: false`, so
  this is a documentation fix, not a validation fix.)
- **`docs/DECISIONS.md`** — extend "The preview's tone adjustment" with a
  protocol-11 subsection covering the five things that are not obvious from
  the code:
  1. the flipped, rescaled domain, and why the math is re-derived rather
     than ported (§0.1);
  2. why the zone centres are placed by position on our own curve rather
     than by NegPy's density coordinates (§1.2);
  3. why the endpoint rescale reads its anchors with every shaping control
     at rest — without which Toe and Shoulder are inert (§1.5);
  4. **why auto is a button and not a checkbox** (§0.4) — the LUT has no
     image, so a persistent mode is not representable, and the solved value
     is recorded as ordinary state;
  5. the negative-shoulder sharpening we have and NegPy does not, and why
     (§1.3).

  Restate that the ranges are NegPy's vocabulary, not a calibrated paper —
  the same status the grade reference already carries.

---

## 9. Chunk D-6 — the Mac app

### 9.1 Collapse the parameter list first

Nine loose `Double?`s plus two flags through `scheduleTone` / `commitTone` /
`setTone` / `performToneCommit` / `CLICommand.editTone` / the panel's
callbacks is unworkable. Introduce one value type and thread it — `nil`
means the reset:

```swift
struct ToneAdjustment: Equatable, Sendable {
    var gradeR: Double
    var snapGamma: Double
    var density: Double
    var shadowDensity: Double
    var highlightDensity: Double
    var toe: Double
    var toeWidth: Double
    var shoulder: Double
    var shoulderWidth: Double
    static let neutral = ToneAdjustment(...)   // 115, 0, 1, 0, 0, 0, 2.5, 0, 2.5
}
```

Auto rides beside it as a small option set or two booleans on the commit
call, **not** as fields on `ToneAdjustment` — they are a request, not state.
Do this refactor as the first commit of the chunk, before any UI.

### 9.2 The pieces

- **`RollManifest.Negative`** (`:157`) — seven new `Double?` fields decoded
  from the `tone_*` keys, with the same "absent before the op existed"
  comment. Fix up every construction site (`applying(event:)`, the test
  helpers).
- **`CLIEvent.recordedTone`** (`CLIEvent.swift:250`) — return a struct, not a
  growing tuple. Keep gating on the presence of the `grade_r` key; a
  present-but-null `grade_r` is still the reset. **New keys absent → neutral
  defaults**, mirroring §6's Python read path, so an older CLI paired with a
  newer app degrades the same way on both sides.
- **`CLICommand.editTone`** (`CLIRunner.swift:260`) — append the seven value
  flags on the non-reset branch, and `--auto-density` / `--auto-grade` in
  place of `--density` / `--grade` when requested.
- **`EditModel.applying(event:)`** (`:398-440`) — carry all nine through.
- **`EditModel.renderGeneration`** (`:498`) — **must** include every new
  value in the token. Miss this and a toe-only change leaves stale 1:1
  region crops on screen. Nine `%.2f`s is unwieldy; hash the
  `ToneAdjustment` instead and format the hash.

### 9.3 The panel (`EditStageView.swift:449`)

Nine sliders, two buttons and Reset will not fit a 280pt popover. Restructure
into three labelled sections, in NegPy's own panel order — exposure, then
contrast, then the trims — and widen to ~360:

| section | controls |
|---|---|
| **Print** | Print Density (+ Auto), Paper Grade (+ Auto), Snap |
| **Zones** | Shadows Density, Highlights Density |
| **Curve** | Toe, Toe Width, Shoulder, Shoulder Width |

Slider details:

| control | range | step | format | reversed |
|---|---|---|---|---|
| Print Density | 0.0…2.0 | 0.05 | `%.2f` | no |
| Paper Grade | 50…180 | 1 | `R%d` | **yes** |
| Snap | −0.5…0.5 | 0.05 | `%+.2f` | no |
| Shadows / Highlights Density | ±0.9 / ±0.5 | 0.05 | `%+.2f` | no |
| Toe / Shoulder | −1.0…1.0 | 0.05 | `%+.2f` | no |
| Toe / Shoulder Width | 0.1…5.0 | 0.1 | `%.1f` | no |

- Print Density is **not** reversed. Grade is reversed because a lower R is a
  harder paper; density has no such inversion — higher is simply denser.
- `ToneSlider` needs no change — it already snaps, debounces, flushes on
  release and resets on double-click.
- **Auto buttons**: a small `wand.and.stars` button beside the Print Density
  and Paper Grade rows, `.help("Solve the density from this negative's own
  metering")`. On tap, commit immediately (no debounce) with the auto flag
  set and every other value as-is; the returned state drives the sliders.
  Disabled while `isBusy`. They are momentary actions — **do not render them
  as toggles**, and do not persist an "auto is on" flag anywhere.
- `syncFromModel`, the `snapped*` helpers, `Reset` and the `.onChange`
  watchers cover all nine. Reset still means "remove the op", not "set the
  neutrals".
- Update the toolbar button's `.help` and `.accessibilityLabel`
  (`EditStageView.swift:178-179`) — it currently says "paper grade and
  midtone snap".
- **Judgement call**: if three sections at 360pt still overflow the popover
  on a small display, promote the panel to a sheet rather than shrinking the
  type. Collapsing **Curve** into a `DisclosureGroup` is the lighter
  alternative. Pick one and say which in the PR.

### Tests (`EditModelTests.swift`, `CLIEventTests.swift`, `CLICommandTests.swift`)

- `setTone` with a full `ToneAdjustment` records and reflects all nine.
- An auto commit reflects the *solved* values the CLI returned, not the ones
  the app sent.
- A `roll info` payload **without** the new keys yields the neutral defaults,
  not `nil` — the legacy-CLI path.
- `renderGeneration` differs for two negatives that differ only in toe.
- `CLICommand.editTone` emits the value flags on the value branch, the auto
  flags in place of their value flags when requested, and none of them on
  `--reset`.
- `recordedTone` decodes the new keys and defaults the absent ones.
- The existing rotate-leaves-tone-alone test still passes.

---

## 10. Order and verification

D-0 → D-1 → D-2 → D-3 → D-4 → D-5 → D-6. D-0 is a no-assertion-change
refactor; D-1 and D-2 are pure and testable alone; only D-4 changes the
wire; only D-6 needs Xcode.

```bash
cd cli && uv run pytest                       # fast tier, per chunk
cd cli && uv run pytest --slow                # once, after D-4
cd mac && xcodegen generate && xcodebuild test -scheme ScannyBoy -destination 'platform=macOS'
```

Per `AGENTS.md`, the slow tier is what exercises the real TIFF and preview
paths; D-4 touches preview regeneration, so run it there.

---

## 11. The open calibrations

Four numbers in this plan are chosen by eye rather than derived. Everything
else is either NegPy's own constant transferred between comparable domains,
or falls out of an inversion.

| constant | value | basis |
|---|---|---|
| `DENSITY_PIVOT_SHIFT` | 0.2 | NegPy's `density_multiplier`, between two identically normalized `[0,1]` domains — firm |
| `ZONE_DENSITY_SCALE` | 0.28 | pinned so full shadow travel just blocks the deepest shadows (§1.2) — **check** |
| `TOE_HEIGHT` | 0.18 | NegPy's full-travel toe decoded to display (§1.3) — **check** |
| `SHOULDER_HEIGHT` | 0.25 | ditto for the shoulder — **check** |

**After D-4 and before D-6**, render one real frame at each extreme
(`--shadow-density ±0.9`, `--highlight-density 0.5`, `--density 0.5 / 1.5`,
`--toe ±1`, `--shoulder ±1`, and the widths at 0.5 / 5.0), look at the
previews, and confirm the travel feels like NegPy's before the sliders ship.
Also run `--auto-density` and `--auto-grade` over a whole roll and check the
solved values look sane frame to frame.

Only the constants move; the curve's shape is settled. Any retune of the
zone constants must keep §1.4's inequality satisfied — the test will say so.
