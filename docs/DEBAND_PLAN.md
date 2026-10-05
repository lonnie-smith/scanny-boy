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

*This section describes the model as built and calibrated (step 1,
`cli/tools/measure_deband.py`); it supersedes the first-draft §3.1 (a single
shared baseline, no cross-block agreement). The constants and their
provenance are listed in `deband.py`'s docstring.*

All arithmetic is in log10 density: `val` (`decode_normalized`) × the
per-channel span (`ceil − floor` from the negative's normalization record),
as in `scratches.py`. The stored correction is converted back to `val`
units per channel, so replay is a plain subtraction. The op carries the
spans (§4), so replay can also judge the protection weight in log10.

Module: `cli/src/scanny_boy/deband.py`, a leaf importing only `numpy`,
`scipy.ndimage`, `cv2` and `normalization`.

Everything below is written for the vertical axis ("bands vary along the
window's height"). Horizontal swaps the window's local width and height
roles; the source image is never transposed.

### 3.1 Fit (per region)

Input: the region's TIFF-space window `(x, y, w, h, tilt)`, and the image
with all earlier regions already applied (§3.3).

1. **Sample.** A tilted window is warped straight (`cv2.warpAffine`, window
   rotation, `SAMPLE_MARGIN_PX` = `REGION_MARGIN` = 192 px of margin on each
   side) so the statistics can be taken on an axis-aligned grid. Pixels the
   warp filled (outside the canvas) are never background.
2. **Background mask.** `L` = mean over channels (log10), `Ls` =
   `GaussianBlur(L, σ=48)`. A pixel is background when `|L − Ls| < 0.03`.
   This excludes wires, poles and edges. A region with under 20 % background
   pixels is rejected (`ValueError`, surfaced as `INVALID_EDIT`: "pick a
   flatter area"). Regions under 256 px along the bands or 512 px across are
   rejected the same way.
3. **Per-block profiles.** Split the region's lines into blocks of 256. Per
   block, per column, per channel: the median over background pixels.
   Columns with under 40 % background across the whole region (a pole or
   tower) or under 50 % within the block are unmeasured there; a block with
   under 25 % measurable columns is unmeasured.
4. **Per-block baseline.** *Each block's* profile gets its own robust
   degree-2 baseline (3 passes, rejecting residuals > 2.5 × 1.4826 × MAD),
   subtracted. This separates "band" from the scene's own gradual gradient,
   and it has to be per block: a baseline shared across blocks turned each
   block's own vertical sky gradient into a "correction".
5. **Common profile, not per-block profile.** A band runs the whole length,
   so the correction is what the measured blocks agree on:
   - per column, the densest cluster of the blocks' low-passed (σ = 96 px)
     deviations, within 0.012 log10 of each other (ties go to the cluster
     nearer zero); its median is the **common profile**;
   - a cloud, a tree's shadow or any broad object that only some blocks
     see falls outside the cluster and is never "corrected" into the band;
   - a block within 0.004 log10 of the common profile also contributes its
     own small *wander* (a tilted or drifting edge), fading out with the
     offset, capped at 0.02;
   - gaps are filled along the band (a cell unmeasured in one block takes
     the nearest measured blocks' value in that column), then across x for a
     column unmeasured in every block.
6. **Confidence.** The correction is scaled by the cluster's share of the
   measured blocks (smoothstep between 0.5 and 0.8, blurred 48 px), so
   ambiguous columns get no correction rather than a guess. Columns within
   24 px of an unmeasured run are re-derived from clean columns beyond (an
   object's halo).
7. **Smoothing, chroma only, cap.** Gaussian across blocks (σ = 1 block) and
   across x (σ = 2 px: enough to cut grain without blurring a sharp edge;
   σ = 8 left a visible line). Then `corr −= mean over channels`, so it sums
   to zero across channels in log units and luminance is untouched. A hard
   cap of 0.05 log10 on the stored correction (measured bands are ≤ 0.03)
   limits the damage from a misused region.
8. **Store** `corr` per block at an x pitch of **8 px**, in `val` units
   (÷ span), int16 at scale 1e-5, zlib, base64. (The plan said 4 px;
   identical held-out scores at 8, half the size: a 1 876-line region is
   about 46 KB, a full 6 000-line frame about 90–130 KB.)

Cost: well under a second per region.

### 3.2 Apply (per region)

For each pixel inside the region's feathered mask:

- **Look up** `corr`: bilinear across x (pitch) and across blocks, by the
  pixel's position in the window's local frame. The window's rotation is
  applied *algebraically* to the pixel's position; the image itself is
  never resampled. That is what makes a region render byte-identical to a
  slice of the full render. `tilt` is the same quantity the `crop` op
  stores: counter-clockwise as displayed, `cv2.getRotationMatrix2D`'s sense.
- **Protection weight.** `p = exp(−((L − Ls) / 0.04)²)`, with `L` and
  `Ls` in **log10** (via the op's `spans`) and `Ls` the σ = 48 blur
  recomputed from the image being corrected. Pixels that differ from their
  surroundings (tower, wires, a building edge at the horizon) keep their
  colour.
- **Feather.** A mask ramp over `min(128 px, 10 % of the side)` on all four
  sides of the window.
- `val[..., ch] −= strength · feather · p · corr_ch`.

Pixels equal to the fill code are skipped. Values are clipped to the encode
headroom like `scratches._apply_one_scratch`. The only neighbourhood
dependence is `Ls`, so one region needs a read margin of
**`REGION_MARGIN` = 192 px** (4σ: `cv2.GaussianBlur` with `ksize=0` reads
4σ, not 3σ, and a 144 px margin was measurably not enough).

### 3.3 Ordering, and the multi-region rule

Regions apply in list order. Region *k* is fitted on the image with regions
`< k` already applied (`fit_region(..., prior_regions=...)`), so sequential
replay matches what each fit saw. Removing a region or changing the axis
refits every region, in order, from their stored windows; adding one fits
only the new region on top of the stored ones. Scratch correction is
applied before every fit, because replay applies it before deband.

**Region renders.** Region *k*'s protection blur reads the image after
regions `< k`, so the zone where a render is exact shrinks by one margin
per sequential region. `previews.render_region`'s fast path therefore reads
the rect widened by **`REGION_MARGIN` × (number of regions in the op)** —
not by one margin — and, when scratches are live too, by the scratch margin
beyond that (scratches replay first and finish exact over the deband rect
before deband runs). The alternative, sending every multi-region render
through the full-decode exact path, was rejected: it would decode and cache
a ~300 MB display image for what is, with three regions, a 1 150 px margin
on a viewer tile, and viewer tiles arrive constantly. Tests hold
byte-identity of the region render against the full render's slice for one
and for three overlapping regions, including hand-made regions that shift
luminance. (Fitted corrections are chroma-only, so a real region barely
changes the luminance the next region's protection weight reads; with real
fits a single margin differs from the full render at most at the
quantization level. The per-region margin is the rule that is exact by
construction, not by that coincidence, and it costs only a wider read.)

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
  "spans": [1.35, 1.25, 1.45],
  "regions": [
    {
      "id": 1,
      "window": [150.0, 5000.0, 7970.0, 1092.0, 0.0],
      "block_px": 256,
      "pitch_px": 8,
      "blocks": 5,
      "corr": "<base64 zlib int16, shape (blocks, ceil(cross/pitch), 3), val units, scale 1e-5>"
    }
  ]
}
```

- `axis` is the **TIFF-space** direction the bands run; `roll info` and
  `edit_recorded` report it as displayed (swapped under odd quarter turns).
- `spans` is the negative's per-channel `ceil − floor` (log10) at fit time.
  It makes the protection weight's 0.04 log10 tolerance exact at replay.
  `fit_version` stays 1: nothing was ever persisted before spans existed.
- `window` is `(x, y, w, h, tilt_deg)` in TIFF pixels, the same shape and
  tilt sense the `crop` op stores, so the display→TIFF mapping is shared
  (`previews.display_crop_window_to_tiff`). `id` is a small positive int,
  stable across edits.
- `strength` is 0–1.5. `repo.validated_deband_params` checks every field and
  decodes each table against its window (a corrupt table is rejected when
  written and replays as no op). Unknown keys are ignored.
- **Size.** Target ≤ 64 KB per region held for regions up to ~3 000 lines;
  a full-frame region is ~90–130 KB. `edit_recorded` omits the `corr`
  tables.
- **Liveness.** The op is live when `enabled`, `regions` is non-empty,
  `canvas` matches the TIFF, `spans` is valid, and the image is 3-channel.
  Anything else is a no-op, never an error. A newer `fit_version` parses to
  `None`.
- **Replay order.** `scratches` → `deband` → `spots` → geometry
  (`heal.apply`). Scratch tables were fitted on un-debanded pixels but
  deband is fitted on scratch-corrected ones; spot detection wants clean
  pixels; the deband window is in TIFF space.
- **Pixel-cache key.** `heal.cache_key` gained a third element, a hash of the
  whole op (a new key shape; the cache is in-memory only).

---

## 5. Integration

### 5.1 CLI (Python)

**`deband.py`** (new):

- `fit_region(image_codes, spans, window, axis, *, prior_regions=None,
  row_mask=None) -> RegionFit` (`row_mask` is a calibration hook)
- `deband_params(canvas, axis, fits, enabled, strength, spans, *, ids=None)
  -> dict`
- `is_live(params, shape) -> bool`
- `apply(image_codes, params, *, region=None, origin=None) -> image_codes`
  (the `scratches.apply` signature)
- `fits_from_params`, `clamp_window`, `parse_region` (used by the CLI)
- `REGION_MARGIN = 192`, `PITCH_PX = 8`

**`library/repo.py`:**

- `DEBAND_OP = "deband"` and a comment block.
- `EditState.deband` (also `HealParams.deband`, `heal.apply` order
  scratches → deband → spots).
- `validated_deband_params` and `_parse_deband_op` sharing one checker.
- `append_deband_edit` → `_coalesce_state_edit`.
- `net_edit_state` fills the new field.

**`previews.py` / `exporter.py`.**

- With `HealParams` (step 2a) previews and the exporter already replay
  through `heal.apply`, so the work was:
  - `heal.cache_key`'s deband element;
  - `_STATE_PREVIEW_OPS`;
  - `render_region`, with the rect widened by `REGION_MARGIN` × (number of
    regions) when live (§3.3);
  - export, with XMP `rendered.deband: {fit_version, regions}` (the region
    count) when live.
- Auto-tone and auto-colour do **not** read pixels: they solve from the
  negative's stored normalization record, so a deband op cannot change
  them. Auto-neutral (`auto_neutral.measure_auto_neutral_from_tiff`) and
  auto-crop do read the raw published TIFF with no heal replay, exactly as
  for scratches and spots; they ignore the op.

**Worth deciding first.** Adding a second heal op this way threads another
kwarg through ~50 call sites. A small preparatory refactor, bundling
`scratches` / `deband` / `spots` into one `HealParams` passed as a unit,
would make this and any future heal op a few-line change. Recommended as
step 2a (§8), mechanical, landing with no behaviour change.

**`stitch_pipeline._composite_and_publish`.**

- **Re-stitch** (`stitch_pipeline._refit_deband`). When a previous `deband` op exists, even a stale one:
  1. clamp each window to the new canvas;
  2. refit each region in order on the new image;
  3. preserve `enabled`, `strength`, `axis` and the region ids;
  4. append the new op.
- **Failure.** Any exception emits a `DEBAND_REFIT_FAILED` warning and
  records nothing (the old op stays, stale). It never fails the stitch.
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
    space through the quarter turns and refits everything. A fresh op runs
    along the TIFF's vertical axis.
  - `--on` / `--off` and `--strength S`, which need no refit.
  - `--clear`.
- **Failures.** `INVALID_EDIT` on a monochrome roll, on a region smaller
  than 256 px along the band or 512 px across, or on a region with < 20 %
  background pixels. The last one gets a message telling the user to pick
  a flatter area.
- **Output.** Regenerates previews and emits `edit_recorded`, whose
  `deband` field carries the same display-space report as `roll info`
  (every edit confirmation does, because a rotation moves the rects) and
  whose `edit.params` omits the `corr` tables. Selection is not supported
  (single `--negative`).
- **`roll info`.** A per-negative `deband` block:
  `{fit_version, enabled, strength, axis, regions: [{id, display_rect}], stale}`,
  with display-space rects (via `tiff_rect_to_display`, with the window's
  tilt) for the overlay, or `null` when there is no op. `stale` means the
  op's canvas no longer matches (a failed re-stitch refit).

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
- **Multi-region render.** The region render of an op with three
  overlapping regions equals the full render's slice byte for byte (§3.3).
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

1. **Algorithm, no pipeline changes** (done). Build `deband.py` fit/apply, the
   synthetic fast tests, and `measure_deband.py`. Calibrate on the §6.3
   set. This is where the risk is, so it goes first.
2. **Op and replay.**
   - 2a. The `HealParams` refactor (done, no behaviour change).
   - 2b. (Done.) The repo op and parser; previews, `render-region` and export;
     `edit deband` and the `roll info` block; re-stitch refit; the
     contract bump; pipeline and edit tests.
3. **App.** Summary parsing, the Heal panel section, draw mode (the
   largest UI piece), overlays, cache tokens, tests.
