# Scratch removal plan

Automatic detection and healing of long, thin, film-length scratches on
**colour negative** rolls. Detection runs inside the stitch stage on every
published negative; the result is recorded as an ordinary edit op, applied
wherever the ops log is replayed (previews, 1:1 region renders, export), and
switchable per negative from the Heal panel.

Nothing here depends on the rest of the roll: every decision is made from
the one stitched negative in hand.

---

## 1. What we are removing

Measured on the `Recalibrate-Geometry` roll (10 negatives, 4×2 grids,
~10 200 × 12 000 px published canvases). The same three scratches appear in
all ten negatives at x ≈ 4500, 5240 and 6760, which is how they were first
mistaken for stitch seams.

**Geometry.** Continuous across the entire frame along the film's long
axis. Not straight: the centre wanders ±10–20 px over ~12 000 rows,
smoothly. On this rig the long axis is the TIFF's vertical; that is a
property of the layout, not something to hard-code.

**Cross-section** (averaged along the tracked centre, per channel, in
log10 transmittance):

| offset from centre | effect |
| --- | --- |
| core, \|u\| ≤ ~2 px | blue transmission 0.31–0.50 of the background, green 0.73–0.81, red 0.89–0.97 |
| shoulders, 5 ≤ \|u\| ≤ ~20 px | every channel slightly *brighter*, +0.005 to +0.02 log10 |

In the published negative the core reads orange; in the positive it reads
blue. The shoulders being brighter means light is redirected rather than
absorbed. The most likely cause is a very shallow groove (the scratches
are invisible to the eye on the physical film), but that part is a
hypothesis. What matters for the design is that **the image under the
scratch is still present, just attenuated**, so the heal is a *correction*,
not an inpaint.

**Depth depends on local density.** The blue core offset, binned by the
background's own level (normalized units, dense → thin):

```
5287: -0.020 -0.028 -0.024 -0.120 -0.086 -0.082 -0.098 -0.100 -0.105 -0.201 -0.310 -0.332
```

Near-zero in the densest quarter, roughly 10× deeper in the thinnest. A
constant log offset under-corrects thin areas and over-corrects dense ones.
A linear-light affine model (`I_obs = t·I + g`) was near-perfect in thin
areas but blew up in dense ones, so it was dropped. See §3.2 for the model
that won.

**Prototype results** on `_DSC5207`, fit on even 64-row bands and scored on
odd ones. The metric is the mean residual blue-vs-red/green chroma in the
core, ×10⁻³ normalized units:

| scratch | none | constant offset | level-binned (chosen) |
| --- | --- | --- | --- |
| 5287 | −100 | −22 | −10 |
| 4556 | −70 | −11 | −6 |
| 6807 | −80 | −14 | −4 |

Visually the line is gone in sky, foliage and gravel. A faint trace
survives in dark, grainy regions (see §7, "core noise").

---

## 2. Scope

**In:**

- Colour rolls only (`FilmKind.COLOUR`). Monochrome rolls never get an op.
- Long, near-continuous scratches running along either canvas axis.
- Detection and fitting at stitch time, on every publish, including
  re-stitches that adopt a negative in place.
- One per-negative on/off switch, **on by default** when anything is
  found.
- Replay in previews, `render-region`, and export.
- A CLI command to toggle, and one to (re)detect on an existing published
  TIFF, for rolls stitched before this feature.

**Out (v1):**

- Per-scratch accept/reject. The whole-negative toggle is the escape hatch.
- Short or partial scratches, diagonal scratches, dust, hairs. Those belong
  to whatever replaces the spot tool.
- Any roll-level prior.
- Tier-2 core inpainting (§7).

**Relationship to `spots.py`.** Independent. The new module imports nothing
from `spots.py`, the op is a new op kind, and the UI is its own section of
the Heal panel. Scrapping the spot tool later should not touch any of this.

---

## 3. Algorithm

All arithmetic runs on the published representation: uint16 codes →
`decode_normalized` → `val`. Per-channel spans `ceil − floor` come from the
negative's normalization record. Multiplying `val` by span gives log10
units, which is what makes the chroma signal physically comparable across
channels. The correction itself is applied in `val`, where an additive
offset *is* an additive log-density offset.

New module: `cli/src/scanny_boy/scratches.py`. It is a leaf importing only
`numpy`, `scipy.ndimage`, `cv2` if needed, and `normalization`.

### 3.1 Detect

Run once per axis: vertical as-is, horizontal on the transposed view.
Everything below is written for the vertical case.

1. **Signal.** `s = span_B·val_B − (span_R·val_R + span_G·val_G)/2`.
2. **Band response.** Split rows into 64-row bands, sampling every second
   row. For each band, average `s` down the band to one row, then correlate
   across x with a matched kernel: `+mean` over the core `|u| ≤ 2`,
   `−mean` over the background `6 ≤ |u| ≤ 20`. This gives
   `R[band, x]`.
3. **Normalize.** Per band, take the robust z-score
   (`(R − median) / (1.4826·MAD)`) and clip it to ±8, so a single dark pole
   cannot carry a path.
4. **Track.** Run a dynamic-programming min-cost path top to bottom: at
   each band the path may move −1/0/+1 column, and a move costs 1.5 (in z
   units). The per-column end score is the path cost divided by the band
   count. Backtrack from local minima of the end score, suppressing
   ±40 px around each taken path.
5. **Gates.** The starting values below come from the prototype on 4
   negatives and must be re-measured with the calibration tool (§6.3):

   | gate | start | observed true | observed false |
   | --- | --- | --- | --- |
   | mean path z | ≤ −3.0 | −4.6 … −7.9 | −4.3 … −4.8 |
   | bands with z < −1 | ≥ 0.85 | 0.89 … 1.00 | 0.74 … 0.77 |
   | drift (max − min x) / length | ≤ 0.004 | 14–30 px / 12 k | 50–100 px / 12 k |
   | chroma sign | blue loss > green loss ≥ red loss in core | ✓ | — |
   | not a step edge | see below | — | frame borders |

   **Frame-border rejection.** The other strong, full-length, consistent
   hits in every negative sat at x≈200–390 and ≈9500–9830. They are the
   film frame's own edges, and they lie *inside* the recorded
   `analysis_rect`, so that rect alone does not exclude them. Gate on
   shape: a scratch sits on a background that is continuous across it; a
   frame edge is a step. Reject when the per-band `|mean(left bg) −
   mean(right bg)|` exceeds k× the core depth in more than half the bands
   (k to be measured). Additionally exclude candidates within 64 px of the
   film-extent boundary when `film_extent.detected`.

6. **Centre refinement.** Per band, find the minimum of the chroma profile
   within ±12 px of the path and refine it to subpixel with a three-point
   parabola (offset clamped to ±1). Then take a 5-band median and a
   Gaussian with σ = 2 bands. The centre at a given row is linear
   interpolation between band centres.

Prototype cost: ~2 s per negative for both steps 2–4 on a full canvas, in
unoptimized numpy.

### 3.2 Fit

For each accepted scratch:

1. **Strip.** Sample `val` at `c(y) + u`, `u ∈ [−32, 32]`, with linear
   interpolation across x only.
2. **Background line.** Per row, the mean of `u ≤ −24` and the mean of
   `u ≥ 24`, joined linearly across u. `dev = strip − bgline`.
3. **Level.** Per row, per channel: the background line's value at
   `u = 0`, **smoothed along the scratch with a Gaussian, σ = 12 rows**.
   Unsmoothed levels are grain-noisy and turn the lookup into visible
   horizontal banding; the prototype's v2 had exactly this defect.
4. **Table.** Split each channel's level into 12 quantile bins. Per bin,
   the per-u median of `dev` gives `table[bin, u, ch]`, and the bin's
   median level gives `centres[bin, ch]`. Zero the table for
   `|u| ≥ 24`, then taper linearly to zero across `20 ≤ |u| < 24`.
   - If a bin has fewer than N rows (scratch mostly crossing one flat
     region), merge adjacent bins. With a single bin left, the model
     degenerates to the constant offset, which is still better than
     nothing.

### 3.3 Apply

For every row `y` and every pixel column `x` with `|x − c(y)| < 24`:

- `u = x − c(y)` (fractional)
- `level = gaussian_σ12(bgline_at_u0)(y)`, recomputed from the image being
  healed
- `correction = interp_level(interp_u(table))`
- `val[y, x, ch] −= correction`

No image resampling: only the correction is interpolated. Pixels outside
±24 px of any centre are untouched. Where two scratches' windows overlap,
apply them sequentially in a fixed order (by position). Pixels equal to
the fill code are skipped.

**Why the fit is stored rather than recomputed at replay.** The published
TIFF is strip-compressed by rows. Re-fitting needs the scratch's full
length, which means decoding the whole image on every preview and every
1:1 region fetch. With the table and centre path stored in the op, apply
is local: a region render only needs the rect plus a margin of 48 rows
(4σ of the level smoothing) and 32 columns. It also pins the result: a
future change to the fitting code cannot silently change an existing
negative's pixels.

---

## 4. The `scratches` op

A state op, sibling of `tone` / `color` / `spots` / `crop`: the latest
wins, a trailing op coalesces in place, and it lives in TIFF space.

```json
{
  "detector_version": 1,
  "source": "auto",
  "enabled": true,
  "canvas": [10143, 11714],
  "scratches": [
    {
      "axis": "vertical",
      "band_px": 64,
      "centres": [4540.2, 4539.8, ...],
      "half_width_px": 24,
      "levels": [[r0, g0, b0], ... 12 rows],
      "table": "<base64 int16, shape (bins, 2*half_width+1, 3), scale 1e-5>",
      "score": -6.28,
      "agreement": 0.99
    }
  ]
}
```

- `centres`: one subpixel centre per band along the scratch, covering the
  whole canvas length.
- `table`: base64 of int16 keeps a scratch to a few KB. Plain JSON floats
  would be ~15 KB per scratch. Both are fine in the ops log; pick base64
  if `roll info` ever carries params, JSON otherwise.
- `found == 0` is still recorded (`"scratches": []`). That distinguishes
  "looked, nothing there" from "never looked" (a pre-feature negative).

**Liveness.** The op is live when all of these hold:

- `enabled` is true;
- the list is non-empty;
- `canvas` matches the published TIFF (same guard as `spots` / `crop`);
- the image is 3-channel.

Anything else is a no-op, never an error. An unknown
`detector_version` newer than the build parses to `None`, the same
degrade `_check_spots_params` applies.

**Replay order.** Scratches first, before spot repair and before any
geometry. A scratch is a property of the film, and every other op either
assumes clean pixels (spot detection) or moves them (crop, flip,
rotations).

---

## 5. Integration

### 5.1 CLI (Python)

**`scratches.py`** (new):

- `detect(image_codes, spans, film_extent) -> list[Candidate]`
- `fit(image_codes, candidate) -> ScratchFit`
- `scratches_params(canvas, fits, enabled) -> dict`
- `is_live(params, shape) -> bool`
- `apply(image_codes, params, *, region=None) -> image_codes`

`apply` works on uint16 in, uint16 out, and decodes/encodes only the
columns it touches. With `region=(x, y, w, h)` it expects the caller to
have read the margin (§3.3) and returns the healed rect.

**`library/repo.py`:**

- `SCRATCHES_OP = "scratches"` and a comment block alongside the others.
- `EditState.scratches: dict | None = None`.
- `validated_scratches_params` (raises) and `_parse_scratches_op`
  (degrades to `None`), sharing one checker the way the spots pair does.
  Unknown keys are ignored.
- `append_scratches_edit` → `_coalesce_state_edit`.
- `net_edit_state` fills the new field.
- No migration: `EditRow.op` / `params` are already generic.

**`stitch_pipeline._composite_and_publish`.** Next to
`estimate_rotation(result.image)`, and only when
`film_kind is FilmKind.COLOUR`:

```python
spans = ceils - floors  # from record.normalization, already set above
candidates = scratches.detect(result.image, spans, film_extent)
fits = [scratches.fit(result.image, c) for c in candidates]
```

Then, after the negative row exists and before `sync_previews` (the same
point the `rotate_fine` seeding uses):

- `enabled` = the previous **live-or-stale** `scratches` op's `enabled`
  if one exists, else `True`. This carries a user's "off" through a
  re-stitch. Unlike `rotate_fine` this seeds **adopted negatives too**:
  a re-stitch changes the canvas, the old op goes stale, and a fresh
  detection is the only way the heal survives.
- `repo.append_scratches_edit(...)`.
- Detection failure of any kind (exception, NaNs) logs a warning event
  and records nothing. It must never fail the stitch.
- Progress: fold the cost into `STITCH_UNITS_PER_NEGATIVE` after timing
  it on a real canvas. Don't guess.
- Memory: the strips are ~65 columns × H × 3 float32 per scratch, and the
  band responses are H/64 × W float32. Both are negligible next to the
  composite's budget, but add them to the estimate function's docstring
  so nobody has to re-derive it.

**`previews.py`:**

- `_display_image` calls `scratches.apply` before `spots.apply_repair`.
- The `_spots_cache_key` sibling `_scratches_cache_key` joins the preview
  cache key.
- `SCRATCHES_OP` joins `_STATE_PREVIEW_OPS`.
- `render_region`: when `scratches.is_live`, read the rect expanded by the
  margin through the existing strip reader, apply with `region=`, and
  slice. Do not route it through the full-decode exact path the way spots
  does; locality is the reason the fit is stored.

**`exporter.py`:**

- `scratches.apply` before `spots.apply_repair`.
- `rendered.scratches: {"detector_version", "count"}` in the XMP
  provenance when live.

**`edits.py` + `cli.py`:**

- `edit scratches --roll DIR --negative ID [...] (--on | --off)`.
  Selection-capable, validated up front like `tone`. Rewrites `enabled` on
  the current op, regenerates previews, emits `edit_recorded`. Fails
  `INVALID_EDIT` on a negative with no op, or on a monochrome roll.
- `edit detect-scratches --roll DIR --negative ID [...]`. Decodes the
  published TIFF and runs the same detect + fit, preserving `enabled`.
  This is the back-fill path for rolls stitched before the feature, and a
  recovery path if a stitch-time detection warned.
- `roll info`: a per-negative `scratches` block next to `spots`:
  `{detector_version, enabled, count, stale}`, or `null` when no op
  exists.

**Contract.** Bump `PROTOCOL_VERSION` 20 → 21. Update:

- `CONTRACT.md`: both commands, the `roll info` block, the stitch-time
  seeding, and the warning code for a failed detection;
- `schema.json`: the command enum and the `roll info` payload.

`ARCHITECTURE.md` gets a short subsection under §7 or §8 pointing here,
plus the module map row.

### 5.2 App (Swift)

- **`NegativeScratches.Summary`**, parsed from `roll info`'s block. It
  lives on `RollManifest.Negative` the way `spotsSummary` does.
- **`EditModel.setScratchRemoval(_ targets:, on:)`** round-trips
  `edit scratches`. Add a `scratchesTerm(of:)`
  (`enabled#count#stale`) to both preview cache-generation tokens beside
  `spotsTerm`; forgetting this means a toggle doesn't refresh the
  filmstrip.
- **Heal panel.** A "Scratches" section above the spot controls:
  - `Toggle("Remove scratches")`, bound to the displayed negative and
    applied to `edit.selectionTargets`, consistent with Detect;
  - a caption, one of: "3 found", "None found", "Colour film only"
    (mono roll, disabled), "Not analysed" (no op; offers a small
    "Analyse" button driving `detect-scratches`), or "Stale — analyse
    again".
  - No overlay markers in v1.
- **`AppActivity` / busy state** follows the spots pattern.

---

## 6. Testing and validation

### 6.1 Fast tier

Synthetic, built on `synthetic_scene`. Copy it; the fixture is read-only.
Encode it through `encode_normalized` with realistic floors/ceils, then
inject a scratch using the *measured* profile from §1: per-channel core
and shoulder shape, level-dependent depth, and a smooth random-walk centre
with ±20 px drift.

- **Detect:** finds the injected scratch; centre error ≤ 1.5 px RMS;
  vertical and horizontal (transposed) both work.
- **Detect, clean scene:** zero detections on the uninjected scene,
  across several seeds.
- **Detect, step edge:** a synthetic frame border (a step in all
  channels, full length) is rejected.
- **Detect, scene line:** a thin, full-length *neutral* dark line is
  rejected by the chroma-sign gate.
- **Heal:** the RMS of healed − clean inside ±24 px is below a bound, and
  pixels outside ±24 px are byte-identical.
- **Heal, level dependence:** the error stays within the bound in both
  the densest and the thinnest third, which guards against regressing to
  the constant model.
- **Apply determinism:** apply(params) on the same image is
  byte-identical twice. A region apply with margin equals the slice of a
  full apply (mirror `test_render_region_matches_full_decode`).
- **Params:** round-trip; malformed shapes raise; newer
  `detector_version` degrades to `None`; canvas mismatch → not live;
  2-D image → no-op.
- **Stitch pipeline**, via `make_work_dir` with a frame hook that injects
  a scratch into every frame at a consistent film position:
  - colour roll → one `scratches` op with `enabled: true`;
  - monochrome roll → no op;
  - re-stitch after `--off` → the new op has `enabled: false` and the new
    canvas;
  - a detection that raises → the stitch still publishes, and a warning
    is emitted.
- **Edits:** `--on/--off` over a selection; `INVALID_EDIT` on mono and
  on a negative with no op; the preview is regenerated.

### 6.2 Swift

`EditModelTests`: the summary parses; the toggle round-trips; the cache
token changes on toggle. `CLIEventTests`: the `roll info` block.

### 6.3 Calibration on real scans

The gates in §3.1 are from four negatives. Before trusting defaults, add
`cli/tools/measure_scratches.py`. It is not a test and must not load at
runtime. Given a roll folder, per negative it writes:

- all candidates with score / agreement / drift / step ratio, and
  accept/reject reasons;
- held-out residual metrics as in §1;
- before / after / 5×-difference crops at fixed rows for each accepted
  scratch, as a PNG contact sheet.

Run it over `Recalibrate-Geometry`, then over at least one roll *without*
visible scratches and one with strong linear scene content (poles,
buildings, horizons in rotated frames). Set the gates from that and record
the measured values in the module docstring, the way `composite.py`
records which constants are measured and which are provisional.

---

## 7. Risks and open questions

- **Frame-edge false positives.** The step-edge gate's threshold is
  unmeasured. Without it, frame borders were the strongest false hits in
  every negative.
- **Scene lines along the film axis.** A thin, slightly blue-casting
  vertical wire could pass every gate. The chroma-sign gate and the
  full-length requirement make this rare, not impossible. The toggle is
  the only remedy in v1; per-scratch reject is the obvious v2.
- **Faint scratches are missed.** On `_DSC5264` the third scratch scores
  −1.97 with 60% agreement. Per-image, that is below any safe gate.
  Accepted cost of having no roll prior.
- **Partial scratches.** A scratch that starts mid-frame fails the
  agreement gate. A later version could track segments and store a row
  range per scratch; the op shape above leaves room (`centres` could
  become `{start_band, centres}`).
- **Core noise.** Where blue transmission is ~0.3, the correction lifts
  core blue by ~×3 in linear light and lifts its noise with it. That is
  the faint residual in dark, grainy crops. A tier-2 step could blend the
  blue channel of `|u| ≤ 2` toward a cross-scratch interpolation, weighted
  by `1 − t`. Evaluate after v1 lands, using the calibration contact
  sheets.
- **Clipped rows.** Headroom-clipped highlights (3% of pixels on
  `_DSC5207`) carry no recoverable dip. The level bins already
  under-correct there, which is the safe direction.
- **Fit bins on uniform scratches.** A scratch crossing mostly one flat
  area has little level spread. The bin merge in §3.2 must be tested, not
  assumed.
- **App refresh after a run.** Verify that the app re-reads `roll info`
  after `run` finishes, so the new block appears without an event. If it
  doesn't, extend `_composite_and_publish`'s returned `edit_recorded`
  fields from one dict to a list.

---

## 8. Order of work

1. **Algorithm, with no pipeline changes.** Build `scratches.py` detect /
   fit / apply, the synthetic fast tests, and `measure_scratches.py`. Run
   calibration on real rolls and set the gates. This is where the risk
   is, so it goes first; nothing else is worth building until the contact
   sheets look right.
2. **Op and replay.** Add the repo op and parser, the previews and
   render-region path, the exporter and provenance, and both `edit`
   commands and the `roll info` block. Then the stitch-time seeding with
   re-stitch carry-forward, the contract bump, and the pipeline/edit
   tests.
3. **App.** Summary parsing, the Heal panel section, cache tokens, tests.
