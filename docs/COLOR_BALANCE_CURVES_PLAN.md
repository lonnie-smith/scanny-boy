# Colour balance: warmth/tint plus channel curves

The Color panel balances a negative with temperature plus three regions
(global, shadows, highlights) of cyan/magenta/yellow sliders. Two problems
make that hard to use:

- **Midtones have no control of their own.** The shadow and highlight
  zone weights are about 0.095 at a display value of 0.5
  (`tone._zone_weights`, sharpness 9). Global is the only full-strength
  control there, and it moves the ends too.
- **Three sliders, two degrees of freedom.** Each region's triple is
  luma-mean-removed (`color._luma_removed`), so one direction of slider
  travel does nothing, and each slider leaks into the other channels. Magenta
  keeps only about 29% of its move on its own channel.

This plan replaces those controls with:

1. **Balance:** two global, lightness-neutral sliders, warmth
   (blue↔yellow) and tint (green↔magenta). They are exactly perpendicular to
   each other and to brightness. They get the image into the ballpark.
2. **Channel curves:** one curve each for red, green and blue, with three
   vertical-only control points at display values 0.25, 0.5 and 0.75. The
   ends stay pinned at 0 and 1.

It follows `docs/TETHER_PLAN.md`'s conventions: numbered chunks, each
independently green.

---

## 0. Decisions already made

| Question | Decision |
|---|---|
| Scope | **Balance only.** Remove `temperature` and global, shadow and highlight CMY. Keep `cast_removal`, `cast_removal_highlights`, `auto_neutral`, `dye_separation` and `separation_damping` unchanged. The Auto button retargets to warmth and tint. |
| Curve points | **0.25, 0.5 and 0.75, y-only**, with 0 and 1 pinned. Black and white stay neutral unless cast removal moves them. |
| Existing colour ops | **None matter.** An op without the new keys parses as no colour op. No migration and no legacy render path. |

### Before chunk 1

`feat/colors` has uncommitted work that makes temperature its own layer
(`color.temperature_cmy`, DECISIONS §colour item 8). This plan deletes that
code. Commit it as a checkpoint first so there is a record of it, or discard
it. That call is yours.

---

## 1. The balance math

### 1.1 Where it applies

It applies at the same place global CMY applies today: a per-channel offset
on normalized log density, **before** the `1 − val` flip and **before** the
camera matrix. The call sites are `tone.build_channel_tables` and
`render._linear_lut_from_codes`.

Reasons to keep it there:

- `auto_color`'s residuals, `cast_slopes`, `auto_neutral` and the roll
  highlight lock (`remap_dense_end`) all work in this space. The Auto solve
  stays closed-form (§3).
- An offset on density before the flip is a uniform shift of the display
  value going into the curve, which is what "filtration" means here.

**What "exactly perpendicular" means.** The guarantee is exact in the space
the offset is added to, under the Rec.709 luma model the current sliders
already use. The camera matrix mixes channels afterwards, so in output
Adobe RGB it holds to first order. That is the same guarantee global CMY
has today. Chunk 9 measures the drift on real frames (§9).

### 1.2 Remove the range division

Today the slider value is divided by `metering.ranges[ch]` before luma
removal (`cmy_offsets`), so the direction a slider moves in depends on each
negative's metering. Perpendicular axes need a fixed space, so **warmth and
tint map directly to offsets, with no range division.**

A side effect is that equal warmth gives an equal display shift on every
frame. That's better for copying a balance across a roll.

### 1.3 The axes

Use the luma-weighted inner product ⟨u, v⟩ = Σ wᵢ uᵢ vᵢ, with
w = `LUMA_WEIGHTS` = (0.2126, 0.7152, 0.0722). Under it, grey (1, 1, 1)
satisfies ⟨grey, v⟩ = luma(v). "Perpendicular to grey" and "lightness-neutral"
are then the same condition, and one inner product defines all three
right angles.

Solving for two unit vectors that are perpendicular to grey and to each other,
with tint taken as green's luma-removed direction, gives (display-space
direction, positive = brighter in that channel):

| Axis | R | G | B | Reads as |
|---|---|---|---|---|
| `WARM_AXIS` | +1.0920 | 0 | −3.2155 | red up, blue down (+ = yellow, − = blue) |
| `MAGENTA_AXIS` | +1.5847 | −0.6310 | +1.5847 | green down, red and blue up (+ = magenta, − = green) |

Warmth leaves green alone, and tint moves red and blue equally. Both have a
luma of exactly zero, and their inner product is 0 (checked numerically).

```python
def balance_offsets(params: ColorParams) -> tuple[float, float, float]:
    display = BALANCE_SCALE * (params.warmth * WARM_AXIS + params.tint * MAGENTA_AXIS)
    return tuple(-display)  # density offset: + density = darker display
```

`BALANCE_SCALE` gets a calibration comment in `color.py`. Its starting value
makes warmth ±1 match the display shift of today's 3500 K / 12000 K extremes
on a reference frame. Chunk 9 tunes it.

Store the axes as literal constants and derive them in a test, not at import.
The test asserts luma = 0, unit norm and orthogonality.

---

## 2. The channel curves

### 2.1 Where it applies

It applies in `tone.curve_values`, **after** the shared endpoint rescale, as
the last per-channel step. It replaces the regional CMY block in
`_curve_raw`, which is deleted.

After the rescale, 0 and 1 are the actual output black and white, so
"pinned ends" means what it looks like in the editor, and 0.25 means a
quarter tone the user can see. The curve comes before `apply_separation`,
which still runs on the table output. Both render paths reach it through
`curve_values`, so preview and export can't drift.

The curves are **not** lightness-neutral, by design: a curve you draw is
the curve that's applied. Lightness stays with the tone panel.

### 2.2 Shape

For channel c, the knots are:

```
x = (0, 0.25,        0.5,         0.75,         1)
y = (0, 0.25 + o₂₅,  0.5 + o₅₀,   0.75 + o₇₅,   1)
```

- Interpolate with **monotone cubic (Fritsch–Carlson / PCHIP)**. It has no
  overshoot and keeps monotone data monotone.
- The curve is identity above 1, so display headroom up to `DISPLAY_CEILING`
  passes through.
- Each offset is bounded to ±0.2 (tune in chunk 9).
- **Ordering rule:** each knot's y must stay at least `CURVE_MIN_GAP`
  (0.02) above the previous one, so a curve can never reverse. This check
  lives in `repo.validated_color_params` and fails with `INVALID_EDIT`. The
  editor clamps drags so it can't send an invalid curve.
- Implement it in numpy as a small pure function
  (`color.channel_curve(v, offsets)`). Use scipy's `PchipInterpolator`
  instead only if scipy is already a dependency.

---

## 3. Auto balance

`auto_color.solve_cmy` becomes `solve_balance`. Its first three steps don't
change: the target offsets from `_neutral_defaults_target`, the cast-slope
compensation, and luma removal. That leaves a luma-zero offset vector
**o**, which lies exactly in the plane the two axes span:

```
d = −o / BALANCE_SCALE              # density offset → display direction
warmth = clamp(⟨d, WARM_AXIS⟩, −1, 1)
tint   = clamp(⟨d, MAGENTA_AXIS⟩, −1, 1)
```

The final `× ranges / (CMY_MAX_DENSITY × gain)` step goes away along with
the range division (§1.2). The Auto button overwrites warmth and tint and
leaves the curves untouched.

The flag is renamed `--auto-cast` → `--auto-balance` (Swift:
`ColorAutoFlags.cast` → `.balance`). It is mutually exclusive with
`--reset`, `--warmth` and `--tint`.

---

## 4. Op shape and contract

### 4.1 Keys

`color.COLOR_PARAM_KEYS` becomes:

```
warmth, tint,
curve_red_25, curve_red_50, curve_red_75,
curve_green_25, curve_green_50, curve_green_75,
curve_blue_25, curve_blue_50, curve_blue_75,
cast_removal, cast_removal_highlights,
dye_separation, separation_damping, auto_neutral
```

All of these are flat floats, which fits the existing per-key validation,
merge (unspecified flags take the recorded value) and `color_*` manifest
fields. Adding more curve points later means new keys and a protocol bump.
That's acceptable.

Remove: `wb_*`, `shadow_*`, `highlight_*`, `temperature`,
`COLOR_PARAM_KEYS_V1`, and the "older op" fill-in logic in
`repo._parse_color_op` and `validated_color_params`. The new key set is
the floor. An op missing any of those keys parses as `None`.

### 4.2 CLI

```
scanny-boy edit color --roll DIR --negative ID [ID ...]
    [--warmth V] [--tint V]
    [--red-25 V] [--red-50 V] [--red-75 V]
    [--green-25 V] [--green-50 V] [--green-75 V]
    [--blue-25 V] [--blue-50 V] [--blue-75 V]
    [--auto-balance]
    [--cast-removal V] [--cast-removal-highlights V]
    [--dye-separation V] [--separation-damping V] | --reset
```

### 4.3 Protocol and manifest

- `events.PROTOCOL_VERSION` goes from 22 to 23.
- In `roll-manifest.schema.json`, replace the `color_wb_*`,
  `color_shadow_*`, `color_highlight_*` and `color_temperature` fields with
  `color_warmth`, `color_tint` and `color_curve_{red,green,blue}_{25,50,75}`.
  `roll info` already loops over `COLOR_PARAM_KEYS`.
- In `CONTRACT.md`, rewrite the `edit color` synopsis and prose section, and
  the `roll info` field list.
- Previews: negatives with an old colour op now render with neutral colour.
  Their cached previews are stale, so run `previews.sync_previews(force=True)`
  once on the new protocol.

---

## 5. Mac

### 5.1 Model and bridge

- `ColorAdjustment`: replace the fields to match §4.1 and delete
  `ColorTemperature`. Group the curves as
  `ChannelCurve { q1, mid, q3 }` × 3, flattened only at the CLI boundary.
- `RollManifest.Negative`: new `color*` fields, with `colorAdjustment`
  gated on `colorWarmth != nil`.
- `CLICommand.editColor` and `CLIEvent`: new flags and recorded keys.
- `ColorAutoFlags.cast` becomes `.balance`.

### 5.2 Panel (`ColorAdjustmentPanel`)

```
Color
  Warmth   [blue ─────●───── yellow]   +0.12
  Tint     [green ────●──── magenta]   −0.04
  [Auto]

Curves                          (R) (G) (B)  All
  ┌─────────────────────────────┐
  │                          ╱  │   drag a point vertically;
  │                 ●     ╱     │   double-click resets the point;
  │           ●  ╱              │   ⌥-click resets the channel
  │     ●   ╱                   │
  │  ╱                          │
  └─────────────────────────────┘
  [Reset Curves]

Correction   (unchanged: cast removal ×2)
Saturation   (unchanged: dye separation, damping)
[Reset All]
```

- Remove the region picker, the CMY sliders, temperature and Region Reset.
- `ChannelCurvesEditor` is a new SwiftUI `Canvas` view. The selected
  channel's points are editable. "All" shows all three curves in their
  colours, with none editable. Drags clamp to ±0.2 and to the ordering rule.
- The drawn curve needs the same PCHIP in Swift (display only, since the CLI
  renders pixels). Add a parity test against Python output at fixed
  knots. The fixture is generated by a Python test and checked in.
- Commits reuse `scheduleColor`'s debounce, like the sliders.

---

## 6. Chunks

Each chunk leaves `pytest` and the Xcode tests green.

1. **Balance math, unwired.** In `color.py`: `WARM_AXIS`, `MAGENTA_AXIS`,
   `BALANCE_SCALE` and `balance_offsets`. Tests: luma zero, unit norm,
   orthogonal, and signs (+warmth raises display R and lowers B; +tint lowers
   G).
2. **Channel curve, unwired.** `color.channel_curve`. Tests: identity at
   zero offsets, exact at the knots, ends pinned, monotone across a sweep,
   identity above 1, and ordering-rule rejection.
3. **Swap the params and the render.** New `ColorParams`, keys, bounds, and
   repo validate/parse. Wire `balance_offsets` in place of `cmy_offsets`
   and `channel_curve` into `curve_values`. Delete `cmy_offsets`,
   `region_cmy`, `temperature_cmy` and the regional block in `_curve_raw`.
   Update the `render`, `previews`, `exporter`, `tone` and `repo` tests,
   including preview/export parity with non-zero balance and curves.
4. **Auto balance.** Add `solve_balance` and update the `edits.run_edit_color`
   Auto branch. Tests: a synthetic residual solves to a pair that nulls it,
   and the curves survive Auto.
5. **CLI and contract.** Flags, `_color_flag_updates`, validation,
   `--auto-balance`, the protocol bump, the schema, `CONTRACT.md`, the
   `cli_test` updates, and the forced preview sync.
6. **Mac model and bridge.** `ColorAdjustment`, `RollManifest`,
   `CLICommand`, `CLIEvent`, plus `CLICommandTests` and `EditModelTests`.
7. **Mac panel.** Balance sliders with gradient tracks, Auto,
   `ChannelCurvesEditor` and the PCHIP parity test. Remove the old controls.
8. **Docs.** A new DECISIONS section, "Balance and channel curves (protocol
   version 23)", superseding the colour op's items 1 and 8. Update the
   `color.py` row in ARCHITECTURE and the `cmy_offsets`/`ranges` mentions in
   ROLL_HIGHLIGHT_LOCK §4.
9. **Tuning on real frames.** Set `BALANCE_SCALE`, the ±0.2 curve bound and
   `CURVE_MIN_GAP`. Measure the Adobe RGB luma drift of a warmth/tint sweep
   after the camera matrix on a few rolls, and record the numbers in
   DECISIONS.

---

## 7. Open questions (not blocking)

- **Balance after the matrix.** If chunk 9's luma drift is visible, move
  balance to the display value just before the curve, using Adobe RGB luma
  weights (0.2974, 0.6273, 0.0753). That makes it exact in the output
  space, but Auto balance would need the residual carried through the camera
  matrix.
- **Histogram behind the curve.** A per-channel histogram of the current
  preview would make point placement much easier. It needs a small
  `render-preview` side output.
- **More points.** If three aren't enough, 5 fixed points (0.125-step) is
  the next step. It adds new keys under the same shape.
