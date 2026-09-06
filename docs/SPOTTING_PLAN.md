# Spotting plan: detect crud, review it, then repair what survives

Dust, hairs, water spots and scratches are the last manual step in the
workflow, and the tedious part is not deciding what to fix — it is *finding*
the specks. This plan adds a detector that proposes candidates, a review
step where the user rejects the bad proposals, and a repair that runs only
on what survives review.

Three principles hold the whole design together, and every decision below
follows from one of them:

1. **The published TIFF is never touched.** Detection and repair are a
   `spots` op in the negative's ops log, exactly like `tone` and `color` —
   except that unlike those two, the export replays it into pixels. Every
   repair is reversible by clearing one op.
2. **Nothing is repaired that the user has not seen.** The detector's
   output is a *proposal*. Pixels change only after an explicit repair
   command, and only inside masks the user had the chance to reject.
3. **The gates err toward missing crud, never toward eating detail.** A
   missed speck costs a manual spot; a false positive silently destroys
   real information in a scan the user may never re-make. Wherever a
   threshold could go either way, it goes conservative.

This plan follows the conventions of `docs/COLOR_PLAN.md` and
`docs/CAST_REMOVAL_PLAN.md`: numbered chunks, each independently green,
every constant in exactly one module, every calibration named and justified
where it lives.

---

## 0. Why this shape

### 0.1 What this plan does not do

Not in scope, and not to be "finished" by an implementer:

- **Infrared cleaning (Digital ICE, iSRD, VueScan's Infrared Clean).** The
  whole family needs a second capture under IR illumination, which a
  copy-stand rig with an unmodified Z f cannot make. It also fails on
  silver-halide black-and-white film by construction, so it would not cover
  this program's monochrome rolls even with the hardware.
- **Polarized dark-field capture.** The strongest non-IR method in the
  literature (crossed polarizers on light source and lens, so the silver
  image is suppressed and only the depolarizing surface defects survive;
  EPFL/HP's "DUST BW" work, four cardinal illumination angles plus PCA).
  It would turn this feature from a heuristic into a measurement, and it is
  the right escalation if §2's detector proves too blunt — but it needs a
  second exposure per frame and two polarizers in the rig. Note it in
  `docs/punchlist.md`; do not build it here.
- **Learned inpainting (GAN or diffusion).** It reconstructs plausible
  detail rather than interpolating measured detail, which is precisely the
  failure mode principle 3 exists to prevent. Not on an archival scan.
- **A Hessian/Frangi ridge filter for scratches.** §2.4's shape gates get
  hairs and scratches out of the same top-hat response the blobs come from,
  with no second filter bank and no eigen-decomposition of a 40-megapixel
  Hessian. If measurement in Chunk S-6 shows thin scratches are being
  missed, that is the escalation — punchlist, not here.
- **Multi-frame consensus across a negative's overlapping scan frames.**
  Tempting, and wrong: crud stuck to the emulsion is imaged by *every*
  frame that covers that patch of film, so it agrees between frames and is
  invisible to an outlier test. What multi-frame consensus *would* catch is
  defects fixed in the optical path (sensor dust, a mote on a lens
  element), which move relative to film content between frames. That is a
  real and separate feature — its natural home is the flat-field reference
  capture, which already photographs the bare light source with no negative
  in the holder — and it is not this plan's problem. Punchlist.
- **Changing what the detector proposes based on the roll's other
  negatives.** Each negative is detected on its own evidence.

### 0.2 What the prior art says, and what survives contact with this rig

Three findings from the literature shaped §2, and each is load-bearing:

- **Detection and repair are separate problems** (Bergman, Maurer,
  Nachlieli, Ruckenstein, Chase & Greig, *Comprehensive Solutions for
  Removal of Dust and Scratches from Images*, J. Electronic Imaging 17(1),
  2008 — the paper behind a generation of shipped scanner software). Every
  serious system builds a defect mask first and inpaints second. Fusing
  them is what makes Photoshop's Dust & Scratches filter a blur.
- **Small-feature extraction is a morphological top-hat**, not a
  brightness threshold: the white top-hat (image minus its opening) and
  black top-hat (closing minus image) isolate exactly the features smaller
  than the structuring element, in either polarity, and the SE size *is*
  the size gate. This is the standard tool for small-defect detection in
  industrial inspection.
- **Shape, not intensity, is what separates crud from content.** Dust and
  water spots are compact; hairs and scratches are thin and elongated.
  Real image detail is generally neither, at these sizes.

To that this rig adds one cue the literature mostly cannot use, because it
depends on knowing the encoding: **crud is neutral** (§0.3). It is the
single strongest false-positive filter available here, and it is why a
colour roll can be detected more aggressively than a monochrome one.

### 0.3 The physics, in this program's coordinates

`normalization.py` fixes the polarity: `val = (D_log - floor_ch) / (ceil_ch
- floor_ch)`, `floor` the *low* log percentile (dense film, scene
highlight) mapping to `0.0`, `ceil` the high one (thin film, base, scene
shadow) mapping to `1.0`. So **`val` rises with the light that reached the
sensor**. Two consequences, and both are the detector's whole basis:

- **Something on the film that blocks or scatters light out of the
  collection cone** — dust, a hair, dried mineral residue from a water
  spot, a base-side scuff — reduces the transmitted light, so `val` *dips*
  toward the dense end. The negative reads locally denser; the print reads
  locally *white*. This plan calls that polarity **`dense`**.
- **Something that removes emulsion** — a scratch through the dye or
  silver — passes more light, so `val` *rises* toward the thin end. The
  print reads locally *black*. This plan calls that polarity **`thin`**.

Both are detected. The names are the ones the rest of the codebase already
uses for the two ends of the density axis; do not rename them to
"dark"/"light", which is ambiguous between the negative and positive views.

**Why crud is neutral, and the correction that makes the test exact.** A
speck of dust is a broadband attenuator: it multiplies the transmitted
light by roughly the same factor in every channel, which in log density is
the *same additive offset* in all three. But each channel is then
normalized by its own span, so an equal log offset arrives in `val` space
scaled by `1 / (ceil_ch - floor_ch)` — unequal across channels. The
neutrality test is therefore not "are the three `val` residuals equal" but
"are the three residuals equal *after multiplying each by its channel's
span*". Those spans are already computed and already robust to a missing
record: `color.read_metering(negative.normalization).ranges` returns
exactly `|ceil_ch - floor_ch|` per channel, falling back to `(1, 1, 1)`
when the record is absent. Use it; do not re-derive it.

Real image content, at these sizes, is rarely neutral in that sense. That
asymmetry is what §2.5 spends.

### 0.4 Two pixel classes the detector must never look at

- **Fill.** `largest_valid_rect` "restricts the meters only — it never
  crops the output" (`stitch_pipeline.py`, section 1.5), so a published
  TIFF is the full canvas with uncovered pixels at the fill sentinel.
  `NORMALIZED_FILL = 1.10` encodes to exactly **code 65535** (verified;
  `val = 0` is code 7864 and `val = 1` is code 60292). Uncovered canvas is
  therefore a plateau above every real value, and the `thin` top-hat fires
  in a band one SE radius wide along the whole fill side of the boundary —
  which is why §2.1 erodes the analysis mask by that radius rather than
  merely excluding the fill pixels themselves.
- **Everything outside `valid_rect`.** The negative record already carries
  it, in canvas pixels, and the normalization meters already restrict
  themselves to it. Detection restricts itself the same way, for the same
  reason and by the same rule.

Both exclusions are applied as a single boolean `analysis` mask in §2.1.

### 0.5 Why the top-hat and not a median residual

The obvious detector is "pixel minus local median". It is not available
cheaply here: `cv2.medianBlur` refuses any kernel above 5 on non-8-bit
input (measured on the pinned OpenCV 4.14 — `k=9` on `uint16` raises), and
the published TIFF is `uint16`. `scipy.ndimage.median_filter` has no such
limit but is far slower at the kernel sizes §2 needs.

`cv2.morphologyEx` has neither problem: it is `uint16`-native, handles
3-channel input directly, and is fast enough at full scan size. Measured on
a 5000×8000×3 `uint16` array, both polarities together: **0.86 s** at SE
radius 12, **2.30 s** at radius 20. Connected components over the resulting
40-megapixel mask: 0.03 s. A subsampled median for the threshold statistic:
0.04 s. The whole detector is a few seconds per negative — an on-demand
command, not a pipeline stage.

### 0.6 Where the work lands, and what it costs

Peak memory is roughly **20 bytes per canvas pixel** while detecting: the
source (6 B/px), the per-channel top-hat response (6 B/px), the reduced
channel-mean response (2 B/px), the threshold mask (1 B/px), and
`connectedComponentsWithStats`' `int32` labels (4 B/px). At 40 megapixels
that is ~800 MB. This is a single-negative command in its own process with
nothing else live, so it needs no entry in `estimate_peak_bytes` — but do
not "optimize" it by detecting a whole selection concurrently.

### 0.7 The constants here are seeded, not measured

Every threshold in §2.7 is a starting value chosen from the physics and the
literature, not from this rig's scans. **Chunk S-6 is not optional.** Until
it runs, the feature is honest but untuned, and the plan says so in the
same breath as it names the numbers.

---

## 1. The data model

### 1.1 The `spots` op

One new op joins the ops log, `SPOTS_OP = "spots"` in `library/repo.py`. It
is a **state** op, not a transform: the latest one wins, and
`_coalesce_state_edit` updates a trailing `spots` op in place, exactly as
`tone` and `color` do. Its params:

```json
{
  "detector_version": 1,
  "sensitivity": 0.5,
  "repair": false,
  "canvas": [7412, 4988],
  "spots": [
    {
      "id": 1,
      "kind": "blob",
      "polarity": "dense",
      "bbox": [4211, 1880, 5, 4],
      "rle": [1, 3, 1, 10, 1, 3, 1],
      "area": 16,
      "score": 9.4,
      "rejected": false
    }
  ]
}
```

- `bbox` is `[x, y, width, height]` in **published-TIFF pixels** (§1.2).
- `rle` is the component's exact mask (§1.3). The example above is a real
  one, small enough to use as a fixture: a 5×4 box holding a plus shape,
  runs alternating from zero, summing to `5 * 4 = 20`, with the ones
  summing to `area`.
- `score` is the component's peak response in robust sigmas (§2.6).
- `rejected` is the review state. **Absent means accepted**: the user
  rejects, never accepts, which is the interaction this plan is built
  around.
- `repair` is the whole-negative switch. `false` means "markers only, no
  pixel anywhere is changed". This is the state the negative is in
  immediately after detection.
- `canvas` is `[width, height]` of the published TIFF the spots were
  detected on. See §1.5 — it is not decoration.

This op **reaches the export** — `previews.py` and `exporter.py` both apply
it (§3.3) — and it is the first op that *synthesizes* pixel values rather
than moving or remapping measured ones. Every op before it either moved
pixels around (`rotate`, `flip`, `rotate_fine`), or pushed every pixel
through a curve the export bakes in (`tone`), or stayed in the preview
entirely (`color`). A repaired pixel is the only pixel in the program that
was never measured, which is why principles 2 and 3 exist.

While you are in `edits.py`, fix its module docstring: it claims "`tone`
and `color` are preview-only judgement aids, so the exporter ignores them",
but `exporter._export_negative` passes `state.tone` to
`render.render_export`, which bakes the curve in (`docs/EXPORT_PLAN.md`
§4). Only `color` is preview-only. The docstring predates the colour-managed
export and has been wrong since.

### 1.2 Coordinates: TIFF space is stored, display space is served

A spot must survive a later rotation, so **the op stores TIFF-space
geometry** — the published TIFF's own pixel grid, before any mirror,
fine rotation or quarter turn. Nothing in the ops log may store display
space, which changes whenever a geometric op is appended.

The app, however, draws markers over a *display-space* image, and Swift
does no image processing and no coordinate math (`README.md`: "Swift only
displays the file it is told to"). So the CLI converts: every command and
every query reports spots in **display space**, already transformed, and
the app draws the rectangles it is handed. Rejection is by `id`, never by
coordinate, so the app never converts anything in either direction.

The forward map is the exact inverse of `previews._display_point_to_tiff`
and must be written next to it, as a new public function in `previews.py`:

```python
def tiff_rect_to_display(
    rect: tuple[int, int, int, int],
    tiff_size: tuple[int, int],       # (height, width)
    *,
    quarter_turns: int,
    flipped_horizontally: bool,
    fine_angle_deg: float,
) -> tuple[int, int, int, int]:
```

It replays `_display_image`'s canonical order on the rect's four corners —
mirror, then the fine rotation, then the quarter turns — takes the
axis-aligned bounding box of the four mapped corners (floor the minimum,
ceil the maximum), and clamps to the display bounds. Concretely, for a TIFF
of shape `(H, W)` and a point `(x, y)`:

1. **Mirror**, when flipped: `x ← W - 1 - x`.
2. **Fine rotation**: `M = cv2.getRotationMatrix2D((W / 2, H / 2),
   -fine_angle_deg, 1.0)` — the same matrix `auto_rotate.rotate_with_fill`
   builds, negated angle and all — then `(x, y) ← M @ (x, y, 1)`.
3. **Quarter turns**, with `r = (-quarter_turns) % 4`:
   - `r == 0`: unchanged; display size `(W, H)`.
   - `r == 1`: `(x, y) ← (y, W - 1 - x)`; display size `(H, W)`.
   - `r == 2`: `(x, y) ← (W - 1 - x, H - 1 - y)`; display size `(W, H)`.
   - `r == 3`: `(x, y) ← (H - 1 - y, x)`; display size `(H, W)`.

Step 3 is the algebraic inverse of the case table in
`_display_point_to_tiff`'s docstring; a test pins the round trip.

An axis-aligned box under a fine rotation grows slightly — the marker
rectangle is a little larger than the defect. That is correct behaviour for
a review marker and is not a bug to fix.

### 1.3 The RLE mask

The repair must not touch a pixel that is not part of the defect. A hair's
bounding box is mostly clean film; repairing the box would destroy real
information to remove a defect occupying a tenth of it. So each spot
carries its **exact component mask**, run-length encoded over its own
bounding box:

- Row-major over the `width × height` box, **alternating run lengths
  starting with a run of zeros** (which may itself be `0`).
- The runs sum to exactly `width * height`.
- Decoding is one line: `np.repeat(np.arange(len(rle)) % 2, rle).reshape(h,
  w).astype(bool)`.

Only Python ever reads it. The app is handed the bbox and draws a
rectangle; it never sees, decodes or renders a mask.

Size is not a concern in practice — a compact blob is a handful of runs,
and a 400×60 hair is a few hundred — but §1.4's cap bounds it absolutely.

### 1.4 Bounds

`MAX_SPOTS = 500` per negative. A mis-set threshold on a grainy negative
could otherwise propose a hundred thousand "spots", writing a
multi-megabyte op and freezing the review UI. When the detector finds more,
it keeps the **highest-scoring** `MAX_SPOTS` and emits one
`SPOT_LIMIT_REACHED` warning naming the count found and the count kept. The
user's remedy is to lower the sensitivity, and the warning says so.

### 1.5 A re-stitch invalidates a spot set, and must be caught

A negative keeps its `negative_id` — and therefore its whole ops log —
across a re-stitch (`repo.py`: "re-stitching a negative — which keeps its
id — keeps its edit history"). But a re-stitch solves a fresh layout, so
the canvas can be a different size and the content sits at different pixel
coordinates. TIFF-space spot geometry recorded against the old canvas would
then point at nothing, and repairing it would inpaint arbitrary parts of
the new image. This is the one way this feature could damage a scan without
the user doing anything wrong, so it is closed structurally:

- The op records `canvas`, the dimensions it was detected against.
- `spots.apply_repair(image, params)` and `spots.is_repairing(params,
  shape)` both **no-op on a mismatch**. A stale set repairs nothing, ever.
- `edits._spots_for_report` reports an **empty list** on a mismatch, so no
  markers are drawn over pixels they do not describe, and `edit list-spots`
  emits one `SPOTS_STALE` warning saying the negative was re-stitched and
  needs re-detecting.
- `roll info`'s summary reports `"stale": true` with zeroed counts. It
  already has the dimensions to compare against —
  `negative.output["width"/"height"]` — so this costs no decode.

Re-running `detect-spots` clears the condition, because it records a fresh
`canvas` with fresh geometry.

---

## 2. The detector — `cli/src/scanny_boy/spots.py`

A new leaf module. It imports `cv2`, `numpy`, and `normalization` (for the
code/val conversion) and **nothing else from this program** — no `repo`, no
`previews`, no manifest types. Its two public functions are pure:

```python
def detect(
    image: np.ndarray,                    # uint16, (H, W) or (H, W, 3)
    *,
    valid_rect: tuple[int, int, int, int] | None,
    channel_ranges: tuple[float, ...],    # color.read_metering(...).ranges
    sensitivity: float = DEFAULT_SENSITIVITY,
) -> DetectionResult: ...

def apply_repair(
    image: np.ndarray,                    # uint16, (H, W) or (H, W, 3)
    params: dict | None,                  # the net `spots` op's params
) -> np.ndarray: ...
```

`DetectionResult` is a frozen dataclass: `spots: list[dict]` (the op's spot
entries, ids not yet assigned), `found: int` (before the §1.4 cap), and
`sigma: float` (the measured robust scale, recorded for Chunk S-6).

### 2.1 Stage 0 — the analysis mask

```python
analysis = np.ones((H, W), dtype=bool)
if valid_rect is not None:
    analysis &= _inside(valid_rect)                       # §0.4
fill = image == FILL_CODE
analysis &= ~(fill if image.ndim == 2 else fill.all(axis=2))
analysis = cv2.erode(analysis.view(np.uint8), se).astype(bool)
```

The mono (2-D) and colour (3-D) cases differ only in that reduction; every
other stage is shape-agnostic. Note the erosion is by the **same `se`**
§2.2 builds, so the analysis mask and the filter always agree about what
"one SE radius from the edge" means.

`FILL_CODE = 65535`, derived once at module import as
`int(normalization.encode_normalized(np.full((1, 1, 1),
normalization.NORMALIZED_FILL, dtype=np.float32)).ravel()[0])` — never
written as a literal. The erosion is what keeps a component from straddling
the fill boundary and taking a step edge for signal (§0.4).

### 2.2 Stage 1 — the top-hat response

Work in **code space** throughout; convert to `val` units only for the
final score and the neutrality gate, via the single derived constant

```python
_CODE_TO_VAL = (
    1.0 + normalization.NORMALIZED_HEADROOM_LOW + normalization.NORMALIZED_HEADROOM_HIGH
) / 65535.0
```

The structuring element is an ellipse of radius `se_radius_px` (§2.7),
built once with `cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2r+1,
2r+1))`. For each polarity:

- `dense`: `response_ch = cv2.morphologyEx(image, cv2.MORPH_BLACKHAT, se)`
  — large where the image dips below its surroundings.
- `thin`: `response_ch = cv2.morphologyEx(image, cv2.MORPH_TOPHAT, se)` —
  large where it rises above them.

Both are computed on the **full multi-channel array** in one call, so
`response_ch` keeps a value per channel — that is what §2.5's neutrality
gate reads. The detection signal `response` is the **mean of `response_ch`
across channels** (better signal-to-noise than any single channel, and
unbiased, unlike a min or a max); on a monochrome roll's 2-D TIFF the two
are the same array. The two names are used consistently below.

A disk structuring element extracts any feature narrower than its diameter
in *every* direction — which is why one filter finds both compact dust and
arbitrarily long hairs, as long as the hair is thin. The SE size is the
width gate; §2.4's gates are the rest.

### 2.3 Stage 2 — the robust threshold

The response is one-sided and non-negative: grain fills the bulk, defects
are the extreme tail. The scale is measured from the negative's own grain,
so a grainy stock automatically raises its own bar — which is the
"conservative where uncertain" principle made numerical.

Over the analysis mask, subsampled with `STAT_STRIDE = 4` in each axis
(measured at 0.04 s; a robust statistic does not need every pixel):

```python
s       = slice(None, None, STAT_STRIDE)
samples = response[s, s][analysis[s, s]].astype(np.float32)
med     = np.median(samples)
sigma   = max(1.4826 * np.median(np.abs(samples - med)), SIGMA_FLOOR)
k       = THRESHOLD_K_MAX - sensitivity * (THRESHOLD_K_MAX - THRESHOLD_K_MIN)
if image.ndim == 2:
    k += MONO_K_BONUS                             # §2.5: one gate fewer
mask    = (response > med + k * sigma) & analysis
```

Each polarity is measured and thresholded **independently, end to end** —
its own `med`, its own `sigma`, its own mask, its own components — and the
two sets are merged only at §2.6's capping step.

`sensitivity` runs `0.0` (most conservative) to `1.0` (most aggressive).
`detect` raises `ValueError` outside that range rather than clamping — a
leaf module with a strict contract — and `cli.py` range-checks the flag
first, so the user sees `INVALID_EDIT`, never a traceback. `SIGMA_FLOOR`
exists so a synthetic fixture with zero noise does not divide the world by
zero — it is a numerical guard, not a measured threshold, and must be
commented as such.

### 2.4 Stage 3 — the shape gates

`cv2.connectedComponentsWithStats(mask, 8)` on each polarity's mask. For
each component, from `stats` alone:

```
major = max(bbox_w, bbox_h)
minor = area / major                    # mean width along the major axis
elongation = major / max(minor, 1.0)
kind = "streak" if elongation >= STREAK_ELONGATION else "blob"
```

Keep the component only if **all** of these hold:

- `area >= MIN_SPOT_AREA_PX` — below this it is grain or a hot pixel.
- `minor <= max_minor_px` — the width gate, the one that matters most.
- `blob`: `major <= 2 * max_minor_px`. A blob wider than twice the width
  gate is image content that happened to clear the threshold.
- `streak`: `major <= MAX_STREAK_LENGTH_FRACTION * long_edge`. A power
  line, a horizon, a branch against sky and a hair are all thin, dark and
  long; length is the only cheap thing that separates them, and a hair
  spanning a quarter of the frame is rarer than a wire that does.

`minor` from `area / major` is exact for an axis-aligned component and
**overestimates by up to √2 for a diagonal one**, so a diagonal hair is
gated more strictly than a horizontal one of the same width. That is
accepted: the error direction is toward rejecting, which costs a manual
spot and never costs a pixel. Do not "fix" it with a PCA that would make
the gate looser.

### 2.5 Stage 4 — the neutrality gate (colour rolls only)

For each surviving component, take the mean per-channel response inside the
component mask, convert each to log-density units by its own channel span
(§0.3), and require agreement:

```
d_c    = mean(response_ch[..., c][component]) * _CODE_TO_VAL * channel_ranges[c]
spread = max(d) - min(d)
keep   = spread <= NEUTRALITY_TOLERANCE * mean(d)
```

This is the single strongest false-positive filter available, and it is
unavailable on a monochrome roll — one channel has nothing to agree with.
A mono roll therefore runs §2.3's threshold with `MONO_K_BONUS` added to
`k`, which is not a fudge factor but the honest statement that with one
fewer gate the remaining one must be stricter. Both facts belong in the
module docstring.

### 2.6 Scoring, capping, and stable ids

`score = (max(response[component]) - med) / sigma` — the component's peak
height in robust sigmas, on the channel-mean response, with `med` and
`sigma` the same two numbers §2.3 measured **for that spot's own
polarity**. Scores from the two polarities are directly comparable because
both are in units of their own noise, which is what makes the next step
legitimate:

1. Merge both polarities' survivors, sort by `score` descending, and
   truncate to `MAX_SPOTS` (§1.4).
2. Re-sort the survivors by `(bbox_y, bbox_x)` and assign `id` `1..N` in
   that raster order, so the numbering the user sees runs top-left to
   bottom-right and is stable for a given detection.

**Re-detection preserves rejections.** `detect()` is pure and knows nothing
about the previous op, so the carry-forward lives in `edits.py`: for each
newly proposed spot, if any *previously rejected* spot's bbox centre lies
within `REJECTION_MATCH_PX` of the new one's, the new spot is born
`rejected: true`. Without this, changing the sensitivity slider throws away
every judgement the user has made, and the tedium this feature exists to
remove comes straight back.

### 2.7 The constants, in one place

All of these live at the top of `spots.py` and nowhere else. Each carries a
comment saying what it does **and that it is seeded pending Chunk S-6**.

| Constant | Seed | What it controls |
| --- | --- | --- |
| `DETECTOR_VERSION` | `1` | Stamped into every op; §5's parser gates on it |
| `MAX_SPOT_MINOR_FRACTION` | `0.004` | Max defect width as a fraction of the canvas **short** edge (≈20 px on a 5000 px edge) |
| `MIN_SPOT_AREA_PX` | `6` | Floor, in pixels; below it is grain |
| `STREAK_ELONGATION` | `4.0` | `blob` vs `streak` |
| `MAX_STREAK_LENGTH_FRACTION` | `0.25` | Max streak length as a fraction of the canvas **long** edge |
| `THRESHOLD_K_MIN` | `4.0` | `k` at sensitivity 1.0 |
| `THRESHOLD_K_MAX` | `12.0` | `k` at sensitivity 0.0 |
| `DEFAULT_SENSITIVITY` | `0.5` | ⇒ `k = 8.0` |
| `MONO_K_BONUS` | `2.0` | Added to `k` when there is no neutrality gate |
| `NEUTRALITY_TOLERANCE` | `0.35` | Max relative channel spread (§2.5) |
| `SIGMA_FLOOR` | `1e-4 / _CODE_TO_VAL` | Numerical guard, not a measurement |
| `STAT_STRIDE` | `4` | Subsampling for the median/MAD |
| `MAX_SPOTS` | `500` | Per negative (§1.4) |
| `REJECTION_MATCH_PX` | `8` | Rejection carry-forward radius (§2.6) |
| `REPAIR_DILATE_PX` | `2` | Mask growth before inpainting (§3.1) |
| `INPAINT_RADIUS_PX` | `3` | `cv2.inpaint`'s neighbourhood radius |

`se_radius_px` and `max_minor_px` are **derived per image**, not constants:
`max_minor_px = MAX_SPOT_MINOR_FRACTION * min(H, W)` and `se_radius_px =
max(3, ceil(max_minor_px))`. A disk of radius `r` removes features narrower
than `2r`, so an SE radius equal to the full width gate leaves comfortable
margin.

---

## 3. The repair

### 3.1 The mask

Union the decoded RLE masks of every spot with `rejected` falsy, placed at
its bbox, then dilate by `REPAIR_DILATE_PX` with an ellipse. The dilation
is not padding for its own sake: the detected mask is the set of pixels
that *cleared the threshold*, and a defect's penumbra falls below it —
inpainting the threshold set alone leaves a visible halo of the defect's
own edge.

Pixels at the fill sentinel are removed from the repair mask before
inpainting, so a spot that somehow reaches uncovered canvas cannot smear
fill into the image.

### 3.2 The inpaint

`cv2.inpaint(..., INPAINT_RADIUS_PX, cv2.INPAINT_TELEA)`, **once per
channel** on `uint16` 2-D planes.

The per-channel loop is not a style choice. Measured on the pinned OpenCV
4.14: `inpaint` accepts `uint16` single-channel and `float32`
single-channel, and **refuses `uint16` three-channel** (it accepts only
8-bit for 3-channel input). Converting to 8-bit to get one call would throw
away half the published file's bit depth in exactly the pixels being
repaired. Three `uint16` calls cost 0.21 s each at 40 megapixels — measured
— so there is nothing to buy by being clever.

Channels are inpainted independently, which can in principle tint a repair
patch. At these sizes, against the alternative of an 8-bit round trip, that
is the better trade; note it in the docstring.

Telea's diffusion fills the hole smoothly and does not synthesize grain, so
a large repair reads as a smooth patch at 100%. That is a *feature* under
principle 3 — it is visibly an interpolation rather than invented texture —
and it is the reason §2.7's width gate is tight. A patch-based fill that
matches grain is the punchlist escalation.

### 3.3 Where it lands in the render: one function, two callers

`apply_repair` is called on the published TIFF's array **before any
geometry**, because the op's coordinates are TIFF space. Exactly two
callers, and no others:

1. `previews._display_image` — gains a `spots_params: dict | None = None`
   keyword and applies the repair as its first step, before the mirror.
   That one insertion covers `generate_preview`, `render_preview`, and
   `render_region`'s exact path, because all three already route through
   it. `render_region`'s strip-level *fast* path never repairs; §3.4 keeps
   it out of that path entirely rather than teaching it to.
2. `exporter._export_negative` — between `tifffile.imread(tiff_path)` and
   `apply_edits(...)`.

The state reaches all three the same way: `repo.EditState` gains a
`spots: dict | None` field, and every caller already fetches
`repo.net_edit_state`. Thread `state.spots` through the existing
`quarter_turns` / `flipped_horizontally` / `tone_params` parameter lists;
add no new lookup path.

**The repair applies in both display modes.** The negative (density) view
shows repaired densities too. One rule, no exceptions: what the user is
comparing when they toggle repair on and off is the same in both views.

### 3.4 `render_region` takes the exact path when a repair is live

`render_region` has a fast path that decodes only the TIFF strips
overlapping the requested rect, and `test_render_region_matches_full_decode`
pins it as bit-identical to a full decode. Inpainting a crop uses different
surroundings than inpainting the whole image, so repairing inside the strip
path would break that equivalence near crop boundaries.

So it does not: extend the existing guard

```python
if abs(fine_angle_deg) >= 1e-9:
```

to

```python
if abs(fine_angle_deg) >= 1e-9 or spots.is_repairing(
    spots_params, (tiff_h, tiff_w)
):
```

and let the exact path (full decode, full replay, slice) handle it — the
mechanism already there for the fine rotation, used for one more reason.
`render_region` already reads `tiff_h, tiff_w` from the header before this
guard, so the shape is in hand.

This is close to free in practice: the stitch stage auto-seeds a
`rotate_fine` op on essentially every negative, so the exact path is
already the common case at 100% zoom.

`spots.is_repairing(params, shape)` is a tiny public predicate in
`spots.py`: true when `params` is not `None`, `params["repair"]` is set,
the recorded `canvas` matches `shape` (§1.5), and at least one spot is not
rejected. `apply_repair` calls it first and returns the image untouched
when it is false, so the guard and the repair can never disagree.

---

## 4. Chunk S-1 — `spots.py`: the detector and the repair

`cli/src/scanny_boy/spots.py` and `spots_test.py` only. No other file
changes. Nothing calls it yet.

### Changes

Everything in §2 and §3.1–3.2: the constants, `detect`, `apply_repair`,
`is_repairing`, the RLE encode/decode helpers, and the module docstring
stating §0.3's polarity convention and §0.7's "seeded, not measured".

### Tests (`spots_test.py`)

Build fixtures as synthetic normalized-density arrays; no RAW, no TIFF, so
this whole chunk is fast-tier.

- **A planted dark disk is found.** A flat mid-density field plus one
  8-px-radius disk pushed toward the dense end → exactly one spot,
  `kind == "blob"`, `polarity == "dense"`, bbox containing the disk.
- **A planted bright disk is found** with `polarity == "thin"`.
- **A planted hair is found**: a 3-px-wide, 300-px-long line →
  `kind == "streak"`, and its RLE decodes to a mask covering the line and
  not the box.
- **Grain alone finds nothing.** Gaussian noise at a realistic sigma, no
  defect, default sensitivity → zero spots. Run it at several noise levels;
  the count stays zero because the threshold is measured from the noise.
- **The neutrality gate rejects a coloured feature.** The same disk planted
  in one channel only → rejected. Planted equally in all three (after the
  span correction of §0.3) → kept.
- **Channel spans are honoured**: a disk planted as an equal *log* offset
  with deliberately unequal `channel_ranges` is kept; one planted as an
  equal *val* offset with the same unequal ranges is rejected.
- **Fill and `valid_rect` are excluded.** A defect planted outside
  `valid_rect`, and a fill-sentinel region abutting real content, both
  yield zero spots.
- **The size gates bite**: a disk larger than `2 * max_minor_px` is
  rejected; a streak longer than `MAX_STREAK_LENGTH_FRACTION` is rejected;
  a 3-pixel component is rejected by `MIN_SPOT_AREA_PX`.
- **Sensitivity is monotone**: the spot count never decreases as
  sensitivity rises.
- **The cap holds**: plant 600 disks → 500 spots, `found == 600`, ids
  `1..500` in raster order.
- **RLE round-trips** for a random boolean mask, and the runs sum to
  `w * h`.
- **`apply_repair` is a no-op** when `params is None`, when `repair` is
  false, and when every spot is rejected — assert array equality with the
  input in all three.
- **`apply_repair` touches only the dilated mask.** Assert bit-exact
  equality outside it, and that the planted disk's pixels moved toward
  their surroundings inside it.
- **`apply_repair` handles a 2-D (mono) image** and a 3-D one.
- **A canvas mismatch repairs nothing (§1.5).** Record a spot set against
  one shape, call `apply_repair` with an image of another → the array comes
  back untouched, and `is_repairing` is false. This is the test that stops
  a re-stitch from inpainting arbitrary pixels; do not weaken it.
- **A repaired negative re-detects clean**: on the single-planted-disk
  fixture, detect → repair → detect again at the same sensitivity finds no
  spot at the repaired location.

---

## 5. Chunk S-2 — the ops log (`library/repo.py`)

### Changes

- `SPOTS_OP = "spots"`, with a module comment in the style of `TONE_OP`'s
  and `COLOR_OP`'s: a state op, coalesced in place, **and the only op whose
  replay synthesizes pixel values** (§1.1).
- `EditState` gains `spots: dict | None`.
- `_parse_spots_op(params) -> dict | None`, following `_parse_tone_op`'s
  discipline — it never raises, and anything malformed degrades to `None`,
  which means *no markers and no repair*. Degrading toward "change no
  pixels" is the only safe direction.

  Validate: `detector_version` an int and `<= spots.DETECTOR_VERSION`;
  `repair` a bool; `sensitivity` a float in `[0, 1]`; `canvas` two positive
  ints; `spots` a list; each entry carrying the core keys `id`, `kind`,
  `polarity`, `bbox`, `rle` (with the runs summing to
  `bbox[2] * bbox[3]`). The parser cannot check `canvas` against a real
  image — it has none — so that comparison stays in `spots.py` (§1.5).
  **Ignore unknown keys**
  rather than rejecting them — `docs/CAST_REMOVAL_PLAN.md` §6.1 is the
  cautionary tale about a parser that demands an exact key set and
  silently discards real user state the day the set grows.
- `append_spots_edit(roll_dir, negative_id, params)` — validates through
  `validated_spots_params` and routes to the existing
  `_coalesce_state_edit`.
- `validated_spots_params(params)` raises `ValueError` with a specific
  message for each malformed shape; `edits.py` turns those into
  `INVALID_EDIT`.
- `net_edit_state` learns the `SPOTS_OP` branch: `spots = parsed` (latest
  wins), exactly like `tone` and `color`.

### Tests (`repo_test.py`)

- A `spots` op round-trips through append → `net_edit_state`.
- A trailing `spots` op is coalesced in place (position does not grow).
- A `spots` op with an unknown extra key on a spot parses fine and keeps
  every core value.
- Each malformed shape — bad `detector_version`, RLE that does not sum to
  the bbox area, missing `bbox`, `sensitivity` out of range — degrades to
  `None`, and `validated_spots_params` raises for it.
- A `spots` op followed by a `rotate` op leaves `state.spots` untouched
  (TIFF-space coordinates do not move when the display transform does).
- A future `detector_version` degrades to `None` rather than being applied.

---

## 6. Chunk S-3 — rendering (`previews.py`, `exporter.py`)

### Changes

- `previews.tiff_rect_to_display` (§1.2), beside `_display_point_to_tiff`.
- `previews._display_image` gains `spots_params` and applies the repair
  first.
- `generate_preview`, `render_preview`, `render_region` each gain
  `spots_params` and pass it down; `render_region`'s exact-path guard grows
  the `spots.is_repairing(...)` clause (§3.4).
- `previews._STATE_PREVIEW_OPS` gains `repo.SPOTS_OP`, so `ensure_preview`
  **regenerates** rather than transforming the cached PNG — a repair
  changes pixels, and the incremental transform path is lossless-geometry
  only.
- Every `repo.net_edit_state(...)` call site in `previews.py` (there are
  several — `ensure_preview` twice, `sync_previews`) passes `state.spots`
  through.
- `exporter._export_negative` applies the repair between `imread` and
  `apply_edits`.
- `exporter.provenance_record` records what the export actually did: a
  `"spots"` key under `"rendered"`, holding
  `{"detector_version", "sensitivity", "repaired": <count>}` — the count of
  masks actually applied, `null` when none. The XMP is the file's
  interpretability record, and "some pixels here are interpolated" is
  exactly the kind of thing it exists to say.

### Tests (`previews_test.py`, `exporter_test.py`, `render_test.py`)

- `tiff_rect_to_display` composed with `_display_point_to_tiff` is the
  identity on corner points, for all four quarter turns × flipped/not, at
  zero fine angle.
- A 90° rotation moves a spot's display rect to the rotated location —
  pinned against a hand-computed expectation, not against the function's
  own output.
- A nonzero fine angle grows the display rect and keeps it containing the
  mapped corners.
- `generate_preview` with `repair: false` is byte-identical to today's
  output; with `repair: true` it differs, and only inside the repaired
  region once scaled.
- `render_region` with a repair live returns the same pixels as a full
  decode + replay + slice (the existing equivalence test, extended).
- `ensure_preview` with a `spots` op regenerates rather than transforming.
- The export applies the repair, and `apply_edits` still sees TIFF-space
  pixels (a rotate plus a repair compose in the right order: repair first,
  then geometry).
- The provenance record names the repair count.

---

## 7. Chunk S-4 — the CLI (`edits.py`, `cli.py`, `events.py`, contract)

### 7.1 Three commands

```
scanny-boy edit detect-spots --roll DIR --negative ID [--negative ID ...] [--sensitivity 0..1]
scanny-boy edit spots        --roll DIR --negative ID [--reject N ...] [--accept N ...]
                                                      [--repair | --no-repair] [--clear]
scanny-boy edit list-spots   --roll DIR --negative ID
```

- `detect-spots` takes a **selection** (`--negative` repeatable, like
  `rotate`), validated up front so a batch either records or fails whole.
  It reads each negative's published TIFF, runs `spots.detect`, carries
  rejections forward (§2.6), and records the op with `repair` **preserved
  from the previous op** — re-detecting on a negative that was already
  repaired keeps it repaired, with the new masks.
- `spots` takes **exactly one** `--negative`: spot ids are per-negative, so
  a selection would be meaningless for `--reject`. `--clear` records an op
  with an empty spot list and `repair: false`. `--reject`/`--accept` are
  repeatable and may be combined; an id not in the current set fails
  `INVALID_EDIT` naming the id.
- `list-spots` is a **pure query** — nothing recorded, no pixels touched,
  in the same family as `render-region` and `render-preview`.

### 7.2 `edits.py`

`run_edit_detect_spots`, `run_edit_spots`, `run_edit_list_spots`, all built
on the existing `_validated_negatives` / `_validated_negative` gates (an
unstitched negative fails `NEGATIVE_NOT_FOUND`, exactly as `edit tone`
does). Each recording command refreshes the preview through
`_refresh_preview(..., repo.SPOTS_OP, ...)` and returns one event field-set
per negative.

The display-space conversion lives here, in one shared helper:

```python
def _spots_for_report(roll_dir, negative, params) -> list[dict]:
    """The op's TIFF-space spots as the app draws them: display-space
    rects, ids unchanged. Swift converts nothing."""
```

It reads the negative's TIFF dimensions from `negative.output`
(`{"width", "height"}` — no pixel decode), the net transform from
`repo.net_edit_state`, and calls `previews.tiff_rect_to_display` per spot.
The `rle` is **never** reported: it is an implementation detail of the
repair and would multiply the payload for nothing.

The rejection carry-forward of §2.6 lives here too, not in `spots.py`,
because it needs the previous op.

`run_edit_detect_spots` writes the op's `canvas` from **the decoded
image's own shape**, not from `negative.output["width"/"height"]`. The two
should agree; taking it from the array means the recorded canvas describes
the pixels actually detected on, even if a record has drifted.

### 7.3 `events.py` and `cli.py`

- `PROTOCOL_VERSION = 13`, with the block comment describing the feature in
  the established style.
- One new event type, `SPOTS_REPORTED = "spots_reported"`, emitted by all
  three commands:

  ```python
  @dataclasses.dataclass(frozen=True, kw_only=True)
  class SpotsReported(Event):
      event_type: ClassVar[EventType] = EventType.SPOTS_REPORTED
      negative_id: str
      detector_version: int
      sensitivity: float
      repair: bool
      spots: list[dict[str, Any]]        # display space, no rle
      found: int                          # before the MAX_SPOTS cap
      preview_path: str | None            # null for `list-spots`
  ```

- Two new warning codes, `SPOT_LIMIT_REACHED` (§1.4) and `SPOTS_STALE`
  (§1.5). No new error codes: every failure here is `INVALID_EDIT`,
  `ROLL_NOT_FOUND` or `NEGATIVE_NOT_FOUND`.
- `cli.py` gains the three subparsers and their dispatch arms, following
  `render-region`'s shape exactly. `--sensitivity` is `type=float` with a
  range check that fails `INVALID_EDIT`.
- `roll info` gains a per-negative **summary**, not the list:

  ```python
  negative["spots"] = None if state.spots is None else {
      "detector_version": ..., "sensitivity": ..., "repair": ...,
      "stale": <canvas != the negative's published dimensions>,   # §1.5
      "count": <total, or 0 when stale>,
      "rejected": <count rejected, or 0 when stale>,
  }
  ```

  The full list is `list-spots`' job. A 36-negative roll with 500 spots
  each would otherwise put megabytes of JSON through every `roll info` —
  and `roll info` is on the path of every roll switch and every metadata
  edit.

### 7.4 Contract and docs

- `shared/contract/CONTRACT.md`: the protocol 13 paragraph at the top, the
  three commands in the `edit` family, the `spots_reported` event, the
  `SPOT_LIMIT_REACHED` code, and the `roll info` summary block. State
  explicitly that reported spots are **display space** and that the app
  never converts coordinates.
- `shared/contract/schema.json` alongside it.
- `README.md`'s Edit-tab paragraph gains a sentence.
- `docs/DECISIONS.md`: the three principles at the head of this plan, and
  §0.1's exclusions with their reasons — particularly that multi-frame
  consensus cannot see emulsion crud, which is the kind of thing that looks
  like an obvious win to a future reader who has not thought it through.

### Tests (`edits_test.py`, `cli_test.py`, `events_test.py`)

- `detect-spots` on a synthetic published TIFF records an op and emits
  `spots_reported` with display-space rects and no `rle` key.
- `detect-spots` twice with a rejection in between carries the rejection
  forward; a spot more than `REJECTION_MATCH_PX` away does not inherit it.
- `detect-spots` preserves `repair` across a re-detect.
- `--reject` on an unknown id fails `INVALID_EDIT` and records nothing.
- `--clear` empties the set and turns repair off.
- `--repair` then `--no-repair` returns the preview to its pre-repair
  bytes.
- `list-spots` records nothing: the ops log's max position is unchanged.
- A spot set whose `canvas` does not match the published TIFF reports an
  empty list plus `SPOTS_STALE`, and `roll info` marks it `stale` with
  zeroed counts (§1.5).
- The `SPOT_LIMIT_REACHED` warning fires with the found/kept counts.
- An unstitched negative fails `NEGATIVE_NOT_FOUND` for all three.
- `roll info` carries the summary, and `null` for a negative with no op.
- `events_test` covers the new event's serialization and the schema.

---

## 8. Chunk S-5 — the Mac app

### 8.1 The value type

`mac/ScannyBoy/Model/NegativeSpots.swift`:

```swift
struct NegativeSpots: Sendable, Hashable {
    struct Spot: Sendable, Hashable, Identifiable {
        let id: Int
        let kind: String        // "blob" | "streak"
        let polarity: String    // "dense" | "thin"
        let rect: CGRect        // display space, straight from the CLI
        let score: Double
        let rejected: Bool
    }
    /// `roll info`'s per-negative block: the counts, without the list.
    struct Summary: Sendable, Hashable {
        let detectorVersion: Int
        let sensitivity: Double
        let repair: Bool
        let stale: Bool         // §1.5: detected against a different canvas
        let count: Int
        let rejected: Int
    }

    let detectorVersion: Int
    let sensitivity: Double
    let repair: Bool
    let spots: [Spot]

    var accepted: [Spot] { spots.filter { !$0.rejected } }
}
```

`RollManifest.Negative` gains a `spotsSummary: NegativeSpots.Summary?`
decoded from `roll info`. Adding a field to that struct
means touching four places, all mechanical, all of which the compiler will
find: the stored property, the explicit `init`, `decodeNegative`, and
`EditModel.applyEditRecorded`'s reconstruction. The full `NegativeSpots`
does **not** go in the manifest — `EditModel` holds it for the displayed
negative only, in a `var spots: NegativeSpots?`, refreshed by `list-spots`
whenever the selection changes.

### 8.2 `EditModel`

Five methods and one busy flag, following `isSettingTone`'s pattern exactly
(`isDetectingSpots`; the view's `.disabled(...)` chains grow one term):

- `detectSpots(_ targets:, sensitivity:)`
- `rejectSpot(_ negative:, id:)` and `acceptSpot(_ negative:, id:)`
- `setRepair(_ negative:, on:)`
- `loadSpots(_ negative:)` — the `list-spots` query, run on selection
  change and after a roll refresh.

Every one of them applies the `spots_reported` payload to `self.spots`
in-memory, the way `applyEditRecorded` does. The three *recording* methods
then call `refresh()`, because the summary in `roll info` has changed;
`loadSpots` does not — it is a pure query, and refreshing after it would
loop.

**The render-generation token must include the spot state**, or the app
will show stale pixels after a repair. `EditModel.renderGeneration(of:)`
and `negativeViewGeneration(of:)` both gain a spots term built from the
summary — `"\(repair)#\(count)#\(rejected)"` is enough to change whenever
the rendered pixels change. Getting this wrong is the most likely bug in
this chunk: the symptom is a repair that "does nothing" because a cached
PNG was reused.

### 8.3 The review UI

In `EditStageView`'s `PreviewPane`:

- A **"Find spots"** toolbar button (SF Symbol `sparkle.magnifyingglass`)
  beside the tone and colour buttons, with a popover carrying the
  sensitivity slider, the counts ("42 found, 6 rejected"), a **Repair**
  toggle, and **Clear**.
- A **marker overlay** drawn over both the fit view and the 100% crop, in
  the same `ZStack` as the image. Display-space rects map to screen the
  same way the image does — through `PreviewZoomModel.fitRect` in fit mode,
  and through `crop.rect` / `crop.displayScale` / `cropScreenOffset` at
  100%. Add the two mapping functions to `PreviewZoomModel` as pure static
  methods so `PreviewZoomModelTests` can pin them.
- Accepted spots draw as a thin stroked rect; rejected ones draw dimmed and
  dashed, so a rejection is visibly *a decision the user made*, not a
  disappearance.
- **Click a marker to toggle its rejection.** Hit-testing is nearest-rect
  within a small slop radius; markers are small, so the slop matters.
  Clicking empty space must keep doing what it does today (space+click
  zoom), so the marker layer only takes a click when the overlay is
  showing and a marker is under the cursor.
- Markers hide entirely while `repair` is on, unless the popover is open —
  the point of turning repair on is to look at the result.

### Tests (`EditModelTests`, `CLIEventTests`, `CLICommandTests`, `PreviewZoomModelTests`)

- `CLICommandTests`: each of the three commands' argument vectors, including
  repeated `--reject`.
- `CLIEventTests`: decoding `spots_reported`, including an empty list and a
  `null` `preview_path`.
- `EditModelTests`: a `spots_reported` event lands in `spots`; rejecting
  updates the local state without a refetch; `renderGeneration` changes
  when `repair` flips and when the rejected count changes.
- `PreviewZoomModelTests`: display-space rect → screen rect in fit mode and
  at 100% with a pan offset, both pinned against hand-computed values.

---

## 9. Chunk S-6 — the calibration

**Do not close this feature without this chunk.** Every number in §2.7 is
seeded from physics and prior art, not from this rig.

### 9.1 The tool

`cli/tools/measure_spot_thresholds.py`, in the style of the existing tools
in that directory. Given a roll folder and a list of negative ids, for each
negative and for `sensitivity` in `0.0 … 1.0` step `0.1`:

- run `spots.detect` and record `found`, the kept count, `sigma`, and the
  score/`minor`/`area` distributions;
- write a **contact sheet**: a PNG per sensitivity, tiling 1:1 crops of the
  36 highest-scoring spots with a 64-px margin, in the positive display
  encode, so precision can be judged by eye rather than asserted;
- write a CSV of every measurement.

### 9.2 The protocol

1. Pick six published negatives spanning the roll's density range and
   subject matter — at least one with sky (large smooth areas, where
   grain-driven false positives show first), one with fine foliage or
   fabric (dense real detail at the detector's own scale), and one known to
   be genuinely dirty.
2. Read the contact sheets from the most aggressive sensitivity downward.
   Choose the highest sensitivity at which **at least 90% of the sampled
   crops are genuine crud**.
3. Set `THRESHOLD_K_MIN` / `THRESHOLD_K_MAX` so that chosen sensitivity
   lands at `DEFAULT_SENSITIVITY`, and record the measured `k` and the
   grain `sigma` range that produced it.
4. Repeat step 2 on a monochrome roll to set `MONO_K_BONUS` honestly. It is
   currently a guess standing in for a missing gate.
5. Measure `MAX_SPOT_MINOR_FRACTION` directly: from the contact sheets,
   take the widest genuine defect found and set the fraction just above it.
   This is the gate that decides what the feature can even see.

### 9.3 What gets written down

The chosen values, the negatives they were measured on, and the observed
precision, in a "Measured constants" section appended to this plan — the
way `docs/STITCH_QUALITY_PLAN.md` records its measured gates. A constant
whose provenance is not written down will be re-guessed by the next person
to touch it.

---

## 10. Work order and what must stay true

**Order:** S-1 → S-2 → S-3 → S-4 → S-5 → S-6. Each is independently green:

- S-1 is a pure module with its own tests and no callers.
- S-2 stores and replays an op nothing renders yet.
- S-3 renders it, testable through the Python API before any CLI exists.
- S-4 exposes it.
- S-5 makes it usable.
- S-6 makes it *right*.

S-1 and S-2 can proceed in parallel; nothing else can.

**Invariants, checked at every chunk boundary:**

1. **The published TIFF is never opened for writing.** Nothing in this plan
   changes a byte of it. If a diff touches `tiff_writer.py` or
   `stitched_tiff.py`, something has gone wrong.
2. **No pixel changes while `repair` is false.** A negative that has been
   detected but not repaired must render byte-identically to one that was
   never detected. There is a test for this; keep it.
3. **Spots are TIFF space in the ops log, display space on the wire.**
   Never the reverse, never both in the same structure.
4. **Malformed state degrades to no repair**, never to a partial one.
5. **The detector never reads outside `valid_rect`, and never reads a fill
   pixel.**
6. **A spot set never touches a canvas it was not detected against**
   (§1.5). Any code path that repairs without checking `canvas` is a bug,
   however convenient.
7. **`spots.py` imports nothing from this program except
   `normalization`.** It is a leaf, and it stays testable without a
   database, a manifest, or a file.

---

## 11. Explicitly out of scope

For `docs/punchlist.md`, each with its attachment point:

- **Patch-based (exemplar / PatchMatch-style) fill** to preserve grain
  across a repair, instead of Telea's smooth diffusion. Attachment point:
  `spots.apply_repair` — the mask side of the feature would not change at
  all. Do this when §9's contact sheets show repairs that read as plastic
  at 100%, and not before.
- **A Hessian/Frangi ridge filter** as a second detection channel for thin
  scratches the top-hat's size gate misses. Attachment point:
  `spots.detect`, as a third response alongside the two top-hats. Do this
  when §9 shows scratches being missed at every sensitivity.
- **Optical-path defect mapping from the flat-field reference capture** —
  sensor dust and lens motes are fixed in camera coordinates and recur on
  every frame, so the bare-light reference already photographs them.
  Attachment point: `flatfield.create_profile`, recording a defect mask on
  the profile. A genuinely different feature from this one (§0.1) and worth
  its own short plan.
- **Polarized dark-field capture** (§0.1) — the measurement that would
  replace the heuristic.
- **Restricting detection to the image frame** rather than the whole
  canvas, once `docs/REBATE_ANCHORING.md` lands a measured rebate geometry.
  Crud on the rebate is harmless either way; this only matters if false
  positives cluster there.
- **Copying a spot set between negatives** — useful if the same holder
  dust recurs across a roll, meaningless if it does not. Wait for evidence.

---

## 12. Implementation status

Chunks S-1 through S-5 are implemented and green (S-1 `spots.py` + tests;
S-2 the `spots` op; S-3 the render path, export path, and provenance; S-4
the three CLI commands, the protocol-13 event and codes, the contract, and
`roll info`'s summary; S-5 the Mac app's review UI). The calibration tool
of §9.1 exists at `cli/tools/measure_spot_thresholds.py`.

**Every constant in §2.7 is still seeded.** §9.2's protocol awaits real
rolls; until it runs, the feature is honest but untuned, and §9.3's
"Measured constants" section is not written yet — do not close this feature
without it.
