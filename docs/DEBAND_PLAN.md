# Deband plan

User-guided removal of **development bands**: broad, low-contrast colour
bands running along the film's length, caused by uneven processing. The
user draws one or more regions over flat, banded areas (sky, pavement) in
the Heal panel. Each region fits a per-line colour correction from its own
pixels and applies it inside the region. The result is recorded as an
ordinary edit op and replayed wherever the ops log is (previews, 1:1 region
renders, export).

Unlike scratch removal, nothing is detected automatically: the evidence in
§1 says an automatic detector would miss real bands and "fix" real scene
colour.

---

## 1. What we are removing

Measured on the `Sep-20-2026-at-4-46-PM` roll (645 on 120, Jobo rotary,
2×2 grids, ~8 300 × 6 250 px published canvases).

**Provenance: the film, not the pipeline.** On `20260920-165541` the raw
tiles were decoded through the flat-field path and compared in overlap.
The sharp band edge sits at *film* x≈2700 in both bottom tiles (sensor
x≈2600 in tile 02, x≈500 in tile 01). The broad band at film x≈5000–7000
appears in both top tiles at the same film position. The bare-light frame
is flat to < 0.005 log10 B/G. The seam crossover (x≈4170) is not involved.
Nothing to fix in stitching or colour conversion. A separate, smaller
issue, tile-to-tile chroma disagreement of ≤ 0.01 log10 near frame edges,
is noted in §7.

**Shape.**

- Runs along the film's length. On this rig that is the TIFF's vertical;
  the op stores the axis rather than assuming it.
- **Blue record only.** It is a change in yellow-dye density of up to
  ~0.03 log10 in raw transmission. In the positive it reads as yellow
  bands.
- Two profiles seen:
  - *Sharp-edged band.* A step over ~100–200 px, decaying back over
    300–500 px. Seen in `165541` (x≈2750) and `165142` (x≈2350).
  - *Broad hump.* ~1 500–2 000 px wide with soft edges. Seen in
    `165541` (5000–7000) and weakly in `165037` (6000–7000).
- **Position varies per frame.** No fixed across-film position on the
  roll.
- **Not always full-length.** In `165541` the x≈2750 band is present in
  the sky and absent from the pavement at the other end of the frame. The
  broad hump is in both.

This is why the tool is region-based and user-guided.

**Survey.** 13 negatives, 7 with enough flat area to judge:

| result | frames |
| --- | --- |
| clear bands | `165541`, `165142` |
| weaker | `165001`, `165037` |
| nothing clear | `164837`, `164919`, `165111` |

**Prototype results** on `165541`, fit on even 32-row bands and scored on
odd ones. The metric is the p99 |deviation from a quadratic| of the
held-out B−G column profile, ×10⁻³ log10, with scene objects masked. Grain
noise floor ≈ 3.

| region | before | one whole-region profile, σ=8 px | 256-row blocks, σ=2 px (chosen) |
| --- | --- | --- | --- |
| sky, rows 5000–6092 | 21.4 | 4.4, and a visible line left at the sharp edge | 3.8, no visible line |
| pavement, rows 184–2060 | 16.1 | 3.3 | 2.4 |

- **Baseline degree.** Degree 1 and 2 were equivalent. Degree 3 was
  worse held-out (sky 5.5, pavement 3.9). Degree 4 absorbed the broad
  pavement band entirely.
- **Protection is not optional.** Without the object-protection weight
  (§3.2), a lattice tower spanning every row of the sky region was
  recoloured to sky.

---

## 2. Scope

**In:**

- Colour rolls only (`FilmKind.COLOUR`). The correction is chroma-only.
- One or more user-drawn regions per negative, each fitted and applied
  independently, in order.
- One band axis per negative (vertical or horizontal in TIFF space).
- A whole-negative on/off switch and a global strength (0–1.5,
  default 1.0).
- Replay in previews, `render-region`, and export.
- Automatic refit of existing regions on re-stitch.

**Out (v1):**

- Automatic detection or region suggestion. This is the obvious v2: see
  §7.
- Luminance (neutral-density) bands.
- Density-dependent correction within a region (§7).
- Freeform masks or brushes. Regions are rectangles, tilted to match the
  display when a fine rotation is present.
- Applying one negative's regions to a selection. Band positions differ
  per frame.

**Relationship to `scratches.py` / `spots.py`.** Independent. The new
module `deband.py` imports neither, the op is a new op kind, and the UI is
its own Heal panel section.

---

## 3. Algorithm

All arithmetic is in `val` (`decode_normalized`) × per-channel span from
the negative's normalization record, i.e. log10 units, as in
`scratches.py`. The stored correction is converted back to `val` units per
channel, so replay is a plain subtraction.

New module: `cli/src/scanny_boy/deband.py`. It is a leaf importing only
`numpy`, `scipy.ndimage`, `cv2`, and `normalization`.

Everything below is written for the vertical axis. Horizontal runs on the
transposed view.

### 3.1 Fit (per region)

Input: the region's TIFF-space window `(x, y, w, h, tilt)`, and the image
with all earlier regions already applied (§3.3, ordering).

1. **Sample.** Read the window's bounding box plus a 144 px margin, and
   rasterize the tilted window as a mask.
2. **Background mask.** `L = mean over channels`,
   `Ls = GaussianBlur(L, σ=48)`. A pixel counts as background when
   `|L − Ls| < 0.03`. This excludes wires, poles and edges.
3. **Block profiles.** Split the region's rows into blocks of 256. Per
   block, per column, per channel, take the median over background
   pixels.
   - Columns whose background fraction across the whole region is
     < 0.4 (a full-height pole or tower) are interpolated from their
     neighbours.
   - Columns whose background fraction within the block is < 0.2 are
     interpolated too.
4. **Smooth along the band.** Gaussian across blocks, σ = 1 block. Bands
   vary slowly along their length, and this lets a slightly tilted or
   wandering edge be tracked.
5. **Baseline.** Take one robust polynomial of degree 2 over the region
   width, fitted to the block-mean profile per channel (3 passes,
   rejecting residuals > 2.5 × 1.4826 × MAD). This is what separates
   "band" from the scene's own gradual gradient (sky toward the horizon,
   light falloff). It is shared by every block.
6. **Deviation.** `dev = profile − baseline`, then a Gaussian across x
   with σ = 2 px. That is enough to cut grain without blurring a sharp
   edge; σ = 8 leaves a visible line (§1).
7. **Chroma only.** `corr = dev − mean_over_channels(dev)`. The
   correction sums to zero across channels in log units, so luminance is
   untouched.
8. **Store** `corr` per block at an x pitch of 4 px (§4). The pitch is to
   be confirmed in step 1 of §8.

Cost in the prototype: well under a second per region, in unoptimized
numpy.

### 3.2 Apply (per region)

For each pixel inside the region's feathered mask:

- **Look up** `corr`: bilinear across x (pitch) and across blocks, by the
  pixel's position along the band axis.
- **Protection weight.** `p = exp(−((L − Ls) / 0.04)²)`, with `Ls` the
  σ=48 blur recomputed from the image being corrected. Pixels that differ
  from their surroundings (tower, wires, a building edge at the horizon)
  keep their colour.
- **Feather.** A mask ramp over `min(128 px, 10 % of the side)` on all
  four sides of the window.
- `val[..., ch] −= strength · feather · p · corr_ch / span_ch`.

The correction is purely per pixel, given the stored table. The only
neighbourhood dependence is `Ls`, so a region render needs a margin of
144 px (3σ). Pixels equal to the fill code are skipped. Values are clipped
to the encode headroom like `scratches._apply_one_scratch`.

### 3.3 Ordering

Regions apply in list order. Region *k* is fitted on the image with
regions `< k` already applied, so sequential replay matches what each fit
saw. Removing or changing the axis refits every region, which is cheap.

**Why the fit is stored.** Same reasons as scratches: a 1:1 region render
must not decode the whole region on every fetch, and a later change to the
fitting code must not silently change an existing negative. Refit happens
only on explicit edits and re-stitch.

---

## 4. The `deband` op

A state op, sibling of `scratches` / `spots` / `crop`: the latest wins, a
trailing op coalesces in place, and it lives in TIFF space.

```json
{
  "fit_version": 1,
  "enabled": true,
  "strength": 1.0,
  "axis": "vertical",
  "canvas": [8269, 6255],
  "regions": [
    {
      "id": 1,
      "window": [150, 5000, 7970, 1092, 0.0],
      "block_px": 256,
      "pitch_px": 4,
      "blocks": 5,
      "corr": "<base64 zlib int16, shape (blocks, ceil(w/pitch), 3), val units, scale 1e-5>"
    }
  ]
}
```

- `window` is `(x, y, w, h, tilt_deg)` in TIFF pixels, the same shape the
  `crop` op stores, so the display→TIFF mapping is shared.
- **Size.** zlib before base64. The profiles are smooth, so this should
  compress several-fold. Target ≤ 64 KB per region; confirm in step 1.
- **Liveness.** The op is live when:
  - `enabled` is true;
  - `regions` is non-empty;
  - `canvas` matches the TIFF;
  - the image is 3-channel.

  Anything else is a no-op, never an error. A newer `fit_version` parses
  to `None`.
- **Replay order.** `scratches` → `deband` → `spots` → geometry.
  - Scratch tables were fitted on un-debanded pixels.
  - Spot detection wants clean pixels.
  - The deband window is in TIFF space.

---

## 5. Integration

### 5.1 CLI (Python)

**`deband.py`** (new):

- `fit_region(image_codes, spans, window, axis, prior_regions) -> RegionFit`
- `deband_params(canvas, axis, fits, enabled, strength) -> dict`
- `is_live(params, shape) -> bool`
- `apply(image_codes, params, *, region=None, origin=None) -> image_codes`
  (the `scratches.apply` signature)
- `REGION_MARGIN = 144`

**`library/repo.py`:**

- `DEBAND_OP = "deband"` and a comment block.
- `EditState.deband`.
- `validated_deband_params` and `_parse_deband_op` sharing one checker.
- `append_deband_edit` → `_coalesce_state_edit`.
- `net_edit_state` fills the new field.

**`previews.py` / `exporter.py`.**

- Follow every `scratches_params` site: there are ~36 in `previews.py`
  and ~13 in `exporter.py`. The core ones are:
  - `_display_image`, applied after `scratches.apply`;
  - `_deband_cache_key`;
  - `_STATE_PREVIEW_OPS`;
  - `render_region`, with the rect expanded by `REGION_MARGIN` when live;
  - export, with XMP `rendered.deband: {fit_version, regions}`.
- Check that auto-tone and auto-colour analysis read through
  `_display_image`, so they see debanded pixels.

**Worth deciding first.** Adding a second heal op this way threads another
kwarg through ~50 call sites. A small preparatory refactor, bundling
`scratches` / `deband` / `spots` into one `HealParams` passed as a unit,
would make this and any future heal op a few-line change. Recommended as
step 2a (§8), mechanical, landing with no behaviour change.

**`stitch_pipeline._composite_and_publish`.**

- **Re-stitch.** When a previous `deband` op exists, even a stale one:
  1. clamp each window to the new canvas;
  2. refit each region in order on the new image;
  3. preserve `enabled`, `strength` and `axis`;
  4. append the new op.
- **Failure.** Any exception logs a warning and records nothing. It never
  fails the stitch.
- Windows can be off by the canvas shift between stitches (tens of px).
  That is acceptable: regions are coarse, and the fit recomputes the
  correction from the new pixels.

**`edits.py` + `cli.py`:**

- `edit deband --roll DIR --negative ID` with one of:
  - `--add-region X,Y,W,H [--tilt DEG]`, in **display space**. The CLI
    maps it through `display_crop_window_to_tiff` ("the CLI converts;
    Swift never does"), fits, and appends.
  - `--remove-region ID`, which refits the remainder.
  - `--axis vertical|horizontal`, as displayed. The CLI converts to TIFF
    space through the quarter turns and refits everything.
  - `--on` / `--off` and `--strength S`, which need no refit.
  - `--clear`.
- **Failures.** `INVALID_EDIT` on a monochrome roll, on a region smaller
  than 256 px along the band or 512 px across, or on a region with < 20 %
  background pixels. The last one gets a message telling the user to pick
  a flatter area.
- **Output.** Regenerates previews and emits `edit_recorded`.
- **`roll info`.** A per-negative `deband` block:
  `{fit_version, enabled, strength, axis, regions: [{id, display_rect}], stale}`,
  with display-space rects (via `tiff_rect_to_display`) for the overlay,
  or `null` when there is no op.

**Contract.** Bump `PROTOCOL_VERSION` 24 → 25. Update
`shared/contract/CONTRACT.md` (the command, the `roll info` block, the
re-stitch refit, and the failed-refit warning code) and `schema.json`.
`ARCHITECTURE.md` gets a pointer here plus the module-map row.

### 5.2 App (Swift)

- **`NegativeDeband.Summary`**, parsed from `roll info`, on
  `RollManifest.Negative` like `scratchesSummary`.
- **`EditModel`:**
  - `addDebandRegion(_:displayRect:tilt:)`, `removeDebandRegion(_:id:)`,
    `setDeband(_:enabled:)`, `setDebandStrength(_:_:)`,
    `setDebandAxis(_:_:)`;
  - `isDebanding` busy state;
  - a `debandTerm(of:)` (`enabled#strength#axis#regionCount#stale`) in
    both preview cache-generation tokens beside `scratchesTerm`.
    Forgetting it means edits don't refresh the filmstrip.
- **Heal panel.** A "Bands" section between Scratches and Spots:
  - `Toggle("Remove bands")`, disabled with no regions.
  - A "Bands run" segmented control (↕ / ↔, as displayed).
  - A Strength slider, 0–1.5, double-click resets to 1.0. It commits on
    release, like the tone sliders.
  - A region list: "Region 1 · 7970 × 1092" with a remove button.
    Hovering a row highlights its outline.
  - An **Add Region** button, which enters draw mode.
  - Captions: "Colour film only" (mono, disabled), "Stale — re-stitched,
    regions refitted" (informational), or a failure message from
    `INVALID_EDIT`.
- **Draw mode.** Reuse `CropGeometry` (resize, translate, clamp, handles)
  and a stripped-down `CropOverlayView`, with no presets or tilt handle;
  tilt comes from the live fine rotation.
  - Drag to create, adjust with handles.
  - **Add** / **Cancel** buttons, Return / Esc.
  - Other preview gestures (spot toggles, zoom-drag) are suspended while
    it is active.
  - The rect is sent in display space.
- **Overlays.** Dashed outlines of existing regions while the Heal tab is
  selected, drawn like spot markers from the summary's display rects.
- **Scope.** Region editing is per displayed negative, not
  `selectionTargets`.

---

## 6. Testing and validation

### 6.1 Fast tier

Synthetic, from a copy of `synthetic_scene`, encoded through
`encode_normalized` with realistic floors and ceilings. Inject bands with
the measured §1 shapes (sharp step with exponential decay, and a broad
hump), blue-only, 0.01–0.03 log10, plus a smooth legitimate horizontal
gradient that must survive.

- **Removal.** Inside the region, the held-out B−G column-profile p99 is
  below a bound. The injected gradient is preserved to within a bound.
- **Locality.** Pixels outside the feathered window are byte-identical.
- **Protection.**
  - A full-height pole inside the region keeps its chroma to within a
    bound.
  - A thin neutral wire is unchanged.
- **Luminance.** The mean luminance over the region is unchanged to
  within quantization.
- **Axis.** Horizontal bands on the transposed scene behave the same.
- **Ordering.** Two overlapping regions apply deterministically. Removing
  the first refits the second.
- **Region render.** An apply with `REGION_MARGIN` equals the slice of a
  full apply (mirror `test_render_region_matches_full_decode`).
- **Params.**
  - Round-trip.
  - Malformed params raise.
  - A newer `fit_version` gives `None`.
  - A canvas mismatch is not live.
  - A 2-D image is a no-op.
- **Edits.**
  - Add, remove, axis, strength and on/off.
  - Display→TIFF mapping under quarter turns, flip and fine rotation,
    reusing the crop tests' transforms.
  - `INVALID_EDIT` on mono, on too-small regions, and on regions with
    too little background.
- **Stitch pipeline** (`make_work_dir`, re-stitch):
  - regions are refitted onto the new canvas, with settings preserved;
  - a refit that raises still publishes and emits a warning.

### 6.2 Swift

- `EditModelTests`: the summary parses; each command round-trips; the
  cache token changes on every edit.
- `CropGeometry` reuse tests for draw mode.
- `CLIEventTests`: the `roll info` block.

### 6.3 Calibration on real scans

`cli/tools/measure_deband.py`. Not a test, never loaded at runtime. Given
a roll folder and region specs, it writes:

- the held-out metrics from §1;
- the correction's range;
- before / after / 10× chroma contact sheets per region.

Run it on:

- **Bands:** `165541` sky and pavement, `165142` sky, `165001` sky.
- **Negative controls:** `164837` and `165111` sky. The correction must be
  near zero there, with no visible change.
- **Strong scene content:** a region deliberately drawn over trees or a
  facade, to see what the protection weight and the baseline do when
  misused.

Set block size, σ, pitch, tolerances and baseline degree from that, and
record which constants are measured in the module docstring.

---

## 7. Risks and open questions

- **Band vs gradient.** The baseline degree decides what counts as a
  band. Degree 2 removes bands up to ~¼ of the region width and keeps
  smoother gradients. A band wider than that is partly kept; a real
  mid-frequency colour change in the region is partly removed. The
  mitigations are the user's choice of region, the strength slider, and
  the toggle. Consider a "Band width" control only if calibration shows
  a need.
- **Region side edges.** If `corr` is far from zero at a region's
  left/right edge, the 128 px feather can still show a soft step. The
  guidance is to draw to the frame edges or to natural boundaries, where
  the protection weight takes over. Calibration should report `corr` at
  the edges; if needed, taper the correction to zero across the outer 5 %
  of the width.
- **Density dependence.** Development differences should scale with
  density, and the `165541` band being absent from the thin pavement is
  consistent with that, or with the band being length-limited. v1 is
  constant within a region, since a region is a flat area by
  construction. Revisit if calibration shows over-correction across
  clouds.
- **Size of the ops log** for full-frame regions. Measure after zlib.
- **Automatic suggestion (v2).** Propose the largest flat area as a
  region the user accepts or adjusts. My automatic flat-row selection
  during the investigation was unreliable, so this needs its own
  measurement.
- **Stitching aside.** Overlapping tiles disagree by up to ~0.01 log10
  B/G near frame edges: flat-field and per-frame scalar gain residual.
  Not the cause of these bands, and invisible at the seam midline, but it
  modulates their apparent strength. Worth its own look under
  `composite.py` / `flatfield.py`, separately from this plan.

---

## 8. Order of work

1. **Algorithm, no pipeline changes.** Build `deband.py` fit/apply, the
   synthetic fast tests, and `measure_deband.py`. Calibrate on the §6.3
   set. This is where the risk is, so it goes first.
2. **Op and replay.**
   - 2a. The `HealParams` refactor, if adopted. No behaviour change.
   - 2b. The repo op and parser; previews, `render-region` and export;
     `edit deband` and the `roll info` block; re-stitch refit; the
     contract bump; pipeline and edit tests.
3. **App.** Summary parsing, the Heal panel section, draw mode (the
   largest UI piece), overlays, cache tokens, tests.
