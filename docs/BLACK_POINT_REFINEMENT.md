# Black point refinement: keeping the negative carrier out of the meters

`analyze_bounds` sets the dense-end floor from `BASE_LUMA_CLIP`, the 0.01
percentile of the analysis region's log luma. On a stitched canvas that is
about 310 cells out of 3.1 million. Any non-film in the analysis region
larger than that owns the black point outright.

The roll at `Sep-6-2026-at-10-56-PM` is the failure. All three negatives
photograph the negative carrier beyond the film edge, stitching reports
that area as validly covered, and none of the three existing withholding
detectors removes it. The floor lands **1.2–1.4 decades too dense**, the
span roughly doubles, and because the span inflates *unequally* per channel
the previews come out both too dark and too blue:

| negative | shipped span R/G/B | true span R/G/B | display error |
|---|---|---|---|
| `_DSC5280` | 2.59 / 2.63 / 2.73 | 1.17 / 1.35 / 1.51 | ×0.45 / ×0.52 / ×0.55 |
| `_DSC5207` | 2.39 / 2.42 / 2.54 | 0.96 / 1.15 / 1.40 | ×0.40 / ×0.47 / ×0.55 |
| `_DSC5215` | 2.55 / 2.55 / 2.57 | 0.85 / 1.01 / 1.22 | ×0.33 / ×0.40 / ×0.47 |

The preview is a bare `1 - normalized` (`previews._build_display_lut`), so
those factors are exactly what reaches the screen. On `_DSC5280`'s asphalt
— a genuine neutral — B/R goes from a correct 1.26 to 1.55. The cast is not
a separate fault; it is the unequal span inflation.

This plan adds one pass, `withhold_non_film`, that finds the film's own
extent and insets the analysis rect inside it.

It follows the conventions of `docs/DENSITY_PLAN.md`, `docs/MONOCHROME_PLAN.md`
and `docs/CAST_REMOVAL_PLAN.md`: numbered chunks, each independently green;
every constant defined in exactly one module with the measurement that
justifies it; every threshold that shapes published output recorded per
negative in the roll manifest.

---

## 0. Why this shape, and why in this order

### 0.1 The governing principle

**The analysis region does not have to be maximal.** It has to be (a)
entirely film and (b) representative of the scene. Losing 10% of the film
costs a percentile meter measured over three million cells nothing;
admitting 0.01% of non-film destroys it. Every rule below is biased toward
shrinking, and the measurements in §1 confirm the bias is free.

This is the opposite of the disposition `withhold_dense_border` was written
with, and deliberately so. That detector's gates exist to keep it *off*
real scene content, because it withholds a mask and a false positive
silently deletes picture. This pass withholds a rectangle at the region's
edge, where a false positive costs a slice of ordinary film and nothing
else.

### 0.2 Why no density-only rule can work

`withhold_dense_border` is the natural home for this and it cannot be made
to fit. The carrier band on `_DSC5280` fails two of its four gates:

- `_is_thin`: the band's bounding box is 1373 × **37** cells against
  `DENSE_BORDER_MAX_WIDTH_CELLS = 33`. Its *mean* width is 26 and would
  pass; the bbox is inflated to 37 only because the band is slanted about
  0.46° relative to the canvas axes.
- `_is_featureless`: medians along its length run −3.271…−3.019, a spread
  of **0.252** against `DENSE_BORDER_MAX_SPREAD = 0.05`. The band fades in
  partway down the canvas rather than running its full height.

Loosening both was measured and **is not a fix**. With
`MAX_WIDTH = 40, MAX_SPREAD = 0.30` the detector fires and withholds 2.29%
of the region — and the floor moves only −3.14 → −2.84, still more than a
decade wrong.

The reason is the transition ramp. The film→carrier boundary is not a step;
it is a monotone fall through every density film legitimately occupies. On
`_DSC5280` the dense tail is cleanly bimodal — a carrier mode at −3.02, a
valley at −2.37, the film mode at −0.92 — but the ramp joining them is
continuous, and it holds far more than the 310 cells the floor percentile
reads. Whatever a density threshold removes, the ramp behind it still owns
the floor.

### 0.3 The ramp is a border phenomenon, which is what makes a rectangle right

After the §2 gap-aware mask removes the carrier lobe, the floor is still
wrong — and the cells responsible were measured for their distance to the
analysis region's edge:

| negative | floor after mask | converged floor | distance-to-edge of the ~400 floor-setting cells |
|---|---|---|---|
| `_DSC5280` | −2.37 | −2.06 | min 0, median 36, p90 45 — **100% within 60 cells** |
| `_DSC5207` | −2.29 | −1.94 | min 0, median 10, p90 18 — **100% within 60 cells** |
| `_DSC5215` | −2.41 | −1.74 | min 0, median 41, p90 48 — **100% within 60 cells** |

The residual is entirely edge-hugging ramp. Nothing in the interior of the
frame is involved. An inset rectangle clears it; a mask cannot.

So the two ideas are not alternatives. **The gap statistic locates the
incursion; the rectangle clears it.** Both are required.

### 0.4 The rectangle is safe on this rig, and that is a recorded decision

A per-edge inset driven by each edge's *deepest* incursion is only wasteful
if the incursion is not edge-shaped — a corner wedge would inset two whole
edges to their worst point.

**The user's negative carriers have sharp 90° corners** (stated 2026-09-07).
The incursion is therefore a set of edge-parallel bands, at most slightly
rotated by registration — 0.46° on `_DSC5280`, which costs about 11 cells
of extra bbox on a 1373-cell edge. Max-per-edge is the correct estimator
for that geometry, and no mask fallback is needed.

This plan therefore commits to the rectangle as the instrument, and keeps
only a **fail-loud guard** (§3.4) for the case where the detector latched
onto something that is not a carrier band at all. It does not carry a
second code path.

### 0.5 The rebate detector cannot carry this, and the reason is worth recording

Combining with `detect_rebate` was measured and rejected as the primary
mechanism, for a sharper reason than "sometimes there is no rebate".

- **On `_DSC5280`'s right edge there is no thin landmark at all.** The
  transition runs monotonically from film (−0.85) to carrier (−3.0) over
  8–12 cells with no plateau. The thinnest cell anywhere within 25 cells
  inboard of it is −0.79…−0.86, while the top-edge base measures −0.695 —
  so the film's own edge content there is *denser* than base, and
  `REBATE_ANCHOR_PERCENTILE − REBATE_DENSITY_TOLERANCE` can never reach it.
- **The thin anchor is global; the illumination is not.** Along
  `_DSC5280`'s top-edge rebate band the base density drifts monotonically
  across the canvas: −0.651 at column 50, −0.686 at column 870, −0.739 at
  column 1470. That is **0.088 decades of drift against a
  `REBATE_DENSITY_TOLERANCE` of 0.10** — the tolerance is almost entirely
  consumed by flat-field residual before any real base variation. A
  rebate-traced film boundary would need a *locally* anchored thin
  reference, which is a larger change, and it would still fail on edges
  with no rebate.

What rebate *is* good for here is corroboration: anything outboard of a
detected rebate component is not film at any density. On `_DSC5215` the
carrier mask already picks up the KODAK 200 edge printing on the left edge,
so the two agree where both fire. §5.3 records the cross-check; nothing
acts on it. Making it act is punchlist work, not this plan.

### 0.6 Ordering

The pass runs **after `withhold_opaque` and before `detect_rebate`**:

```
withhold_opaque        unchanged — the absolute gate, and its
                       "the analysis rect is on the holder" error
withhold_non_film      NEW
detect_rebate          unchanged, now measuring base on a film-only region
withhold_dense_border  unchanged, now seeing only intra-film edge fog
```

After the opaque gate, because a wholly-opaque rect must still raise its own
diagnostic rather than reach a histogram. Before `detect_rebate`, because
`Rebate.base_density` becomes `analyze_bounds`' `base_refs` and must be
measured on film only. The rebate is thin and does not disturb the dense
tail, so leaving it in `keep` during §2's histogram is harmless.

---

## 1. Chunk E-0 — the measurement tool

Every constant below was fitted on **three negatives from one roll on one
rig**. Most carry one to two orders of magnitude of margin on that data;
`FILM_EXTENT_LOBE_RISE` carries 2.5x on `_DSC5207` and is the binding one.
That is reassuring and it is not a measurement. Before E-3 pins anything,
the thresholds must be replayed over more rolls.

This is cheap because the whole analysis runs off **published TIFFs**: the
normalization is invertible from the recorded `floors`/`ceils`, so a tool
can reconstruct `grid_log` exactly and replay the meters without
re-stitching anything.

### Changes

New `cli/tools/measure_film_extent.py`, following
`cli/tools/measure_spot_thresholds.py`'s shape (argparse, `--roll`,
repeated `--negative`, `--out-dir`, a CSV plus PNG contact sheets).

For each negative it:

1. reads the published TIFF and its `normalization` record, and rebuilds
   `grid_log = decode_normalized(codes) * (ceils - floors) + floors`, then
   `block_median_grid`. **Note the one lossy step:** the encode clips at
   `-NORMALIZED_HEADROOM_LOW`, so truly opaque cells come back at the rail
   rather than at −6.0. That affects `withhold_opaque` replay only; the
   carrier lobe this plan targets sits well above the rail. The tool must
   say so in its output rather than pretend the reconstruction is exact.
2. rebuilds `keep` from the recorded `analysis_rect` divided by
   `ANALYSIS_BLOCK_PX`;
3. writes the region's log-luma histogram at `FILM_EXTENT_HISTOGRAM_BIN`,
   with the detected valley, lobe and mode marked;
4. writes the **inset sweep**: floor versus uniform inset in 10-cell steps
   to 150 cells, which is the evidence for §3's convergence rule;
5. writes a **mask overlay PNG** — film in grey, carrier mask in red — so
   the finding is judged by eye and not only asserted;
6. records to CSV: valley threshold, lobe fraction, mask fraction, the four
   per-edge insets, region fraction kept, resulting floors and spans, and
   the sweep.

### The protocol this tool serves

Run it over at least **three rolls** spanning: a roll with carrier on one
edge only, a roll with carrier on all four, and a roll with **no carrier
in frame at all** (the no-op case, which is the one that matters most —
§2.4). For each, confirm from the histogram that the valley is where the
detector put it, and from the sweep that the floor reaches a plateau at
least 50 cells wide. Append the results to this document as a "Measured
constants" section, in the manner of `docs/REBATE_ANCHORING.md`'s §11.

### Tests

None. It is a tool, not production code; `packaging_test.py` must not start
bundling it.

---

## 2. Chunk E-1 — the detector, recorded and acted on by nothing

Ship the finding first, on the MONOCHROME_PLAN §1.4 pattern: the record
appears, nothing consumes it, and a roll can be stitched to gather evidence
before published pixels move.

### 2.1 The statistic

Do **not** look for an empty gap in the sorted dense tail. There isn't one:
the two lobes are joined by a continuous ramp, and even at the valley's
floor `_DSC5280` still has 191 cells in the bin. Look for a *second mode*.

```python
def _find_valley(lum: np.ndarray, keep: np.ndarray) -> float | None:
    """The log density separating a contaminant lobe from the film lobe,
    or None when the dense tail is unimodal (the no-op path, and the
    common case)."""
    values = lum[keep]
    edges = np.arange(values.min(), values.max() + BIN, BIN)
    counts, edges = np.histogram(values, bins=edges)
    mode = int(np.argmax(counts))                 # the film lobe
    # 1. walk denser until the counts collapse relative to the film mode
    first = next((i for i in range(mode - 1, 0, -1)
                  if counts[i] <= FILM_EXTENT_VALLEY_DROP * counts[mode]), None)
    if first is None or counts[:first].sum() < FILM_EXTENT_MIN_LOBE_FRACTION * values.size:
        return None
    # 2. the contaminant's own mode is the tallest bin below the collapse,
    #    and the valley is the emptiest bin BETWEEN the two modes
    lobe = int(np.argmax(counts[:first]))
    between = counts[lobe + 1:mode]
    if between.size == 0:
        return None
    valley = lobe + 1 + int(np.argmin(between))
    # 3. gate at the valley, not at `first`
    if counts[valley] > FILM_EXTENT_VALLEY_DROP * counts[mode]:
        return None
    if counts[lobe] < FILM_EXTENT_LOBE_RISE * max(counts[valley], 1):
        return None
    if counts[:valley].sum() < FILM_EXTENT_MIN_LOBE_FRACTION * values.size:
        return None
    return float(edges[valley] + BIN / 2)
```

Three conditions, all required: the counts have collapsed relative to the
film mode, *and* something rises again below the collapse, *and* what rises
is big enough to matter. A smooth unimodal dense tail satisfies the first
and never the second.

**Step 2 is not cosmetic and must not be simplified away.** Returning the
*first* qualifying bin instead of the valley's minimum was measured, and it
puts the split on the ramp's shoulder rather than in the gap: on `_DSC5280`
the `LOBE_RISE` gate then clears by a factor of 1.0 — exactly on the
threshold — where locating the minimum clears it by 13.9. Same published
result, an order of magnitude more robustly reached.

### 2.2 The constants

All in `normalization.py`, nowhere else.

```python
# The histogram cell for the film/non-film split, in log10 D. Fine enough
# that the valley is located to better than the ramp's own width (8-12
# cells across, ~0.15 decades per cell on _DSC5280), coarse enough that
# a three-million-cell region fills every bin the film lobe occupies.
FILM_EXTENT_HISTOGRAM_BIN = 0.05
# A valley bin holds at most this fraction of the film mode's count.
# Measured across the three negatives: 0.00072, 0.00065, 0.00054 -- the
# gate carries 28-37x margin. Provisional and unmeasured beyond one roll.
FILM_EXTENT_VALLEY_DROP = 0.02
# ... and the contaminant's own mode is at least this many times the
# valley's count. Measured: 55.5x, 9.9x, 82.4x -- margins of 13.9x, 2.5x
# and 20.6x. **This is the binding gate of the three**, and _DSC5207's
# 2.5x is the tightest number in this plan; it is what E-0 must scrutinise
# hardest before E-3 pins it.
FILM_EXTENT_LOBE_RISE = 4.0
# A lobe smaller than this fraction of the region is not worth a rect.
# Sits above BASE_LUMA_CLIP's 0.0001 by 5x, because a contaminant that
# cannot reach the floor percentile cannot move the floor. Measured:
# 0.0219, 0.0044, 0.0269 -- margins of 44x, 9x, 54x.
FILM_EXTENT_MIN_LOBE_FRACTION = 0.0005
# Decades below the valley a cell must sit to seed a component. Three
# histogram bins: the seed must be unambiguously in the lobe, not in the
# valley's noise.
FILM_EXTENT_SEED_OFFSET = 0.15
```

### 2.3 The mask

Hysteresis plus border connectivity — the same two-level trick Canny uses,
for the same reason: the loose level alone would leak into film, the tight
level alone would miss the ramp.

```
seed  = keep & (lum <= valley - FILM_EXTENT_SEED_OFFSET)
loose = keep & (lum <= valley)
mask  = union of connected components of `loose` that contain a seed cell
        AND touch _region_border(keep)
```

Border connectivity here is physics, not a heuristic: non-film is *outside*
the film, so on a canvas it is always connected to the outside. Unlike
`withhold_dense_border`'s area/thinness/flatness gates, there is nothing
further to test — the shape of the carrier is not the discriminator.

### 2.4 The no-op path is the important one

`withhold_non_film` returns `keep` unchanged, with
`FilmExtent(detected=False, ...)`, when: `_find_valley` returns `None`, or
no seed cell survives, or no component contains both a seed and a border
cell. **A negative with no carrier in frame must reach this path**, and E-0
must demonstrate it on a real roll, not only on a synthetic grid.

Prototype evidence: re-running the detector on each of the three negatives'
*already-cleaned* regions found nothing, on all three.

### 2.5 The dataclass

```python
@dataclasses.dataclass(frozen=True)
class FilmExtent:
    """The film-extent pass's finding. `valley` is the absolute log density
    the split was made at and `lobe_fraction` the share of the region below
    it -- recorded because neither is recoverable from `insets`, and they
    are what a measurement of FILM_EXTENT_VALLEY_DROP / _LOBE_RISE would
    revise. `insets` is (top, bottom, left, right) in grid cells."""

    detected: bool
    valley: float | None
    lobe_fraction: float
    mask_fraction: float
    insets: tuple[int, int, int, int]
    region_fraction: float          # of `keep` surviving
    convergence_steps: int          # E-2; 0 in this chunk
```

### Changes

- `normalization.py`: `FilmExtent`, `_find_valley`, `withhold_non_film`,
  the five constants above. `withhold_non_film` computes the mask and the
  insets and **returns `keep` unchanged** in this chunk — it reports only.
- `composite.py`: call it between `withhold_opaque` and `detect_rebate`,
  discarding the returned keep; add `film_extent: FilmExtent` to
  `CompositeResult`.
- `stitch_pipeline._normalization_record`: write the `film_extent` block.

`NORMALIZE_FORMAT_VERSION` does **not** move in this chunk and the new
constants do **not** join `build_params()` — nothing shapes published
output yet. Both happen in E-3.

### Tests (`normalization_test.py`, `composite_test.py`)

- A synthetic grid with a film lobe and a separated dense band along one
  edge: the valley lands between the lobes; the mask covers the band and
  the ramp cells adjacent to it; `insets` names the right edge and only it.
- A unimodal synthetic grid: `detected is False`, `insets == (0, 0, 0, 0)`,
  `keep` returned identical (assert object equality of contents, not
  identity).
- A dense band that does **not** touch the region border (a dark object in
  the frame's interior at carrier density): not withheld. This is the gate
  that keeps the pass off scene content.
- A band below `FILM_EXTENT_MIN_LOBE_FRACTION`: not detected.
- `composite_test.py`: `CompositeResult.film_extent` is populated and the
  published pixels are byte-identical to before the chunk.

---

## 3. Chunk E-2 — from mask to rectangle, and the convergence check

### 3.1 Per-edge insets

Assign each mask cell to the edge of `keep`'s bounding box it is nearest
to; the inset for an edge is the deepest such cell's distance from that
edge, plus one. Edges with no mask cells get 0.

Measured against a uniform inset sweep, these land within a few cells of
the converged answer on all three negatives:

| negative | mask-derived insets (cells) | uniform sweep converges at | region kept |
|---|---|---|---|
| `_DSC5280` | top 28, bottom 39, right 39 | 50 | 93.2% |
| `_DSC5207` | bottom 20, right 20 | 20 | 96.2% |
| `_DSC5215` | top 47, bottom 42, left 5, right 48 | 60 | 90.7% |

The mask-derived insets sit a little inside the converged answer, which is
exactly what §3.2's loop exists to close.

### 3.2 The convergence loop replaces a pinned margin

Do not pin the margin. Measure it, per negative, by pushing until the floor
stops moving. The separation between "still eating ramp" and "on the
plateau" is two orders of magnitude:

```
_DSC5280   inset  40 →  50 cells:  floor +0.069      50 → 150 cells:  ≤0.001
_DSC5207   inset  10 →  20 cells:  floor +0.244      20 → 120 cells:  ≤0.002
_DSC5215   inset  50 →  60 cells:  floor +0.168      60 → 150 cells:  ≤0.002
```

The probe is the floor itself — `_percentile(lum[rect], BASE_LUMA_CLIP)`,
one percentile, cheap. Withholding contaminant makes the floor *rise*
(less negative), so the test is one-sided.

```python
insets = per_edge_insets(keep, mask)          # 3.1; may be 0 on an edge
insets = tuple(v + FILM_EXTENT_MARGIN_CELLS for v in insets)
steps = 0
for _ in range(FILM_EXTENT_MAX_STEPS):
    moved = False
    probe = _percentile(lum[_inset_rect(keep, insets)], BASE_LUMA_CLIP)
    for edge in range(4):
        trial = list(insets)
        trial[edge] += FILM_EXTENT_CONVERGENCE_STEP_CELLS
        rect = _inset_rect(keep, tuple(trial))
        if not _region_viable(rect):
            continue
        if _percentile(lum[rect], BASE_LUMA_CLIP) - probe > FILM_EXTENT_CONVERGENCE_DELTA:
            insets, moved, steps = tuple(trial), True, steps + 1
            break                              # re-probe before the next edge
    if not moved:
        break
```

**All four edges are probed, including edges the mask did not touch.** This
is deliberate and covers the one failure the mask cannot see: if
`valid_rect` happens to cut through the ramp, there is ramp inside `keep`
with no carrier core to seed a component, so that edge's mask-derived inset
is 0 and the ramp survives. The probe finds it. On a clean edge the loop
exits after one round having moved nothing, so the cost is one percentile
per edge.

### 3.3 The constants

```python
# Added to the mask-derived inset before the convergence loop starts, so
# the loop begins inside the ramp's shoulder rather than on its lip. The
# plateau is 100+ cells wide on every negative measured, so this value is
# not critical: sweeping it 0 -> 32 moved the resulting span by at most
# 0.04 log10 D.
FILM_EXTENT_MARGIN_CELLS = 8
# One convergence step, in cells. 10 cells is 60 source px at
# ANALYSIS_BLOCK_PX (0.36 mm at the reference rig), about a quarter of the
# widest ramp measured -- ~40 cells, on _DSC5215.
FILM_EXTENT_CONVERGENCE_STEP_CELLS = 10
# Floor movement per step, in log10 D, below which an edge is converged.
# In-plateau steps measured at <= 0.002 and pre-plateau steps at 0.069 to
# 0.244, so this sits in empty space between them.
FILM_EXTENT_CONVERGENCE_DELTA = 0.01
# Cap on total steps. 24 steps is 240 cells of travel shared across four
# edges -- 8.6 mm at the reference rig, using the same px/mm as
# DENSE_BORDER_MAX_WIDTH_CELLS' 1.2 mm. Far beyond any incursion measured
# (the deepest was 48 cells), and the guard against a pathological grid
# walking the rect down to nothing.
FILM_EXTENT_MAX_STEPS = 24
```

### 3.4 The guard is fail-loud, not a fallback

Per §0.4 there is no second code path. There is one guard, and it warns
rather than switching strategy:

```python
# The rect must keep at least this fraction of the analysis region. Below
# it, what the detector found is not a carrier band -- a genuinely dark
# scene band along an edge, or an analysis rect that was mostly non-film
# to begin with. The worst case measured was 90.7% kept, on _DSC5215's
# four-edge carrier -- so this gate sits far from anything observed.
FILM_EXTENT_MIN_REGION_FRACTION = 0.50
```

Below it: **still apply the rect**, record `region_fraction`, and emit
`NORMALIZE_FILM_EXTENT_EXCESSIVE` as a `WarningEvent`. Applying is the
right call — a region that is more than half non-film has a floor that is
certainly wrong — but the user must be told the frame is unusual. If
`_region_viable` would leave fewer cells than `NEUTRAL_MIN_PIXELS`, raise
`NormalizationError` with a message naming the analysis rect, in the manner
of `withhold_opaque`'s wholly-opaque error.

### Changes

`normalization.py` only: `per_edge_insets`, `_inset_rect`, the convergence
loop inside `withhold_non_film`, the five constants, the guard. Still
reporting-only — `composite.py` continues to discard the returned keep.

### Tests (`normalization_test.py`)

- Synthetic grid with a ramped edge band: the loop converges, `insets`
  exceeds the mask-derived value, `convergence_steps > 0`.
- A grid whose ramp reaches inside `keep` with its core outside it: the
  mask-derived inset for that edge is 0 and the loop still finds it. This
  is §3.2's whole justification and must have a test.
- A clean grid: the loop exits with `convergence_steps == 0` and the
  mask-derived insets unchanged.
- `FILM_EXTENT_MAX_STEPS` is honoured on an adversarial monotone gradient.
- A region driven below `FILM_EXTENT_MIN_REGION_FRACTION` produces the
  warning and still applies; one driven below viability raises.

---

## 4. Chunk E-3 — act on it

### Changes

- `composite.py`: use the returned `keep`. This is the line that moves
  published pixels.
- `normalization.py`: `NORMALIZE_FORMAT_VERSION` 4 → **5**, and all ten
  `FILM_EXTENT_*` constants (five from E-1, five from E-2) join
  `build_params()`.
- `events.py`: `NORMALIZE_FILM_EXTENT_WITHHELD` (info, emitted when
  `detected`) and `NORMALIZE_FILM_EXTENT_EXCESSIVE` (warning, §3.4).
  `PROTOCOL_VERSION` 17 → **18**.
- `stitch_pipeline.py`: emit both, beside the `NORMALIZE_HEADROOM_CLIPPED`
  site. The info event's message should name the four insets in **pixels**,
  not cells — the user thinks in canvas pixels.
- `mac/ScannyBoy/Model/CLICode+FriendlyNames.swift`: friendly names for
  both codes.

### 4.1 The sibling gap, fixed here

`build_params()` currently records the `DENSE_BORDER_*` family but **not**
`REBATE_*` and **not** `OPAQUE_*`, though all three shape published output.
`OPAQUE_*` also never bumped `NORMALIZE_FORMAT_VERSION` when it landed, and
its `opaque` block is written to the manifest but **not declared in
`shared/contract/roll-manifest.schema.json`** (it validates only because
that definition has no `additionalProperties: false`).

Since this chunk bumps the format version and touches `build_params()`
anyway, fold both families in at the same time. Adding one family while two
siblings stay out would make the record harder to reason about, not easier.
This is a bounded addition — keys and a schema block, no behaviour change —
and it is the last moment where it is free.

### 4.2 Existing rolls

A v4 roll's recorded bounds are not comparable with a v5 one; that is what
the version says. The three negatives on `Sep-6-2026-at-10-56-PM` must be
re-stitched, and the plan's success criterion is that all three spans land
within 0.02 log10 D of §0's "true span" column rather than at 2.4–2.7.

### Tests (`composite_test.py`, `stitch_pipeline_test.py`, `events_test.py`)

- End-to-end on a synthetic canvas with a carrier band: the published
  bounds match those from a hand-cut region to within
  `FILM_EXTENT_CONVERGENCE_DELTA`.
- A canvas with no band: bounds byte-identical to E-2.
- `events_test.py`: `PROTOCOL_VERSION == 18`; both codes round-trip.
- `stitch_pipeline_test.py`: the two filters at lines 932 and 969 that
  currently except `NORMALIZE_HEADROOM_CLIPPED` need review — a synthetic
  fixture that now trips the film-extent info event will surface there.

---

## 5. Chunk E-4 — contract, schema, decisions

### 5.1 The manifest block

```json
"film_extent": {
  "detected": true,
  "valley": -2.37,
  "lobe_fraction": 0.0219,
  "mask_fraction": 0.0180,
  "insets": [28, 39, 0, 39],
  "region_fraction": 0.932,
  "convergence_steps": 2,
  "rebate_agrees": null
}
```

`insets` is in **grid cells**, matching `DENSE_BORDER_MAX_WIDTH_CELLS` and
the rest of the region gates; the manifest already records
`analysis_block_px`, so pixels are one multiplication away. Say so in the
schema description, because `analysis_rect` beside it is in canvas pixels
and the mismatch will otherwise bite someone.

### 5.2 Schema

`shared/contract/roll-manifest.schema.json`: declare `film_extent`, and
declare the missing `opaque` block per §4.1. Neither joins `required` —
negatives stitched before this plan have neither.

`shared/contract/CONTRACT.md`: protocol 18, the two new codes.

### 5.3 The rebate cross-check

Record `rebate_agrees` as a nullable boolean: `null` when no rebate
component was detected on any inset edge, otherwise whether every detected
rebate component lies inboard of the corresponding inset. **Nothing reads
it.** It exists so that a later plan deciding whether to promote rebate to
a hard outer bound (§0.5) has evidence from real rolls rather than
argument.

### 5.4 `docs/DECISIONS.md`

Append a section, "The film extent, and the black point (E-1…E-4)", under
the existing "The analysis region, and the rebate detector (D-3, §3.13)".
It must record, durably:

- the governing principle of §0.1 — the region is not maximal, and why;
- that the rectangle is the instrument because the carriers have sharp 90°
  corners, and that this is a **rig fact**, so a user with a different
  carrier geometry invalidates §0.4 and needs the mask path this plan
  deliberately did not write;
- the §0.5 finding that base density drifts 0.088 decades across a stitched
  canvas against a 0.10 tolerance, which is the reason the rebate detector
  is not the mechanism here and the reason any future rebate-traced
  boundary needs a local anchor;
- that `withhold_dense_border` was measured and found insufficient, with
  the numbers, so nobody re-litigates loosening its constants.

---

## 6. What must stay true

Invariants for anyone touching this later:

1. **The no-op path is load-bearing.** Most negatives have no carrier in
   frame. A change that makes `withhold_non_film` fire on a clean frame is
   a regression even if the resulting picture looks fine.
2. **Border connectivity is not optional.** It is the only thing standing
   between this pass and a dark object in the middle of the frame.
3. **The convergence probe reads `BASE_LUMA_CLIP`.** If that constant ever
   moves, the probe moves with it — they are the same statistic, and the
   loop is meaningless if they diverge.
4. **The pass never crops output.** Like the analysis region it refines, it
   restricts the meters only. `valid_rect` and `coverage_fraction` remain
   the machine-readable coverage answer.
5. **`insets` are cells, `analysis_rect` is pixels.** Do not unify them
   without changing both and the schema description.

---

## 7. Order, verification, and the open calibrations

E-0 → E-1 → E-2 → E-3 → E-4, each independently green. E-0 and E-1 may land
in either order, but **E-0's evidence must exist before E-3 pins the
constants into `build_params()`** — that is the chunk after which they are
roll invariants and moving them costs a format version.

Verification that the plan worked, in order:

1. `cd cli && uv run pytest` stays green at every chunk boundary.
2. `uv run pytest --slow` after E-3, which is the only tier that exercises
   real RAW decode, stitching and the tone curve.
3. `./scripts/test-mac.sh` after E-3 for the friendly-name additions.
4. Re-stitch `Sep-6-2026-at-10-56-PM` and check the three spans against
   §0's table.

### The open calibrations

All nine `FILM_EXTENT_*` constants are **provisional and unmeasured**, the
same status `REBATE_*`, `DENSE_BORDER_*` and `OPAQUE_*` carry. They are
fitted to three negatives from one roll on one rig. They go on
`docs/punchlist.md` together, with `cli/tools/measure_film_extent.py` named
as the instrument that would close them.

Two specific things this plan did not resolve:

- **The `NORMALIZE_HEADROOM_CLIPPED` population.** Under corrected bounds,
  `_DSC5207` puts 0.17% of mask-surviving grid cells past the encode
  headroom, above `HEADROOM_CLIP_WARN_FRACTION`. Those are believed to be
  ramp cells rather than real film, but that was measured on the grid, not
  on the published full-resolution pixels, and the warning currently
  measures over all covered pixels. Once E-3 lands, check whether the
  warning starts firing on frames where it should not, and whether its
  population should become film-only.
- **Promoting rebate to a hard outer bound** (§0.5, §5.3), which needs the
  `rebate_agrees` evidence from real rolls first.
