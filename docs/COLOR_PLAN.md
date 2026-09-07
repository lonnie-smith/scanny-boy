# Colour plan: white balance, cast removal and dye separation on the preview

Six new controls on the Edit tab and on a new `edit color` subcommand,
ported from NegPy's Colour panel and the dye-separation pair in its Tone
panel:

- **Temperature** — a Kelvin lever over the selected region's magenta and
  yellow. 3000–12000 K, **5500 K neutral**. Derived, never stored.
- **Cyan / Magenta / Yellow** — enlarger filtration. ±1.0 each, 0 neutral.
- **Cast Removal** — balances each layer against the frame's own greys.
  0.0–1.0, **0 neutral**.
- **Region selector** — Global / Shadows / Highlights, which of the three
  tonal ranges Temperature and CMY act on. Nine stored CMY values, three
  per region.
- **Dye Separation** — saturation in density space. 0.5–1.5, **1.0
  neutral**.
- **Separation Damping** — decides *where* Dye Separation's push lands.
  0.0–1.0, 0 neutral; dead (and disabled) at Dye Separation 1.0.
- **Reset** — per region (that region's three CMY back to neutral) and
  whole-panel (remove the op).

Every one of them is **disabled on a monochrome roll**, which has no
layers to balance and no colour to separate (§6).

They land as a new `color` op (`repo.COLOR_OP`), a sibling of the `tone`
op and subject to the same two boundaries `docs/DECISIONS.md` already
established for it: **the op is preview-only** — the published TIFF and
`export` never see it — and **it is a state, not a transform**, coalesced
in place as the latest op.

This plan follows the conventions of `docs/DENSITY_PLAN.md` and
`docs/GRID_STITCH_PLAN.md`: numbered chunks, each independently green,
every constant in exactly one module, every calibration named and
justified where it lives.

---

## 0. Why this shape

### 0.1 This plan stacks on the density plan

`docs/DENSITY_PLAN.md` (chunks D-0…D-6) must land first. This plan reuses:

- `tone.ToneParams` and the `curve_values(values, params)` signature (D-0),
- the input pivot `pivot_in = 0.5 + (density - 1) · DENSITY_PIVOT_SHIFT`
  (D-1 §1.1), which cast removal solves against,
- the rule that the endpoint rescale reads its anchors with every shaping
  control at rest (D-1 §1.5), which §1.6 below extends to colour,
- the metering reader over a negative's `normalization` record (D-2
  `auto_tone.py`), which §2 extends,
- the Swift `ToneAdjustment` value type and the widened tone panel (D-6),
  which C-5 copies for colour.

If colour has to land before density for some reason, chunk C-0 grows to
include D-0's refactor and §1's formulas lose the `density` term. Do not
try to run the two plans in parallel — they both rewrite `_curve_raw`.

### 0.2 The input domain is NegPy's; only the output is flipped

This is a *narrower* domain gap than the density plan had, and it matters,
because it means most of the colour math ports verbatim rather than being
re-derived.

Our published TIFF holds `to_log_density(linear) = log10(linear)`,
per-channel affine-stretched by `floors`/`ceils` into a normalized `[0,1]`
(`normalization.normalize_log_image`). That is **exactly** NegPy's
`img[y, x, ch]` — a normalized log exposure, dense end at 0, thin end at 1.
So:

| quantity | NegPy | ours |
|---|---|---|
| curve **input** `u` | normalized log exposure | the same — our `values` before `1 - val` |
| curve **output** | print density `D ∈ [0, 2.3]`, higher is darker | display `v ∈ [0, 1]`, higher is brighter |

Two consequences, and they are the two rules everything in §1 obeys:

- **Anything acting on the input ports unchanged.** Global CMY filtration
  (`val + cmy_offsets[ch]`) and cast removal (a per-channel slope and pivot
  on `u`) are both input-side. Their constants are in the same normalized
  units on both sides. Transfer them.
- **Anything acting on the output needs the flip and a scale.**
  `v_negpy ≈ 1 - v_ours`, so an additive density offset becomes a
  subtraction, a weight `σ(k·(v_negpy - c))` becomes `σ(k·((1-c) - v_ours))`,
  and a magnitude on a 2.3-wide axis becomes a magnitude on a 1-wide one.
  Regional CMY and dye separation are output-side.

### 0.3 Where each control acts

Mirroring `_apply_print_curve_kernel`'s order exactly
(`negpy/features/exposure/logic.py:196-266`), with the density plan's steps
in place:

1. **global CMY** offsets the per-channel input `u`
2. per-channel straight line about the pivot — **cast removal sets the
   per-channel slope and pivot here**, Print Density sets the input pivot
3. midtone gamma (Snap)
4. **regional CMY** — complementary shadow/highlight weights read on the
   *post-Snap* value
5. zone density offsets
6. softplus toe/shoulder knees
7. endpoint rescale (§1.6)
8. **dye separation + damping** — cross-channel, per pixel
9. clip and encode

Steps 1–7 are per-channel *scalar* functions of that channel's own code, so
they compose into **three 1-D LUTs**. Step 8 does not, and that is the one
architectural change this plan makes (§0.4).

Do not reorder. In particular regional CMY must read the post-Snap value,
for the same reason zone density does: the shadows the slider grabs are the
shadows of the print you are looking at.

### 0.4 Dye separation breaks the 1-D LUT, and only dye separation

`separation_damping_gain` (`logic.py:57-76`) reads each pixel's own chroma —
the spread of the three channels at that pixel. No per-channel table can
express it, and neither can a fixed 3×3 matrix. Even undamped, the
separation is a cross-channel mix.

So `previews._encode_display_png` grows a second path:

```
codes (uint16, H×W×C)
  → three float32 tables, 65536 → display value      (steps 1-7)
  → if separation is active: per-pixel spread about the pixel mean  (step 8)
  → clip, ×255, uint8
```

Two properties keep the cost honest:

- **The fast path survives.** With `dye_separation == 1.0` the tables are
  composed straight to uint8 and the render is exactly today's
  `lut[image]` — one table lookup, no float image. That is the common case
  and it must not regress.
- **The float stage only ever sees display-sized pixels.** `generate_preview`
  downscales in code space *before* encoding (`_write_downscaled`), and
  `render_region` crops before encoding. Neither path materialises a
  multi-megapixel float image.

Do not reach for a 3-D LUT. Three 1-D tables plus a five-line per-pixel
kernel is smaller, exact, and needs no interpolation error budget.

### 0.5 A separate `color` op, not more keys on `tone`

Twelve stored colour values on top of the tone op's nine would make one
21-key op whose reset button cannot distinguish "flat tone" from "neutral
colour". A sibling op keeps both states complete, independently resettable
and independently coalesced, and it keeps `validated_tone_params` readable.

Both ops are preview-only states. `net_edit_state` returns them together
(§4), which is the moment its 4-tuple should become a value object.

### 0.6 Our normalization has already defeated the mask, so Cast Removal defaults to 0

NegPy starts a colour negative at `cast_removal_strength = 0.5` because its
input is an un-normalized log density and the orange mask is still in it.

Ours is not. `analyze_bounds` (`normalization.py:330-410`) already solves
per-channel `floors` from a **shared, chroma-gated, same-pixel** set at the
dense end and per-channel `ceils` at the thin end, and
`normalize_log_image` stretches each channel through its own pair. The mask
is gone from the published pixels before the preview ever runs.

What is left is the residual **crossover** — the dense end was tied on
measured neutrals, the thin end on plain per-channel percentiles, so the
scene-shadow end can still carry a cast. That is precisely what NegPy's
one-point shadow tie corrects (§2.2), and it is a trim, not a rescue.

So our neutral default is **0.0**, and the op's absence means no colour
correction at all — the invariant that keeps "neutral is byte-identical"
true. Say this in `DECISIONS.md`; a reader who knows NegPy will otherwise
read the 0 default as a mistake.

### 0.7 Temperature is a lever, not a field

NegPy stores no Kelvin value: the slider projects the region's (M, Y) pair
onto the Planckian direction (`wb_to_kelvin`) and drags along it
(`kelvin_to_wb`), leaving the off-locus green–magenta tint untouched. Cyan
stays at 0, as it does in a real darkroom head.

Copy that exactly. **Do not add a `temperature` field to the op.** The
conversions are two pure functions in `color.py`; the panel owns the
anchor-during-drag behaviour (§7.3).

---

## 1. The curve math

All of §1 lands in `cli/src/scanny_boy/tone.py`, which becomes a
per-channel evaluator. The *derived quantities* it consumes — CMY offsets,
cast slopes — come from `color.py` (§2). Nothing else computes either.

### 1.1 The parameter object

```python
@dataclass(frozen=True)
class ColorParams:
    wb_cyan: float = 0.0
    wb_magenta: float = 0.0
    wb_yellow: float = 0.0
    shadow_cyan: float = 0.0
    shadow_magenta: float = 0.0
    shadow_yellow: float = 0.0
    highlight_cyan: float = 0.0
    highlight_magenta: float = 0.0
    highlight_yellow: float = 0.0
    cast_removal: float = 0.0
    dye_separation: float = 1.0
    separation_damping: float = 0.0

NEUTRAL_COLOR = ColorParams()
```

Field names are exactly the `color` op's param keys, as `ToneParams`'
are. It lives in `color.py`, not `tone.py`, so that `tone.py` keeps
owning only the curve.

### 1.2 Global CMY: an input offset, scaled by the channel's own stretch

NegPy's `filtration_offsets` (`logic.py:1123-1140`):

```
d_ch = slider_ch · cmy_max_density / |ceil_ch - floor_ch|
```

`cmy_max_density = 0.2` is an absolute log10 density (1.0 slider = 20cc).
The division is what makes the same slider print the same filtration on
every frame: each channel is normalized by its own stretch, so a fixed
*normalized* offset would mean a different *density* per channel, and the
slider's colour meaning would drift frame to frame — worst exactly where it
matters, since the mask makes the blue channel's range the odd one out.

Both sides of the fraction are log10 densities in our pipeline too
(`to_log_density` is `log10`, `floors`/`ceils` are its percentiles), so
this transfers with no conversion:

```
CMY_MAX_DENSITY = 0.2            # NegPy's, unchanged
offset_ch = slider_ch * CMY_MAX_DENSITY / max(|ceil_ch - floor_ch|, 1e-6)
u_ch = values_ch + offset_ch     # applied to the input, before `1 - val`
```

`abs()` on the range, as NegPy has it, so the slider direction is uniform.
With no `normalization` record the range is 1.0 (NegPy's `bounds is None`
branch) — the offsets still work, they are merely uncalibrated.

**This makes the display tables negative-dependent.** They already are in
practice (built per call in `_encode_display_png`); §3 makes the dependency
explicit by passing a metering object in.

### 1.3 Regional CMY: complementary weights about the midtone

NegPy (`logic.py:217-220`):

```
w_sh = σ(3.0 · (v - zone_center))          # v is density: high = dark
w_hi = 1 - w_sh
v   += shadow_cmy[ch]·w_sh + highlight_cmy[ch]·w_hi
```

with `zone_center = anchor_target_density = 0.75` (its midtone) and
`shadow_cmy = slider · cmy_max` — **no bounds division here**, because
these are added to `v`, which is already an absolute density.

Note the shape: **complementary** sigmoids summing to 1, unlike zone
density's two independent mid-sparing ones. Every tone gets some of one or
the other; Global is not "Shadows + Highlights". Keep it that way — it is
what makes the three regions read as a split rather than three overlapping
midtone controls.

Ported, flipped and rescaled:

```
w_sh = σ(REGION_SHARPNESS · (REGION_CENTRE - v))
w_hi = 1 - w_sh
v    = v - REGION_CMY_SCALE * (shadow_cmy[ch]·w_sh + highlight_cmy[ch]·w_hi)
```

```
REGION_CENTRE    = 0.5    # our midtone, as 0.75 is NegPy's
REGION_SHARPNESS = 7.0    # NegPy's 3.0 on a 2.3-wide axis: 3.0 × 2.3 = 6.9
REGION_CMY_SCALE = 0.09   # 0.2 density on a 2.3-wide axis ≈ 0.087 of ours
```

`REGION_SHARPNESS` follows the density plan's method for `ZONE_SHARPNESS`
(same *fractional* transition width, not the same number).
`REGION_CMY_SCALE` is a calibration, not a conversion — see §9.

The sign: positive slider = more of that dye = denser = darker in that
channel, so it **subtracts** in display space. Index the tuple by channel —
cyan is the red channel's dye, magenta green's, yellow blue's — matching
NegPy's `(cyan, magenta, yellow)` at `ch = 0, 1, 2`.

### 1.4 Cast removal: a per-channel slope tilt that pins the anchor

We have the metering NegPy's one-point shadow tie needs, and only that
one. `normalization.measure_shadow_refs` records the per-channel
`SHADOW_NEUTRAL_PERCENTILE = 98.0` percentile — NegPy's own
`shadow_neutral_percentile` — on every published negative
(`stitch_pipeline._normalization_record`). NegPy's richer three-band
neutral-axis solve (`per_channel_curve_params`' first branch) reads bands
and a confidence we do not measure. **Port the fallback branch
(`logic.py:1043-1064`), not the neutral-axis one**, and say so in the
comment so nobody "finishes" the port by inventing the missing refs.

Everything below is in **display coordinates** (`x_display = 1 - x_norm`),
so it composes with the density plan's `pivot_in` directly.

```
a    = pivot_in                                    # the anchor: the curve's own input pivot
g    = 1 - shadow_refs_norm[GREEN]                 # green's shadow reference
r_ch = 1 - shadow_refs_norm[ch]
t_ch = g + clamp(strength · (r_ch - g), ±CAST_MAX_OFFSET)

slope_ch = clamp(slope · (a - g) / (a - t_ch), SLOPE_MIN, SLOPE_MAX)
pivot_ch = a - (slope / slope_ch) · (a - pivot_in)
```

```
CAST_MAX_OFFSET = 0.1     # NegPy's cast_removal_max_offset, same normalized units
```

Reading it: `t_ch` is channel `ch`'s shadow reference moved `strength` of
the way from green's toward its own, clamped so a wild reference cannot
run away with the curve. The slope is tilted so that `t_ch` prints exactly
what green prints at `g`, and the pivot is re-solved so the **anchor prints
unchanged** — cast removal balances colour, it never moves exposure. Green
is the reference channel and is untouched (`slope_G = slope`,
`pivot_G = pivot_in`), exactly as in NegPy.

At `strength = 0`, `t_ch = g` and every channel keeps the achromatic slope
— byte-identical to no colour op. At `strength = 1` and `density = 1`
(where `a = pivot_in = 0.5`), `pivot_ch = 0.5` for every channel and the
whole solve is one division.

Guards, all NegPy's: `|a - t_ch| < 1e-6` → `slope_ch = slope`; the slope
clamp is the existing `[SLOPE_MIN, SLOPE_MAX]`; a missing or single-channel
metering record makes the whole thing inert.

**No confidence term.** NegPy multiplies the slider by a neutral-axis
confidence (`effective_cast_strength`); with no neutral axis we take its
`confidence is None` branch, which is the slider unchanged. If we later
record whether `_same_pixel_color_floor_refs` fell back, that flag is the
natural confidence input — note it as future work, do not fake it now.

### 1.5 Dye separation and damping: a spread about the pixel mean

With no paper profile there is no dye-crosstalk matrix, so NegPy's
`compose_density_matrices(dye, sat)` degenerates to `sat` alone, and
`resolve_saturation_matrix` (`papers.py:194-208`) with equal per-channel
k is exactly a spread about the achromatic mean:

```
e'_ch = mean(e) + k · (e_ch - mean(e))
```

`e` is density above paper base there; here it is the display value, and
because `1 - v` is affine the spread is the same operation with the same k.
So the whole of dye separation, for us, is:

```
m       = (v_R + v_G + v_B) / 3
chroma  = sqrt(((v_R-v_G)² + (v_G-v_B)² + (v_R-v_B)²) / 3)
k_eff   = damping_gain(dye_separation, separation_damping, chroma)
v'_ch   = m + k_eff · (v_ch - m)
```

`chroma` is NegPy's `_rms_chroma` measure verbatim (`logic.py:258`) — the
hue-symmetric spread, which `max - min` is not. `damping_gain` is
`separation_damping_gain` verbatim:

```python
def damping_gain(k, damping, chroma):
    if k <= 0.0:
        return 0.0
    h = (SEPARATION_REF_SPREAD - chroma) / (SEPARATION_REF_SPREAD + chroma)
    return np.minimum(k ** ((1.0 - damping) + damping * h), SEPARATION_K_MAX)
```

```
SEPARATION_REF_SPREAD = 0.15   # NegPy's 0.35 density on a 2.3-wide axis
SEPARATION_K_MAX      = 3.0    # NegPy's clamp, unchanged
```

`h` runs from 1 at grey to −1 at extreme separation, so at damping 1 muted
colour takes the full k, the reference spread is left at exactly 1.0, and
vivid colour gets 1/k. That sign reversal between the two populations is
the entire point — it is what a frame-wide matrix cannot do, and it is why
the control is not just "less separation".

**It is inert at `dye_separation == 1.0`** for any chroma, since `1**x = 1`.
That is not a special case to code around; it is why the UI disables the
damping slider there (§7.3) and why the fast path in §3 tests only
`dye_separation`.

**Monotonicity.** NegPy's gain is monotone in chroma for `k < e²`, which
covers the clamped `[0, 3]` domain, so no two pixels can swap which reads
as more saturated. Our k range is `[0.5, 1.5]`, well inside. Assert it in a
test over the range corners anyway, as the density plan does for its zone
inequality.

**Order.** Separation runs *after* the endpoint rescale, on the final
display values, and the clip follows it. That is NegPy's order (its
separation is the last thing before transmittance) and it is what makes the
control ease off where the curve is already compressed: at the toe and
shoulder the three channels have converged, chroma is small, and there is
little spread left to push.

### 1.6 One shared endpoint rescale, read on the achromatic channel

The density plan's §1.5 rule — the rescale anchors are read with grade and
snap only — is necessary but **not sufficient** once the slopes go
per-channel. A per-channel rescale would renormalize each channel to
`[0, 1]` independently, which is a white balance in the opposite direction:
it would undo exactly the colour difference cast removal and CMY just
created.

The rule, stated to cover both plans at once:

> **The rescale anchors are read once, on the achromatic curve — grade and
> snap only, every density, colour and shaping control at rest — and the
> same `(low, high)` pair rescales all three channels.**

```python
neutral = replace(tone_params, density=1.0, shadow_density=0.0, ...)
low  = float(_curve_raw(np.array([0.0]), neutral, NEUTRAL_COLOR, channel=None)[0])
high = float(_curve_raw(np.array([1.0]), neutral, NEUTRAL_COLOR, channel=None)[0])
```

The endpoint pin exists so a soft *grade* still reaches black and white; it
belongs to the grade and to nothing else. The trailing clip then blocks up
where a colour move pushed a channel past the end, which is what a print
does.

This also preserves the invariant everything hangs on: **at neutral colour
the three tables are identical to each other and to the density plan's
single table, and the LUT is bit-identical to today's.**

---

## 2. The derived solves — `cli/src/scanny_boy/color.py`

A new module: pure functions, no I/O, no image. It keeps manifest knowledge
out of `tone.py` and curve knowledge out of `edits.py`, exactly as
`auto_tone.py` does for metering.

```python
@dataclass(frozen=True)
class ColorParams: ...          # §1.1

@dataclass(frozen=True)
class Metering:
    """The colour-relevant slice of a negative's `normalization` record."""
    ranges: tuple[float, ...]        # |ceil - floor| per channel
    shadow_refs_norm: tuple[float, ...] | None

def read_metering(record: dict | None) -> Metering        # never raises
def cmy_offsets(params, metering) -> tuple[float, ...]    # §1.2
def region_cmy(params) -> tuple[tuple[float, ...], ...]   # §1.3, (shadow, highlight)
def cast_slopes(params, metering, slope, pivot_in) -> tuple[tuple[float, float], ...]  # §1.4
def damping_gain(k, damping, chroma)                      # §1.5
def apply_separation(rgb: np.ndarray, params) -> np.ndarray  # §1.5, float32 in/out
def wb_to_kelvin(magenta, yellow) -> float                # §0.7
def kelvin_to_wb(kelvin, magenta, yellow) -> tuple[float, float]
```

`read_metering` returns a `Metering` with `ranges = (1.0,)·C` and
`shadow_refs_norm = None` for a missing, incomplete or degenerate record —
CMY still works uncalibrated, cast removal goes inert. It never raises and
never warns; the warning is `edits.py`'s job (§5.2).

`wb_to_kelvin` / `kelvin_to_wb` are transcribed from
`logic.py:1248-1271` with their constants:

```
TEMP_REF_KELVIN = 5500.0
TEMP_MIN_KELVIN, TEMP_MAX_KELVIN = 3000.0, 12000.0
TEMP_K_MAGENTA, TEMP_K_YELLOW = 0.0029, 0.0057
```

These are a nominal readout — a least-squares projection onto the Planckian
direction in mired, not a colorimetric temperature — and NegPy says so.
Repeat the caveat in our docstring; the number on the slider is a
navigation aid.

**Why they live in the CLI and not only in Swift:** so `edit color
--temperature` can exist, and so there is one implementation to test. The
panel calls the CLI's numbers through the recorded (M, Y), never its own.

---

## 3. Chunk C-0 — `tone.py` goes per-channel

Pure refactor plus the colour terms; no persistence, no CLI.

- `_curve_raw(values, tone_params, color_params, *, channel, metering)`.
  `channel=None` means the achromatic curve — the rescale anchors and the
  mono path both use it, and with neutral colour every channel equals it.
- Steps 1, 2 (cast slope/pivot) and 4 of §0.3 inserted at their places.
- `curve_values` takes the same arguments and keeps §1.6's shared rescale.
- `build_channel_tables(tone_params, color_params, metering, channels) ->
  np.ndarray` of shape `(channels, 65536)`, float64 display values in
  `[0, 1]`, replacing `build_display_lut`. A `build_display_lut(...) ->
  uint8` wrapper stays for the fast path, built by rounding the tables.
- On `channels == 1` every colour term is skipped (§6.3) and the single
  table is the achromatic one.

### Tests (`tone_test.py`)

1. **Neutral is identical.** `build_display_lut` at neutral colour equals
   the density plan's table, bit for bit, and the three rows of
   `build_channel_tables` are equal to each other.
2. **Global CMY offsets the input.** A positive cyan darkens the red
   channel's table everywhere in the midtones; the green and blue tables
   are untouched. The magnitude scales with `1/range`: doubling the red
   channel's recorded range halves the shift.
3. **Regional CMY is regional.** `shadow_cyan = 1` moves the red table at
   `v = 0.2` by much more than at `v = 0.8`; `highlight_cyan` mirrors it;
   the two weights sum to 1 at every tone (assert on the weights directly).
4. **Cast removal ties the shadow reference.** With a hand-built metering
   whose red shadow ref sits off green's, `strength = 1` makes the red
   table at the red ref equal the green table at green's ref, to 1e-9; the
   anchor `pivot_in` prints identically in all three; green is untouched.
5. **Cast removal is bounded.** A reference 10× past `CAST_MAX_OFFSET`
   produces the same tables as one exactly at the limit.
6. **No metering, no cast.** `Metering(ranges=(1,1,1), shadow_refs_norm=None)`
   gives the achromatic tables for any strength.
7. **The rescale is shared.** Two channels with different cast slopes have
   *different* endpoint values — proving §1.6 did not renormalize the colour
   away.
8. **Separation spreads and collapses.** `apply_separation` at k=1.5 grows
   a pixel's chroma, at k=0.5 shrinks it, at k=1.0 is the identity to
   float32 exactness; a grey pixel is unchanged at every k.
9. **Damping reverses.** At `damping = 1`, a muted pixel's `k_eff` ≈ k while
   a vivid pixel's ≈ 1/k, and a pixel at `SEPARATION_REF_SPREAD` gets
   exactly 1.0.
10. **Damping is monotone in chroma** over the k and damping corners, and
    `k_eff` never exceeds `SEPARATION_K_MAX`.
11. **Monotone and in range.** Extend the density plan's whole-box sweep
    with the colour corners; every table monotone non-increasing in the
    code, every value in `[0, 1]`.
12. **Kelvin round-trips.** `wb_to_kelvin(kelvin_to_wb(K, m, y))` ≈ K
    across the range; the tint component (the off-locus residual) is
    preserved; neutral (0, 0) reads 5500 K.

---

## 4. Chunk C-1 — persistence (`library/repo.py`)

**Invariant, stated in the module comment and held everywhere: a `color`
op's params are the complete colour state, never a delta and never a mode.**
Twelve keys always present, or twelve explicit nulls for the reset.

- `COLOR_OP = "color"`, bounds imported from `color.py` — no
  hand-mirrored pairs (the density plan already deletes repo's duplicated
  tone bounds; hold the line).
- `validated_color_params(params: dict | None) -> dict | None` — all twelve
  set or all twelve `None`, per-field range checks, the same `ValueError`
  style.
- `append_color_edit(roll_dir, negative_id, params)` — the same
  coalesce-a-trailing-op-in-place body as `append_tone_edit`. Factor the
  shared coalescing out rather than copying it; two state ops is where that
  pattern earns a helper.
- `net_edit_state` — **its 4-tuple becomes five values, so make it a value
  object now**:

  ```python
  @dataclass(frozen=True)
  class EditState:
      quarter_turns: int
      flipped: bool
      fine_angle_deg: float
      tone: dict | None
      color: dict | None
  ```

  Every call site (`edits.py` ×4, `previews.py` ×4, `cli.py` ×1) unpacks a
  tuple today; converting them is mechanical and is this chunk's bulk.
- Malformed degrades, it does not fail: an out-of-range value on any of the
  twelve → `color = None`, matching the existing tone rule. A `color` op
  from a future build with unknown extra keys → ignore the extras, take the
  twelve.
- Backward compatibility is trivial here — the op simply did not exist, so
  absence is the reset. There is no legacy key shape to migrate, unlike
  the tone op.

### Tests

- Reset writes twelve explicit nulls and reads back as `None`.
- Out-of-range on each of the twelve raises without recording.
- Coalescing collapses repeated commits to one row; a `tone` op between two
  `color` ops blocks the coalesce (they are separate ops, and only a
  *trailing* one coalesces).
- A rotate between them leaves both states intact.
- `EditState` round-trips through every call site (the existing tests
  converted).

---

## 5. Chunk C-2 — the render path (`previews.py`)

### 5.1 The two paths

```python
def _display_tables(tone_params, color_params, metering, channels):
    """Cached per (params, metering, channels); the tables are ~0.5 MB."""
```

- **Fast path** — no colour op, or `dye_separation == 1.0`: round the
  tables to uint8 and index per channel. Three lookups instead of one; on
  a 1024-px preview that is unmeasurable.
- **Float path** — `dye_separation != 1.0`: gather float32 per channel,
  `color.apply_separation`, clip, `rint(·×255)`.

Indexing per channel replaces today's `lut[image]`. Use
`np.take_along_axis` or an explicit three-iteration loop — whichever reads
better; the loop is three lines and obviously correct.

### 5.2 Metering plumbing

`_encode_display_png` gains a `metering` argument.
`generate_preview` / `render_region` / `ensure_preview` / `sync_previews`
already hold the `NegativeRecord`, so they pass
`color.read_metering(negative.normalization)` down. `render_region`'s
caller in `edits.py` does the same.

### 5.3 Cache keying

`ensure_preview`'s incremental path (`PREVIEW_OPS`) must not accept a
`color` op — an 8-bit PNG cannot be re-coloured losslessly, for the same
reason it cannot be re-curved. Add `COLOR_OP` to the always-regenerate
branch beside `TONE_OP`.

### Tests (`previews_test.py`, slow tier)

- Neutral colour produces a byte-identical preview to no colour op.
- A cyan-only change alters the preview's red channel mean and leaves green
  and blue within rounding.
- `dye_separation = 1.5` raises the preview's mean saturation;
  `0.5` lowers it; `1.0` is byte-identical to the fast path.
- A cast-removal change on a negative whose `normalization` is `None`
  leaves the preview unchanged.
- `render_region` honours the recorded colour op (mirror
  `test_render_region_applies_the_recorded_tone`).
- The published TIFF's bytes are unchanged after every one of these.

---

## 6. Chunk C-3 — the monochrome gate

### 6.1 The source of truth, and the ordering problem

The gate belongs to the **roll**, not the negative: a roll is one film stock
(`MONOCHROME_PLAN` §0.3). The right field is the roll's frozen `film_kind`,
which `MONOCHROME_PLAN` §2 introduces and which nothing writes yet — §1's
detector records `normalization.mono` evidence per sampled negative and
acts on nothing, deliberately, because its threshold is not to be pinned
without a measurement you have approved.

So there are two ways to build this chunk, and the choice is yours:

- **(a) Land `MONOCHROME_PLAN` §2 first.** The gate is then real on day
  one, and this chunk is ~20 lines. It costs whatever §1's measurement
  pass costs.
- **(b) Wire the gate now against a `film_kind` that is always
  `"colour"`.** Everything below is built and tested — the CLI validation,
  the `roll info` field, the Swift disable, the achromatic curve path — and
  turning it on when §2 lands is a one-line change in one function. But
  until then a monochrome roll's colour controls are *enabled*, which is
  not what you asked for.

**Recommendation: (a).** The requirement is explicit, and (b) ships a
feature whose stated constraint is not met. If §1's measurement is not
close, take (b) and treat the gap as known, not as done.

Either way the predicate is defined **once**:

```python
def roll_is_monochrome(roll: RollManifest) -> bool:
    """The roll's frozen film kind (MONOCHROME_PLAN §2). Colour has no
    meaning on a single-density roll: there are no layers to balance and
    no dyes to separate."""
```

### 6.2 What the gate does

- **CLI**: `edit color` on a monochrome roll fails with `INVALID_EDIT`
  and a message naming the reason, validated up front for the whole
  selection like every other edit. `--reset` is still allowed — removing an
  op recorded before a re-stitch changed the roll's kind must always work.
- **`roll info`**: report `film_kind` on the roll. Swift needs it, and it
  is the roll's own state, not derived per negative.
- **Swift**: the Colour toolbar button is disabled with a `.help` saying
  why (§7.4). The error is then unreachable through the UI, which is the
  point — the CLI check is the backstop, not the mechanism.

### 6.3 What the curve does

Independent of the roll's kind, `build_channel_tables` on a **single-channel**
image skips every colour term and returns the achromatic table. That is not
belt-and-braces, it is correctness: once `MONOCHROME_PLAN` §3's collapse
lands, mono rolls publish one-channel TIFFs, and `cmy_offsets` indexed by
channel or a three-channel `apply_separation` would crash on them. Write
this path now and test it with a synthetic one-channel array; it costs two
`if`s.

---

## 7. Chunk C-4 — CLI, protocol, contract

### 7.1 `edit color`

A new subcommand beside `edit tone`, same selection semantics.

```
--roll DIR                    --negative ID (repeatable)
--cyan V --magenta V --yellow V                 global,      -1..1
--shadow-cyan V --shadow-magenta V --shadow-yellow V         -1..1
--highlight-cyan V --highlight-magenta V --highlight-yellow V -1..1
--temperature K               3000-12000, a lever over the region's M/Y
--region {global,shadows,highlights}            which region --temperature drives
--cast-removal V              0..1  (0 neutral)
--dye-separation V            0.5..1.5 (1.0 neutral)
--separation-damping V        0..1  (0 neutral)
--reset                       remove the adjustment
```

- `--temperature` is **resolved to (M, Y) before validation**, against the
  named region's current recorded pair, via `color.kelvin_to_wb`. It is
  mutually exclusive with that region's `--magenta` / `--yellow`. `--region`
  defaults to `global` and only means anything with `--temperature`.
- Unspecified values take the negative's **currently recorded** value, not
  the neutral default. This differs from `edit tone`, whose flags are
  all-or-nothing, and it is deliberate: twelve required flags for a
  one-slider change is unusable, and the panel's debounced commits would
  have to resend all twelve every time. State the difference in
  `CONTRACT.md` — it is the sort of asymmetry that reads as a bug later.
- `--reset` wins over and ignores every value flag, as `edit tone`'s does.
- Validation runs on the **merged** twelve-key state, so a partial update
  can never record an out-of-range op.

### 7.2 `edits.run_edit_color`

Same shape as `run_edit_tone`: validate the whole selection first, then per
negative merge → validate → `append_color_edit` → `_refresh_preview` →
collect the `EditRecorded` fields. Emit `TONE_METERING_UNAVAILABLE` once per
negative whose `normalization` record cannot serve a non-zero
`cast_removal`, and record anyway.

> The density plan introduces that code. If both plans are being executed,
> rename it to `METERING_UNAVAILABLE` in the density plan **before it
> ships** — it is not tone-specific, and renaming a shipped contract code is
> worse than renaming a planned one.

### 7.3 `roll info`

Twelve derived `color_*` fields per negative, `null` when there is no op,
plus `film_kind` on the roll (§6.2). Add a derived `color_temperature`
too: `null` when there is no op, otherwise `wb_to_kelvin(wb_magenta,
wb_yellow)` — Swift should not carry a second implementation of the
projection, and a readout is exactly the kind of thing the CLI owns.

### 7.4 Protocol 11 → 12 (`events.py`)

Bump `PROTOCOL_VERSION` with a comment block in the existing style:
protocol 12 keeps 11's roll model and adds the `edit color` subcommand,
the thirteen `color_*` fields and `film_kind` on `roll info`, and the
`color` op in the ops log. `EditRecorded` does not change — the colour
state rides the recorded op's `params`, as the tone state does.

### 7.5 Contract and docs

- **`shared/contract/CONTRACT.md`** — a "Protocol version 12" paragraph;
  the usage line; an `edit color` section covering the thirteen flags,
  their ranges and sign conventions, the partial-update rule (§7.1), the
  temperature lever, the monochrome refusal, and the fact that the op is
  preview-only.
- **`shared/contract/schema.json`** — `"edit color"` into the `command`
  enum. Verify whether anything else constrains op names; do not assume.
- **`shared/contract/roll-manifest.schema.json`** — the thirteen
  `color_*` fields beside the tone ones, `["number", "null"]`, each marked
  "Derived, not stored"; `film_kind` on the roll.
- **`docs/DECISIONS.md`** — a "The preview's colour adjustment" section
  covering the six things not obvious from the code:
  1. the input domain is NegPy's and the output is flipped, so input-side
     controls port verbatim and output-side ones need a scale (§0.2);
  2. why colour is a second op rather than more keys on `tone` (§0.5);
  3. why Cast Removal defaults to 0 here and 0.5 in NegPy — our
     normalization already defeated the mask (§0.6);
  4. why the endpoint rescale is shared across channels, without which
     cast removal and CMY are self-cancelling (§1.6);
  5. why dye separation is the one control that is not a LUT, and why the
     fast path still exists (§0.4);
  6. that we port the shadow-tie branch of cast removal and not the
     neutral-axis branch, because we do not measure the refs it needs
     (§1.4).

### Tests (`cli_test.py`, `edits_test.py`, `events_test.py`)

- All thirteen flags round-trip through `roll info`.
- A single `--cyan` on a negative with a recorded op leaves the other
  eleven values alone (§7.1's partial-update rule).
- `--temperature 3200 --region shadows` moves `shadow_magenta` and
  `shadow_yellow` and nothing else; the resulting `color_temperature`
  reads back within a step of 3200.
- `--temperature` with `--magenta` for the same region is a usage error.
- `--reset` removes the op and restores the neutral preview bytes.
- A monochrome roll refuses `edit color` but accepts `--reset`.
- `edit color` on a negative with `normalization = None` and a non-zero
  `--cast-removal` warns and still records.
- The published TIFF is untouched; `export` output is unchanged with a
  colour op recorded.
- Emitted lines carry `protocol_version: 12`.

---

## 8. Chunk C-5 — the Mac app

### 8.1 The value type first

Mirror D-6's `ToneAdjustment` refactor before any UI:

```swift
struct ColorAdjustment: Equatable, Sendable {
    var wbCyan, wbMagenta, wbYellow: Double
    var shadowCyan, shadowMagenta, shadowYellow: Double
    var highlightCyan, highlightMagenta, highlightYellow: Double
    var castRemoval: Double
    var dyeSeparation: Double
    var separationDamping: Double
    static let neutral = ColorAdjustment(...)   // 0…0, 0, 1.0, 0
}
```

- `EditModel` gains `scheduleColor` / `commitColor` / `setColor` and an
  `isSettingColor` flag, copying the tone debounce and
  supersede-in-flight-session machinery verbatim.
- **`renderGeneration` must include the colour state.** Miss it and a
  cyan-only change leaves stale 1:1 region crops on screen. Hash
  `ColorAdjustment` alongside `ToneAdjustment`.
- `RollManifest.Negative` gains the thirteen `color_*` fields;
  `CLIEvent.recordedColor` decodes the op's params, gating on the presence
  of a key as `recordedTone` does; absent keys take neutral defaults so an
  older CLI degrades the same way on both sides.

### 8.2 The panel

A second toolbar button beside Tone, opening a Colour popover at ~360 pt:

| section | controls |
|---|---|
| **Color** | region selector (Global / Shadows / Highlights), Temperature, Cyan, Magenta, Yellow, region Reset |
| **Correction** | Cast Removal |
| **Saturation** | Dye Separation, Separation Damping |

| control | range | step | format | notes |
|---|---|---|---|---|
| Temperature | 3000…12000 K | 50 | `%.0fK` | **mired-linear travel**: warm (low K) on the left, cool (high K) on the right; yellow→blue track gradient |
| Cyan / Magenta / Yellow | −1…1 | 0.02 | `%+.2f` | |
| Cast Removal | 0…1 | 0.05 | `%.2f` | |
| Dye Separation | 0.5…1.5 | 0.02 | `%.2f` | |
| Separation Damping | 0…1 | 0.05 | `%.2f` | disabled at Dye Separation 1.0 |

Behaviour, all of it NegPy's:

- **The region selector is a view, not a state.** It picks which three of
  the nine stored values the C/M/Y sliders and Temperature drive. It is
  panel-local — do not record it, do not send it except as `--region`
  alongside `--temperature`. Show an edited dot on a region button when any
  of its three values is non-zero (NegPy's `_region_buttons`).
- **Temperature anchors its (M, Y) for the whole drag**
  (`ColorSidebar._on_temp_drag_started`). Re-projecting an already-clipped
  pair on every tick corrupts the tint component. This is the one
  non-obvious thing in the panel; copy the anchor, and say why in a comment.
- **Cyan is untouched by Temperature**, as in a real darkroom head.
- **Region Reset** zeroes the selected region's three values only.
  **Panel Reset** removes the op — the same distinction the tone panel
  already draws.
- **Separation Damping is disabled at Dye Separation 1.0**, with a `.help`
  saying it redistributes that slider's push and has none of its own.
  Disabled, not hidden: a hidden control reads as missing.
- **The whole button is disabled on a monochrome roll**, `.help` explaining
  that a single-density roll has no layers to balance.

Mirror D-6's judgement call: if three sections overflow the popover on a
small display, promote to a sheet rather than shrinking the type, and say
which you picked in the PR.

### Tests (`EditModelTests`, `CLIEventTests`, `CLICommandTests`)

- `setColor` records and reflects all twelve.
- A partial commit (one slider) leaves the other eleven at their recorded
  values.
- `renderGeneration` differs for two negatives differing only in
  `dyeSeparation`.
- `CLICommand.editColor` emits `--temperature` with `--region` and never
  alongside that region's `--magenta`.
- A `roll info` payload without the colour keys yields `nil`, not neutral —
  absence means no op here, unlike the tone op's legacy-key case.
- The tone panel's existing tests still pass, and a colour edit leaves the
  tone state alone.

---

## 9. Order, verification, and the open calibrations

```
C-0 → C-1 → C-2 → C-3 → C-4 → C-5
```

C-0 is pure and testable alone; C-1 and C-2 are green in the CLI's own
suite; only C-4 changes the wire; only C-5 needs Xcode. C-3 can move
earlier if `MONOCHROME_PLAN` §2 lands first (§6.1 option (a)) — it is
listed here because it is the chunk most likely to wait.

```bash
cd cli && uv run pytest                       # fast tier, per chunk
cd cli && uv run pytest --slow                # once, after C-2 and again after C-4
cd mac && xcodegen generate && xcodebuild test -scheme ScannyBoy -destination 'platform=macOS'
```

Per `AGENTS.md` the slow tier is what exercises the real TIFF and preview
paths, and C-2 is entirely preview path — run it there, not only at the end.

### The open calibrations

Three numbers are chosen by eye rather than derived. Everything else is
either NegPy's own constant transferred between comparable domains, or falls
out of an inversion.

| constant | value | basis |
|---|---|---|
| `CMY_MAX_DENSITY` | 0.2 | NegPy's, between identical log10-density domains — firm |
| `CAST_MAX_OFFSET` | 0.1 | NegPy's, identical normalized domain — firm |
| `SEPARATION_K_MAX` | 3.0 | NegPy's clamp, dimensionless — firm |
| `REGION_SHARPNESS` | 7.0 | NegPy's 3.0 at the same fractional width — firm |
| `REGION_CMY_SCALE` | 0.09 | 0.2 density on a 2.3-wide axis, mapped to ours — **check** |
| `SEPARATION_REF_SPREAD` | 0.15 | NegPy's 0.35 density spread, ditto — **check** |
| `CAST_REMOVAL` default | 0.0 | our normalization already defeated the mask (§0.6) — **check on real rolls** |

**After C-2 and before C-5**, render one real colour frame at each extreme —
`--cyan ±1`, `--shadow-magenta ±1`, `--highlight-yellow ±1`,
`--cast-removal 1`, `--dye-separation 0.5 / 1.5`, and
`--dye-separation 1.4 --separation-damping 0 / 1` — look at the previews,
and confirm the travel feels like NegPy's before the sliders ship. In
particular:

- **`REGION_CMY_SCALE`**: full travel on a region should be a visible but
  correctable cast, not a colour wash. The density plan's
  `ZONE_DENSITY_SCALE` was pinned the same way and landed well below the
  naive `1/2.3` conversion; expect the same here.
- **`SEPARATION_REF_SPREAD`**: at `damping = 1`, the pixels that come out
  *unchanged* should be the ones you would call "normally saturated" in that
  frame. If everything reverses, the reference is too low; if nothing does,
  too high.
- **Cast Removal's default**: run `--cast-removal 1` on a few rolls. If it
  is routinely an improvement rather than a trim, our normalization is
  leaving more crossover than §0.6 assumes, and the default and the
  reasoning both want revisiting.

Only the constants move; the shapes are settled. Any retune must keep the
neutral-is-byte-identical property, which the C-0 tests assert.
