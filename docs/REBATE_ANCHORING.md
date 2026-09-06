# Rebate anchoring: a per-roll film-base reference for the thin end

Today a negative's thin-end bounds (`Bounds.ceils`) come from percentiles of
**scene content**. `normalization.analyze_bounds`' own docstring calls the
thin end "physically anchored: density on real film is bounded below by
base" — but nothing measures the base. It is a percentile that lands near
base only when the scene happens to have deep shadows.

Meanwhile `normalization.detect_rebate` already measures the real thing —
`Rebate.base_density`, the per-channel median log density inside the film
rebate — and `stitch_pipeline._normalization_record` writes it into the roll
manifest, where **nothing ever reads it back**.

This plan closes that loop with a dedicated capture step:

1. The user shoots **one extra frame per roll**, showing a lot of film
   rebate, exposed about two stops darker than the roll's scans. Call it the
   **base frame**.
2. The base frame is attached to the roll **before any scans are
   converted**, through its own command and its own field in the Add Scans
   sheet. It can be replaced freely until it is used; the first successful
   conversion **locks** it for the life of the roll.
3. Every negative on the roll then takes its thin-end **per-channel colour
   deviation** from that locked measurement instead of from its own scene
   percentiles.

This plan follows the conventions of `docs/MONOCHROME_PLAN.md`,
`docs/DENSITY_PLAN.md` and `docs/GRID_STITCH_PLAN.md`: numbered chunks, each
independently green; every constant in exactly one module; every threshold
that shapes an output recorded in the roll manifest; and **no threshold
pinned without a measurement the user has approved** — §11 exists to produce
that measurement, and chunk B-5 must not land before it.

**Existing rolls are invalidated on purpose** (§9). This is a decision the
user has taken: rolls stitched before this feature stay readable, editable
and exportable, but nothing new can be stitched into them. There is no
compatibility shim anywhere in this plan.

---

## 0. Why this shape, and why in this order

### 0.1 What is actually wrong today

`normalization.analyze_bounds` (`cli/src/scanny_boy/normalization.py:331`)
samples on two independent axes and recombines them:

```python
mean_lf = percentile(luma, BASE_LUMA_CLIP)              # luma axis: level
mean_lc = percentile(luma, 100 - BASE_LUMA_CLIP)
c_ceils = [percentile(values[:, ch], 100 - BASE_COLOR_CLIP) ...]   # colour axis
mean_cc = median(c_ceils)
ceils   = tuple(mean_lc + (c_ceils[ch] - mean_cc) for ch in range(channels))
```

`c_ceils` is meant to measure **the orange mask** — the per-channel offset the
film base imposes. On a frame with real shadows, the thinnest scene content
sits close to base and `c_ceils` approximates it. On a high-key frame — snow,
sky, a backlit portrait, a frame with no shadow anywhere — the thinnest
content is a long way from base, its colour is the *scene's* colour, and the
mask estimate is wrong. The whole negative then carries a colour cast that
`clamp_bounds` can only partly pull back, because the roll population it
clamps against was estimated the same way.

That is the failure this plan removes.

### 0.2 The load-bearing property: the colour axis is already level-free

**Read this section before writing any code. Every safety argument in this
plan rests on it, including the entire exposure story in §1.**

Look at the recombination again:

```python
ceils[ch] = mean_lc + (c_ceils[ch] - median(c_ceils))
```

Only `c_ceils[ch] − median(c_ceils)` survives. Add a constant `k` to every
entry of `c_ceils` and the result is unchanged. **The colour axis contributes
shape, never level; the level comes entirely from `mean_lc`, which is
measured on the negative itself.**

Now: what does a difference in exposure between the base frame and the roll's
scans do to a measured base density? A shutter, aperture or ISO change scales
linear light by one constant factor `k` across all three channels. In log
density that is `+log10(k)` in every channel — pure common mode. The
deviations are untouched.

Three consequences, and they are what make this feature safe:

- **The base frame's exposure does not have to match the roll's.** It only
  has to be the same film, the same light source, the same camera body, and
  the same flat-field profile.
- **The base frame *should* be shot darker** (§1). Clear base is the thinnest
  thing on the film and therefore the brightest thing in the scan; at the
  roll's own exposure it may well clip, and clipped base is worthless base.
  Because the measurement is exposure-invariant, stopping down costs nothing.
- **The camera's white balance is irrelevant.** `raw_decode.RAW_PARAMS` sets
  `use_camera_wb=False`, `use_auto_wb=False`, `user_wb=[1,1,1,1]`,
  `output_color=rawpy.ColorSpace.raw`. Channel ratios are a fixed property of
  the sensor, the light source and the film — not of a camera setting.

The second consumption site (§4.2) is invariant for the same reason:
`_same_pixel_color_floor_refs` computes `anchored = g_flat - base` and then
`chroma = anchored.max(axis=1) - anchored.min(axis=1)`. A common-mode shift
in `base` shifts every channel of `anchored` equally and `max − min` does not
move.

§11's measurement exists to **verify this claim on real film** rather than
assume it. Do not skip it.

### 0.3 Why a dedicated base frame and not the per-negative rebate

Three reasons, in order of importance.

1. **Not every negative shows usable rebate.** A tightly framed scan, a 2×2
   grid whose middle cells are all picture, a strip scanned edge to edge —
   any of these leaves `detect_rebate` with nothing to find. A feature that
   only works sometimes cannot be an anchor.
2. **`detect_rebate` cannot measure a frame that is mostly rebate.** Its
   separation gate reads
   `percentile(lum[component], 50) < percentile(lum[outside], 99) + REBATE_MIN_SEPARATION`,
   and it explicitly `continue`s when `outside` is empty. On a frame that is
   *entirely* base there is no outside, so the detector correctly finds
   nothing. This is not a bug to fix — that gate is what makes "no rebate at
   all" return cleanly on a real negative — it means the base frame **needs
   its own detector** (§2.2), not a call into `detect_rebate`.
3. **A dedicated frame can be shot for the job.** Stopped down so it cannot
   clip, framed so rebate dominates. The per-negative rebate is whatever
   geometry happened to leave in the corner.

The per-negative rebate finding stays exactly where it is, doing exactly what
it does today (withholding base cells from the meters). §6 adds one use for
it: cross-checking the roll anchor, recorded and acted on by nothing.

### 0.4 The base frame is not necessarily pure rebate

This is the requirement that shapes §2 more than any other. The user's film
does not always have a long clear run: a base frame may be

- entirely clear base (a piece of leader) — the easy case;
- two rebate bands with a strip of picture between them (a frame straddling
  the gap between negatives) — **the common case**;
- one rebate band with picture filling the rest.

So the measurement cannot be "take the median of the frame". It must
**find** the base region inside a frame that may also contain image content,
and it must not be fooled by the two things that are *thinner* than base:
bare light around the film edge, and sprocket holes.

On a negative the density ordering is fixed and one-sided, thinnest first:

| | thinnest → densest |
|---|---|
| 1 | bare light (no film in the path) |
| 2 | sprocket holes, gaps between strips |
| 3 | **film base / rebate** ← what we want |
| 4 | scene shadows (thinnest image content) |
| … | … |
| n | scene highlights (densest image content) |

Nothing in a photograph can be thinner than base, so the discriminator is
clean in one direction. The detector's whole job is to pick population 3 out
of that ordering, and its safety rule is **refuse rather than guess** (§2.3).

### 0.5 Why the roll, and why locked

Film base is a property of the film stock and its development. A roll is one
stock, developed once. So the measurement belongs to the roll, exactly like
`film.kind` (`docs/MONOCHROME_PLAN.md` §0.3) and for the same reason: a
per-negative value would drift and produce a roll whose negatives disagree
about what the mask is.

The lock exists because the anchor shapes published pixels. Negatives
stitched under anchor A and negatives stitched under anchor B would not match
each other, and the roll would be internally inconsistent in a way no later
edit could fix. So: **free to replace until the first negative is published,
frozen forever after.** Changing it after that means a new roll.

**Be honest about the cost.** Today a bad thin-end estimate ruins one
negative. After this lands, a bad *base frame* — one that passes the gates
but is not really clean base — ruins the colour of the **whole roll**. That
is only an acceptable trade if:

- the gates in §2.3 are strict and fail loudly rather than degrading quietly,
- the user can see and replace the frame before committing (§3, the unlocked
  window), and
- §6's drift evidence is recorded from day one so a bad anchor is visible in
  the manifest afterward.

All three are requirements of this plan, not nice-to-haves.

### 0.6 What this does not change

- **The level.** `mean_lc` still comes from the negative's own luma
  percentile. The base frame fixes the *colour* of the thin end, not the
  white point. §12 records why anchoring the level too is deferred, and §6
  records the evidence that would settle it.
- **The dense end.** `floors` and `c_floors` are untouched.
- **The published format.** Still normalized log density, still
  `decode_normalized` as the single inverse, still the same headroom
  constants. `NORMALIZE_FORMAT_VERSION` does not move.
- **Existing published negatives.** Their `floors`/`ceils` are recorded in
  their own manifest blocks, so they remain correctly decodable, editable and
  exportable forever (§9).

### 0.7 Ordering is mandatory

- **B-1 → B-2 → B-3 → B-4**: the detector module, the roll-level state and
  its command, the run-time gate, then the drift evidence. Through B-4
  nothing the pipeline publishes changes: the anchor is measured, stored,
  locked, and read by nobody.
- **§11's measurement gate sits between B-4 and B-5.** B-5 is the only chunk
  that changes published pixels and must not land until the user has approved
  the numbers §11 produces.
- **B-6 (the Mac app) can be developed in parallel with B-2…B-4** but must
  land before or with B-3, because B-3 is what makes a run fail without a
  base frame and the app needs the field by then.

---

## 1. The capture rule

This is what the user does, and what the Add Scans sheet must say.

> **Shoot one base frame per roll, before you scan the roll.**
>
> Frame a stretch of the film that shows **as much rebate (clear film base)
> as you can find** — a piece of leader is ideal, and the gap between two
> negatives works too. Some image content in the frame is fine; the rebate
> just has to be the largest flat area. Keep bare light and sprocket holes
> out of the frame.
>
> **Expose about two stops darker than your scanning exposure**, so that the
> rebate sits roughly in the middle of the camera's histogram. Do not go
> more than about three stops down.
>
> Same camera, same lens, same light panel, same flat-field profile as the
> roll's scans. The exposure does *not* need to match — only the film, the
> light and the rig do.

### 1.1 Why two stops, and why "the middle of the histogram" is the right cue

At the roll's normal scanning exposure the clear base sits near the top of
the raw histogram — call it 0.8 of full scale — because the exposure is set
to keep the *dense* end off the floor. Stopping down and converting to what a
camera's (roughly sRGB-encoded) histogram displays:

| stops down | linear | camera histogram |
|---|---|---|
| 1 | 0.40 | ~66% |
| **2** | **0.20** | **~48%** |
| 3 | 0.10 | ~35% |

So the user's instinct is right: **roughly centring the rebate in the
histogram is about two stops down**, and that is the number to put in the
UI. Treat 1.5–3 stops as the acceptable band; the gates in §2.3 are the real
safety net and will say so if the frame is unusable.

### 1.2 Why not stop down further

Because of the mask. On colour negative film the orange mask is *dense in
blue*: through clear base, blue typically runs 0.8–1.2 log10 D below red —
two to four stops. So if the rebate's luma sits at 0.20 of full scale, its
blue channel is already down around 0.05, and at three stops down it is
around 0.03. The block-median reduction over ~10⁵–10⁶ grid cells crushes the
noise, but there is no reason to spend the headroom: the measurement is
exposure-invariant (§0.2), so darker buys nothing and eventually costs blue
precision.

`FILM_BASE_MIN_CHANNEL` (§2.1) is the gate that enforces this. It is a
**per-channel** floor, not a luma floor, precisely because blue is the
channel that runs out first and a luma-only test would not see it.

### 1.3 What is checked, and what cannot be

| requirement | how it is checked | on failure |
|---|---|---|
| a dominant flat rebate region exists | §2.2's detector + area gate | error |
| it is not sensor-clipped | per-channel clipped fraction | error |
| no channel is too dark to measure | `FILM_BASE_MIN_CHANNEL` | error |
| no second large flat region of different density | ambiguity gate | error |
| same camera body as the roll's scans | EXIF camera model | warning |
| same flat-field profile as the run | recorded profile id | warning |
| readable NEF | `raw_decode.decode_raw` | its own existing codes |

Cannot be checked, and must therefore be said in the UI text: **same film
stock, same development, same light panel.**

Deliberately not imposed: matching shutter, aperture, ISO or white balance
(§0.2).

---

## 2. The new module: `cli/src/scanny_boy/film_base.py`

A new module. Every constant of the feature lives here and nowhere else. It
owns the decode of one reference frame, the detector, the gates, and the
params record. It knows nothing about manifests or rolls.

### 2.1 Constants

All of these are **provisional and unmeasured**, in the same status as
`REBATE_*` and `DENSE_BORDER_*` in `normalization.py`. §11 pins them. Ship
with these starting values, record the measured statistics from day one, and
change the numbers only at the user gate.

```python
# The measurement runs on normalization's block-median grid, at
# normalization.ANALYSIS_GRID. This module does not define its own.

# --- finding the populations ---
# Log10 D. Width of the candidate band taken below each pass's thin anchor.
# Wider than normalization.REBATE_DENSITY_TOLERANCE because a base frame's
# rebate is a large region that may carry a gentle residual gradient.
FILM_BASE_BAND_WIDTH = 0.15
# Thin-end anchor percentile within each pass's remaining cells.
FILM_BASE_ANCHOR_PERCENTILE = 99.5
# Log10 D, P90 - P10 within one component: base is featureless.
FILM_BASE_MAX_COMPONENT_SPREAD = 0.05
# Log10 D. Two populations closer than this are the same population — this
# is what merges two rebate bands on opposite sides of the frame into one
# measurement instead of throwing half the data away (§2.2 step 5).
FILM_BASE_MERGE_SEPARATION = 0.06
# How many peel passes enumerate populations from the thin end down.
FILM_BASE_MAX_PASSES = 4

# --- gating the result ---
# Of the whole grid. "A lot of rebate": the chosen population must be at
# least this much of the frame.
FILM_BASE_MIN_AREA_FRACTION = 0.20
# Ambiguity gate. If some OTHER separated flat population is at least this
# fraction of the chosen one's area, the frame is refused rather than
# guessed at (§2.3 gate 5).
FILM_BASE_AMBIGUOUS_RATIO = 0.60
# Per-channel fraction of the chosen population's cells at or above
# normalization.SCAN_CLIP_LEVEL past which the frame is refused. Clipped
# base is worthless base — the same line detect_rebate already takes.
FILM_BASE_MAX_CLIPPED = 0.001
# Log10 D. Per-channel median floor inside the chosen population. Blue
# through an orange mask is the channel that runs out first (§1.2), so this
# is deliberately per-channel and not a luma test.
FILM_BASE_MIN_CHANNEL = -2.0
# Grid cells in the chosen population. Fewer is too few samples for a stable
# per-channel median.
FILM_BASE_MIN_CELLS = 1024

# Bumped whenever the measurement's arithmetic changes in a way that makes an
# old recorded value non-comparable with a fresh one.
FILM_BASE_MEASURE_VERSION = 1
```

### 2.2 The detector

```python
@dataclasses.dataclass(frozen=True)
class Population:
    """One flat, thin population found in the base frame. `area_fraction` is
    of the whole grid; `density` is the per-channel median log10 density
    inside it."""

    density: tuple[float, float, float]
    luma: float
    area_fraction: float
    cells: int
    spread: float


@dataclasses.dataclass(frozen=True)
class BaseMeasurement:
    """One base frame's finding. `density` is the per-channel median log10
    density of the chosen population — the same quantity, measured the same
    way, as normalization.Rebate.base_density, so the two are directly
    comparable (§6). Always three channels: the base frame is decoded as RGB
    whatever the roll's film kind turns out to be (§5).

    `populations` is every population the detector found, thinnest first,
    recorded whether or not the frame passed. §11 reads these off real frames
    to pin the thresholds, and a rejected frame's list is what tells the user
    what went wrong."""

    density: tuple[float, float, float]
    chosen_index: int
    populations: tuple[Population, ...]
    clipped_fractions: tuple[float, float, float]
    grid_cells: int
    measure_version: int = FILM_BASE_MEASURE_VERSION


class FilmBaseError(Exception):
    """A base frame that cannot be used. Carries a stable CONTRACT.md code,
    exactly like flatfield.FlatFieldError. A bad NEF is NOT one of these —
    decode_raw's UnsupportedRawError / UnreadableRawError propagate
    unchanged, because a bad NEF already has stable codes."""

    def __init__(self, code: Code, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
```

```python
def measure(linear: np.ndarray) -> BaseMeasurement:
    """Find the film-base population in one decoded, flat-fielded base frame.

    `linear` is float32 linear light in [0, 1] — what linear.decode_to_linear
    returns — with the run's flat-field gain already applied. Flat-fielding is
    not optional: the gain map is what removes the light panel's falloff, and
    without it the falloff alone can split one rebate region into several
    components or fail the flatness test.

    Measures and chooses; does not gate. `gate()` gates, so a caller can
    record a failing frame's populations before raising (§11).
    """
```

Steps, in order. This mirrors `normalization.withhold_dense_border`'s
multi-pass peel structure deliberately — copy that function's shape.

1. `grid = normalization.block_median_grid(normalization.to_log_density(linear))`,
   `lum = normalization.luma_of_log(grid)`.
2. `remaining = np.ones(lum.shape, dtype=bool)`; `found: list[np.ndarray] = []`.
3. Repeat up to `FILM_BASE_MAX_PASSES` times:
   - `anchor = percentile(lum[remaining], FILM_BASE_ANCHOR_PERCENTILE)`;
   - `candidates = remaining & (lum >= anchor - FILM_BASE_BAND_WIDTH)`;
   - connected components of `candidates` via
     `cv2.connectedComponentsWithStats(..., connectivity=8)`;
   - keep each component whose
     `percentile(lum[component], 90) - percentile(lum[component], 10) <= FILM_BASE_MAX_COMPONENT_SPREAD`;
   - append every kept component's mask to `found`; remove all candidates
     (kept or not) from `remaining`;
   - break when nothing was kept or `remaining` is empty.
4. **No border-connectivity gate, and no separation-from-outside gate.** Both
   exist in `detect_rebate` because there rebate is a minority intruding on a
   picture; here it is the subject. Including them is the single most likely
   mistake an implementer will make — they are what make `detect_rebate`
   return nothing on an all-rebate frame (§0.3).
5. **Merge by density, not by space.** Group the components in `found` into
   populations: two components belong to the same population when their luma
   medians differ by less than `FILM_BASE_MERGE_SEPARATION`. This is what
   makes two rebate bands on opposite sides of a frame one measurement of
   ~40% of the frame rather than two of ~20% each — the common case (§0.4).
   Build one `Population` per group over the union of its cells.
6. Sort populations **thinnest first** (descending luma; remember log density
   is negative and thinner is larger).
7. **Choose the population with the largest `area_fraction`.** That is the
   rule, and it is what the capture instruction is written to satisfy:
   rebate is the largest flat thing in the frame. Bare light and sprocket
   holes are thinner but small; image content is not flat.
8. `clipped_fractions` is measured **inside the chosen population only**, on
   `10 ** grid`, against `normalization.SCAN_CLIP_LEVEL`.

Implementation notes:

- **Use `normalization.block_median_grid` and `normalization.to_log_density`.
  Do not reimplement either.** The block median is what makes the statistic
  dust-immune and resolution-invariant, and it is the same reduction the
  per-negative path uses, which is what makes §6's comparison meaningful.
- **The median, not the mean**, everywhere. A dust shadow that survived the
  block median must not move the answer.
- If step 3 finds no population at all, return a `BaseMeasurement` with
  `populations=()` and `chosen_index=-1`; `gate()` turns that into
  `FILM_BASE_NOT_FOUND`. `measure` never raises.

### 2.3 The gates

```python
def gate(measurement: BaseMeasurement) -> None:
    """Raise FilmBaseError unless `measurement` came from a usable base
    frame. Checked in the order below; the first failure is the one the user
    sees, so the order is chosen for diagnostic value."""
```

| # | condition | code | message shape |
|---|---|---|---|
| 1 | `not measurement.populations` | `FILM_BASE_NOT_FOUND` | "no flat film-base region was found in the base frame; it must show a large area of clear rebate" |
| 2 | `chosen.area_fraction < FILM_BASE_MIN_AREA_FRACTION` | `FILM_BASE_TOO_SMALL` | "the largest flat rebate region covers only 9% of the base frame (at least 20% is needed); reframe to include more clear film base" |
| 3 | `chosen.cells < FILM_BASE_MIN_CELLS` | `FILM_BASE_TOO_SMALL` | as above, phrased in cells |
| 4 | `max(clipped_fractions) > FILM_BASE_MAX_CLIPPED` | `FILM_BASE_CLIPPED` | "the base frame's rebate is sensor-clipped in the red channel; re-shoot it about two stops darker — the exposure does not need to match the roll" |
| 5 | `min(chosen.density) < FILM_BASE_MIN_CHANNEL` | `FILM_BASE_TOO_DARK` | "the base frame's blue channel through the rebate is too dark to measure (−2.3); re-shoot it brighter — about two stops below your scanning exposure, not more than three" |
| 6 | any other population with `area_fraction >= FILM_BASE_AMBIGUOUS_RATIO * chosen.area_fraction` | `FILM_BASE_AMBIGUOUS` | "the base frame contains two large flat regions of different density (48% and 37% of the frame); one of them may be bare light or a second strip — reframe so the film rebate clearly dominates" |

Gate 5 is the one that catches "too far stopped down" and the one most likely
to fire in practice on colour film, because blue through the mask is the
first channel to run out (§1.2). Its message must name the remedy in stops.

Gate 6 is the refuse-rather-than-guess rule from §0.4. It fires on exactly
the frames where step 7's "largest wins" heuristic is not safe.

### 2.4 Loading a base frame

```python
def load(reference: Path, gain_map: np.ndarray | None) -> BaseMeasurement:
    """Decode `reference` with the locked RAW_PARAMS, apply `gain_map` if
    given, and measure. Does NOT gate — the caller gates, so it can record a
    failing frame's populations before raising (§11).

    Geometric correction (distortion, CA) is deliberately not applied: the
    measurement is a per-channel median over a flat region, which no
    geometric warp moves. Flat-field IS applied, because falloff is what the
    flatness test would otherwise trip on.
    """
```

1. `decoded = raw_decode.decode_raw(reference)` — let
   `UnsupportedRawError` / `UnreadableRawError` propagate unchanged.
2. If `gain_map is not None`: `flatfield.resize_gain_map(gain_map, decoded.width, decoded.height)`
   then `flatfield.apply_in_place(pixels, full_res_gain)`. Ignore the returned
   clipped count — gate 4 measures clipping on the result, which is the
   number that matters.
3. `linear = linear_module.decode_to_linear(pixels)`.
4. `return measure(linear)`.

### 2.5 Params

```python
def build_params() -> dict:
    """The feature's constants, recorded under stitch_params["film_base"]
    (§9). These shape published pixels once B-5 lands — a frame that fails a
    gate produces no roll at all, and the chosen population decides the
    anchor — so they are roll invariants."""
```

Return every `FILM_BASE_*` constant above, keyed by its lower-cased name
without the prefix, plus `"measure_version"`. `stitch_pipeline._stitch_params`
(`cli/src/scanny_boy/stitch_pipeline.py:224`) gains one line:

```python
        "film_base": film_base.build_params(),
```

**No forward shim is needed** (§9): rolls stitched before this feature are
invalidated, so no stored `stitch_params` without a `film_base` key will ever
be compared against a fresh one.

---

## 3. The roll-level state machine

### 3.1 The `film_base` manifest block

A new top-level block on `RollManifest`
(`cli/src/scanny_boy/roll_manifest.py:428`), sibling to `film` and
`camera_color`:

```python
    # REBATE_ANCHORING §3.1: the roll's film-base reference. `None` until
    # `roll set-base-frame` attaches one; replaceable while `locked_at` is
    # None; frozen for the life of the roll once the first negative has been
    # published against it (§3.2). A roll cannot be stitched without one.
    film_base: dict[str, Any] | None = None
```

Add it to `to_dict()` beside `"film": self.film`, to `from_dict`, and to the
`repo` row. Follow `film`'s plumbing exactly — it is one nullable JSON object
on the roll row.

Block shape:

```json
{
  "density": [-0.4213, -0.1187, -0.9902],
  "locked_at": null,
  "attached_at": "2026-09-06T18:04:11Z",
  "source_name": "_DSC5012.NEF",
  "source_sha256": "3f9c…",
  "flat_field_profile_id": "a1b2c3d4-…",
  "camera_model": "NIKON Z f",
  "chosen_index": 0,
  "populations": [
    {"density": [...], "luma": -0.21, "area_fraction": 0.44, "cells": 34_100, "spread": 0.012},
    {"density": [...], "luma": -0.68, "area_fraction": 0.09, "cells":  7_020, "spread": 0.031}
  ],
  "clipped_fractions": [0.0, 0.0, 0.0],
  "grid_cells": 786432,
  "measure_version": 1
}
```

- `density` is always a 3-array, even on a monochrome roll (§5).
- `locked_at` is `null` while replaceable, an ISO-8601 UTC timestamp once
  locked. **This is the only lock state; do not derive the lock from
  `roll.negatives` or `roll.runs`** — a negative can be removed, and a
  derived lock would silently unlock a roll whose pixels were already
  anchored.
- `populations` is the full detector output, kept as evidence and as the
  data §11 reads.

### 3.2 The state machine

Three states, and every transition:

```
       roll init
           │
           ▼
   ┌───────────────┐   roll set-base-frame (measures + gates)
   │    ABSENT     │ ─────────────────────────────────────────┐
   │ film_base=None│                                          │
   └───────────────┘                                          ▼
           │                                        ┌───────────────────┐
           │ run / stitch                           │     ATTACHED      │
           ▼                                        │  locked_at = null │
    error FILM_BASE_REQUIRED                        └───────────────────┘
                                                     │        │       ▲
                          run / stitch publishes ────┘        │       │
                          the roll's first negative           │       │
                                   │                          │       │
                                   ▼           roll set-base-frame ────┘
                          ┌───────────────────┐   (replaces freely)
                          │      LOCKED       │
                          │ locked_at = <ts>  │
                          └───────────────────┘
                                   │
                     roll set-base-frame → error FILM_BASE_LOCKED
```

Rules, stated so they can be implemented literally:

1. **`roll set-base-frame` on an ABSENT roll**: decode, measure, gate. On a
   gate failure, emit the error and change nothing. On success, write the
   block with `locked_at: null` and emit `base_frame_set`.
2. **`roll set-base-frame` on an ATTACHED roll**: identical. The previous
   block is overwritten. This is the "re-upload a different frame" path.
3. **`roll set-base-frame` on a LOCKED roll**: error `FILM_BASE_LOCKED`,
   message:
   > "this roll's film-base reference was locked on 2026-09-06 when its first
   > negative was converted and cannot be changed; create a new roll to use a
   > different base frame"
4. **`run` / `stitch` on an ABSENT roll**: error `FILM_BASE_REQUIRED` **before
   any pixel work**, message:
   > "this roll has no film-base reference; add one with the base-frame field
   > before converting scans (docs/REBATE_ANCHORING.md)"
5. **`run` / `stitch` on an ATTACHED roll**: proceed. In `_append_this_run`,
   at the moment the run's first negative is written, set
   `film_base["locked_at"] = _now_iso()`.
6. **`run` / `stitch` on a LOCKED roll**: proceed, using the locked block.
7. **A run that fails before publishing anything leaves the roll ATTACHED.**
   The lock is set alongside the negatives, in the same write, so this is
   automatic — do not set it earlier.

### 3.3 Where the run-time checks go

In `stitch_pipeline.run_stitch` (`stitch_pipeline.py:1121`):

- **Rule 4's check goes immediately after the roll manifest loads and its
  invariants are checked, before `_detect_all`.** The user must not wait
  through ten minutes of stitching to be told the roll has no base frame.
- The flat-field comparison (`FILM_BASE_FLATFIELD_CONFLICT`, §7.2) goes in
  the same place: warn when the run's `--flatfield` differs from the block's
  `flat_field_profile_id`.
- The camera comparison (`FILM_BASE_CAMERA_CONFLICT`) goes in
  `roll set-base-frame` itself when the roll already has a `camera_color`
  block, and in `run_stitch` otherwise — a fresh roll has no camera on record
  until its first run seeds one (`_seed_camera_color`,
  `stitch_pipeline.py:1522`).
- **Rule 5's lock write goes in `_append_this_run`** (`stitch_pipeline.py:1565`),
  in the same manifest write as the negatives.

### 3.4 One writer, deliberately

`roll set-base-frame` is the **only** thing that writes `film_base.density`.
`run` and `stitch` get no `--base-frame` flag.

The alternative — a `--base-frame` flag on `run` that attaches as a side
effect — was rejected because it would put the lock/replace logic in two
places and would let a user silently re-anchor a roll from a command whose
purpose is something else. CLI users call two commands; the app calls
`roll set-base-frame` when the field changes and `run` when the user hits
Convert.

---

## 4. Consumption (chunk B-5 — the only chunk that changes pixels)

### 4.1 `analyze_bounds` grows one optional argument

```python
def analyze_bounds(
    grid_log: np.ndarray,
    keep: np.ndarray,
    base_refs: tuple[float, ...] | None = None,
) -> Bounds:
```

`base_refs` is the roll's locked `film_base["density"]`. Inside, replace the
thin-end colour measurement:

```python
    # Colour pass. Thin end: the roll's measured film base
    # (docs/REBATE_ANCHORING.md §4), falling back to plain per-channel
    # percentiles of scene content. Only the deviation from the median
    # survives the recombination below, so the base frame's own exposure
    # cancels and never has to match the roll's (§0.2).
    if base_refs is not None and len(base_refs) == channels:
        c_ceils = [float(v) for v in base_refs]
    else:
        c_ceils = [
            _percentile(values[:, channel], 100.0 - BASE_COLOR_CLIP)
            for channel in range(channels)
        ]
```

Everything below that line is unchanged — `mean_cc = median(c_ceils)`, the
recombination, the finiteness and degeneracy guards. **Do not touch
`mean_lc`.** The level stays with the luma axis (§0.6).

The `len(base_refs) == channels` guard is what makes §5 work: on a mono roll
`channels == 1` and a 3-array `base_refs` silently falls back, which is the
correct behaviour.

### 4.2 The chroma anchor gets better for free

`_same_pixel_color_floor_refs` already receives `base = np.asarray(c_ceils)`
and uses it as the anchor for its chroma measurement. With §4.1 in place it
receives the *real* base instead of a scene percentile, so the dense-end
near-neutral gate measures chroma against the actual orange mask.

**No code change here — but a re-check is required.** `NEUTRAL_CHROMA_CAP`
(0.29) and `NEUTRAL_FIRST_PASS_CAP` (0.55) were calibrated against the old,
scene-derived anchor. §11 step 4 measures the shift; if the gate's acceptance
rate moves materially, the caps are re-pinned at the same user gate.

### 4.3 Threading it through

`composite.composite()` (`cli/src/scanny_boy/composite.py:628`) already takes
`reference_bounds: list[Bounds] | None` for the clamp. Add a sibling:

```python
    base_refs: tuple[float, ...] | None = None,
```

and pass it at the call site (`composite.py:888`):

```python
    bounds = analyze_bounds(grid, keep, base_refs)
```

`stitch_pipeline` passes the locked density down beside the
`reference_bounds` it already computes with `_reference_bounds`
(`stitch_pipeline.py:1037`).

**Interaction with `clamp_bounds`:** with a fixed anchor, every negative's
`ceils` deviations become nearly identical across the roll, so the population
MAD collapses toward zero. `CLAMP_MIN_WINDOW = 0.5` floors the window, so the
clamp stays inert on legitimate exposure variation. No change needed — but
put that sentence in a comment beside the clamp so a future reader does not
"fix" the now-tiny MAD.

---

## 5. Monochrome rolls

On a monochrome roll `collapse_to_mono` reduces the image to one channel
before `analyze_bounds` runs, and with one channel the colour deviation is
identically zero. There is no orange mask to anchor and nothing for a base
frame to contribute.

1. **The base frame is still required.** One rule, no branches. The film kind
   is not known until the first run's detector pre-pass, so a conditional
   requirement would have to be evaluated after that decision — and a roll
   that skipped the base frame because the detector guessed monochrome, then
   turned out to be colour, could never be anchored without starting over.
2. **It is measured and recorded as three channels**, exactly as on a colour
   roll. The block is evidence and provenance; recording it costs nothing.
3. **It is not consumed.** §4.1's `len(base_refs) == channels` guard makes a
   3-array fall back on a 1-channel image without a special case.
4. Gate 5 (`FILM_BASE_TOO_DARK`) still applies per channel even though only
   the merged channel is published. A silver negative's base is near-neutral,
   so no channel is disadvantaged and the gate is easy to pass.

---

## 6. Drift evidence, recorded and acted on by nothing

Two numbers per negative, added to the `normalization` block by
`stitch_pipeline._normalization_record` (`stitch_pipeline.py:979`) whenever
**both** the roll has a locked anchor **and** that negative's own
`detect_rebate` fired with an unclipped `base_density`:

```python
        # REBATE_ANCHORING §6: how this negative's own rebate compares with
        # the roll's locked anchor. `level_offset` is the common-mode
        # difference — capture-exposure drift across the roll, the quantity
        # §12's deferred level-anchoring decision needs. `shape_residual` is
        # the largest per-channel disagreement AFTER removing that common
        # mode: the direct measure of whether the roll anchor is right.
        # Recorded, read by nothing.
        "base_check": {
            "level_offset": ...,   # median(neg) - median(roll)
            "shape_residual": ..., # max_ch |(neg[ch]-median(neg)) - (roll[ch]-median(roll))|
        },
```

Absent whenever either input is missing.

Why this is not optional: §0.5's cost is that a bad anchor is a per-roll
failure. `shape_residual` is the only thing in the record that would expose
one after the fact, and it is free — both quantities are already measured.

A future `FILM_BASE_DRIFT` warning could fire on a large `shape_residual`,
but **do not add it in this plan**: there is no measured distribution to set
a threshold against. Record first, threshold later, per the house rule.

---

## 7. CLI, contract, schema

### 7.1 The new command

Added to the `roll` subparser group (`cli/src/scanny_boy/cli.py:106`),
following `roll rename` (`cli.py:123`) as the precedent for a subcommand that
mutates roll state outside a run:

```python
    roll_set_base = roll_subparsers.add_parser(
        "set-base-frame",
        help="Attach or replace the roll's film-base reference frame.",
    )
    roll_set_base.add_argument("--roll", required=True, metavar="DIR")
    roll_set_base.add_argument("--frame", required=True, metavar="FILE")
    roll_set_base.add_argument("--flatfield", metavar="PROFILE_ID")
```

Emits one `base_frame_set` event on success:

```json
{
  "event": "base_frame_set",
  "roll_id": "…",
  "source_name": "_DSC5012.NEF",
  "density": [-0.4213, -0.1187, -0.9902],
  "area_fraction": 0.44,
  "population_count": 2,
  "locked": false
}
```

`roll info` reports the whole `film_base` block verbatim, `null` when absent.

### 7.2 New event codes

Add to `Code` in `cli/src/scanny_boy/events.py:122` and document each in
`shared/contract/CONTRACT.md`'s code tables.

Errors:

- `FILM_BASE_REQUIRED` — the roll has no film-base reference; `run`/`stitch`
  refuse.
- `FILM_BASE_LOCKED` — the reference is locked and cannot be replaced.
- `FILM_BASE_NOT_FOUND` — no flat rebate population found in the frame.
- `FILM_BASE_TOO_SMALL` — the chosen population is below the area or cell
  floor.
- `FILM_BASE_CLIPPED` — the rebate is sensor-clipped.
- `FILM_BASE_TOO_DARK` — a channel's median inside the rebate is below
  `FILM_BASE_MIN_CHANNEL`.
- `FILM_BASE_AMBIGUOUS` — two large flat populations of different density.
- `ROLL_PREDATES_FILM_BASE` — this roll was stitched before film-base
  anchoring and cannot take new negatives (§9).

Warnings:

- `FILM_BASE_CAMERA_CONFLICT` — the base frame's EXIF camera model differs
  from the roll's. Warning, not an error: the measurement may still be fine.
- `FILM_BASE_FLATFIELD_CONFLICT` — the run's flat-field profile differs from
  the one the base frame was measured with.

### 7.3 Contract and schema

- `shared/contract/CONTRACT.md`: bump the protocol version **12 → 13**, and
  add a "**The film-base reference**" section at the top describing the
  command, the block, the state machine, the ten codes, the `base_frame_set`
  event and the invalidation of older rolls. Follow the shape of the existing
  protocol-11 monochrome section.
- `shared/contract/schema.json`: the `base_frame_set` event; the new codes.
- `shared/contract/roll-manifest.schema.json`: bump
  `manifest_format_version` `const` **7 → 8**; add the `film_base` property
  (nullable object, `additionalProperties: false`, `density` a 3-number
  array, `locked_at` `["string", "null"]`, `populations` an array of the
  §3.1 shape); add `base_check` to the per-negative `normalization` block.

---

## 8. The Mac app

### 8.1 The Add Scans field

A new required field on the Add Scans sheet, above the existing flat-field
picker (so the reading order is: base frame → flat-field → grid → files):

- **Label:** "Film base reference"
- **Empty state:** a file well with "Choose base frame…" and the §1 capture
  instruction as visible help text, not a tooltip. It must say, in this
  order: show as much clear rebate as you can; some picture in the frame is
  fine; **expose about two stops darker than your scans, so the rebate sits
  near the middle of the camera's histogram**; don't go past three stops.
- **Attached, unlocked:** the file name, the measured density, the rebate's
  area fraction as a percentage ("rebate: 44% of frame"), and a "Replace…"
  button.
- **Locked:** the same summary, the lock date, no Replace button, and one
  line: "locked when this roll's first negative was converted."
- **Convert is disabled** while the field is empty, with the reason shown —
  the same treatment the sheet already gives a missing flat-field profile.
- Choosing a file calls `roll set-base-frame` immediately and shows the
  result or the gate error inline. Do not defer validation to Convert; the
  point of the separate command (§3.4) is that the user finds out in two
  seconds.

### 8.2 Files

1. **`Roll.swift` / `RollManifest.swift`** — decode `film_base`; add
   `filmBase` to the roll model with `density`, `lockedAt`, `sourceName`,
   `populations`.
2. **`CLICommand`** (`mac/ScannyBoy/CLIBridge/`) — a `.rollSetBaseFrame`
   case.
3. **`ConfigurationModel.swift`** — `baseFrameURL`, the call to
   `roll set-base-frame`, the inline error, and `runEnabled`
   (`ConfigurationModel.swift:236`) gaining the base-frame condition.
4. **A `BaseFrameField` view** in `RunSubviews.swift`, and its placement in
   the Add Scans sheet.
5. **`CLICode+FriendlyNames.swift`** — friendly names for the ten new codes.
6. **`NewRollSheet.swift`** — unchanged; the base frame is attached on the
   Add Scans stage, not at roll creation, because the user may not have shot
   it yet when they create the roll.

---

## 9. Invalidating existing rolls

The user has decided that rolls stitched before this feature are thrown away
rather than migrated. That removes every compatibility shim this plan would
otherwise need — in particular there is **no** `upgrade_stitch_params`, and
`_stitch_params_for_invariant_check` (`roll_manifest.py:617`) is not touched.

Version bumps:

| thing | from | to |
|---|---|---|
| `CONTRACT.md` protocol | 12 | 13 |
| `roll_manifest.ROLL_MANIFEST_FORMAT_VERSION` | 7 | 8 |
| `roll-manifest.schema.json` `manifest_format_version` const | 7 | 8 |
| `film_base.FILM_BASE_MEASURE_VERSION` | – | 1 |

`normalization.NORMALIZE_FORMAT_VERSION` **does not change**: no constant in
`normalization.build_params()` is added or altered, and `analyze_bounds` with
`base_refs=None` is byte-identical to today.

**What "invalidated" means precisely.** An old roll:

- **still loads.** `load_roll_manifest` accepts
  `manifest_format_version` 7 and 8.
- **is still editable and exportable.** Its published TIFFs carry their own
  `floors`/`ceils` in their own `normalization` blocks, so
  `decode_normalized`, the preview, the tone/colour ops and the export all
  keep working exactly as before. Nothing the user has already made is lost.
- **cannot take new negatives.** `run` and `stitch` on a roll whose
  `manifest_format_version < 8` fail with `ROLL_PREDATES_FILM_BASE`:
  > "this roll was stitched before film-base anchoring; create a new roll and
  > re-stitch its scans to add more negatives"
- **cannot be given a base frame.** `roll set-base-frame` fails with the same
  code — retrofitting an anchor onto a roll whose negatives were normalized
  without one would make the roll internally inconsistent, which is the exact
  failure §0.5's lock exists to prevent.

Check the version once, at the top of `run_stitch` and of the
`set-base-frame` handler, before anything else.

**Alternative if you would rather be harder:** make `load_roll_manifest`
reject version 7 outright. That is one line, but it takes the Edit and Export
tabs down with it for rolls the user may still want. The recommendation above
is the kinder default; say the word and it collapses.

---

## 10. Chunks

Each chunk is one branch and one pull request, merged in order
(`DECISIONS.md`, "Product and repository"). Each must be green on its own.

### B-1 — the detector module

**Files:** `cli/src/scanny_boy/film_base.py` (new),
`cli/src/scanny_boy/events.py`.

**Do:** §2.1's constants; `Population`, `BaseMeasurement`, `FilmBaseError`;
`measure`, `gate`, `load`, `build_params` (§2.2–§2.5). Add the ten codes from
§7.2 to `events.Code` — codes may exist before anything raises them.

**Tests:** new `cli/src/scanny_boy/film_base_test.py`, fast tier, on
synthetic arrays:

- a uniform field with a known per-channel offset returns exactly that offset
  and passes `gate`;
- **the exposure-invariance property**: the same field scaled by 0.5 and by
  0.125 produces `density` values whose *deviations from their own median*
  agree to within 1e-5. This test is the executable form of §0.2 and must not
  be deleted;
- **two separated rebate bands with picture between them** merge into one
  population of the summed area (§2.2 step 5) — the common case;
- a field that is 100% rebate yields one population covering the whole grid
  and passes (the case `detect_rebate` cannot handle, §0.3);
- a small bright sliver (bare light) plus a large rebate region: the rebate
  wins, and the sliver is recorded as a second, thinner population;
- a *large* bright region plus a similar-sized rebate region fails
  `FILM_BASE_AMBIGUOUS`;
- a rebate region under 20% of the frame fails `FILM_BASE_TOO_SMALL`;
- a clipped rebate fails `FILM_BASE_CLIPPED`;
- a rebate whose blue channel sits below `FILM_BASE_MIN_CHANNEL` fails
  `FILM_BASE_TOO_DARK` **while luma is still comfortably above it** — this is
  the test that proves the gate is per-channel and not a luma test (§1.2);
- a single hot pixel does not change `density`.

**Green when:** `film_base_test.py` passes; nothing else calls the module.

### B-2 — roll state and the command

**Files:** `cli/src/scanny_boy/roll_manifest.py`,
`cli/src/scanny_boy/library/models.py`, `cli/src/scanny_boy/library/repo.py`,
`cli/src/scanny_boy/cli.py`, `shared/contract/CONTRACT.md`,
`shared/contract/schema.json`,
`shared/contract/roll-manifest.schema.json`.

**Do:** the `film_base` field and its persistence (§3.1); the
`roll set-base-frame` command and the `base_frame_set` event (§7.1); §3.2's
rules 1–3; the contract, schema and version bumps (§9). `run`/`stitch` are
untouched and still work without a base frame.

**Tests:** `cli_test.py` / `roll_manifest_test.py` — attach on an absent
roll; replace on an attached roll; `FILM_BASE_LOCKED` on a locked one; a gate
failure changes nothing on disk; round-trip the block through
`to_dict`/`from_dict`/`repo`; the event validates against `schema.json`.

**Green when:** the block round-trips, the three rules have tests, and the
schema-conformance suite passes.

### B-3 — the run-time gate and the lock

**Files:** `cli/src/scanny_boy/stitch_pipeline.py`,
`cli/src/scanny_boy/probe.py`, `cli/src/scanny_boy/cli.py`.

**Do:** §3.2's rules 4–7; §3.3's placement, including the two conflict
warnings; §9's `ROLL_PREDATES_FILM_BASE` version check on both entry points;
`probe --roll` reporting `film_base` so the app can gate Convert without
starting a run.

**Tests:** `stitch_pipeline_test.py` — `run`/`stitch` on an absent roll fail
`FILM_BASE_REQUIRED` **before any intermediate is read** (assert on the
absence of progress events, not just the code); a successful run sets
`locked_at`; a run that fails mid-way leaves `locked_at` null; a run whose
`--flatfield` differs warns; a version-7 roll fails
`ROLL_PREDATES_FILM_BASE`.

**Green when:** the state machine is fully covered and published pixels are
still unchanged.

### B-4 — the drift evidence

**Files:** `cli/src/scanny_boy/stitch_pipeline.py`,
`shared/contract/roll-manifest.schema.json`.

**Do:** §6's `base_check` sub-block.

**Tests:** `stitch_pipeline_test.py` — present with correct arithmetic when
both inputs exist; absent when the roll has no locked anchor, when the
negative's rebate did not fire, or when it was clipped.

**Green when:** published pixels are still unchanged and the manifest carries
the comparison. **Stop here and run §11.**

### B-5 — consume the anchor (gated on §11)

**Files:** `cli/src/scanny_boy/normalization.py`,
`cli/src/scanny_boy/composite.py`, `cli/src/scanny_boy/stitch_pipeline.py`,
`docs/DECISIONS.md`, plus any thresholds §11 re-pinned.

**Do:** §4.1's `base_refs` argument and branch; §4.3's threading and comment.
Add a "**Rebate anchoring**" subsection to `DECISIONS.md`'s "Normalization
decisions" recording the locked decisions.

**Tests:**

1. `normalization_test.py` — `analyze_bounds` with `base_refs=None` returns
   exactly what it returns today (a regression lock against a stored
   expectation);
2. `normalization_test.py` — with `base_refs` given, `ceils` deviations equal
   the base's deviations and `mean_lc` is unchanged; **and adding a constant
   to every entry of `base_refs` does not change the result** — the §0.2
   property asserted at the consumption site as well as the measurement site;
3. `normalization_test.py` — a 3-array `base_refs` on a 1-channel image falls
   back silently (§5);
4. `composite_test.py` — a composite with `base_refs=None` is byte-identical
   to the pre-change output for a stored fixture;
5. `stitch_pipeline_test.py`, slow tier — a real roll stitched with an anchor
   differs from the same roll stitched without one, and the difference is
   confined to the per-channel `ceils` deviations.

**Green when:** all five pass and the user has approved §11's numbers.

### B-6 — the Mac app

**Files:** `mac/`, per §8. May be developed in parallel with B-2…B-4; must
land no later than B-3.

**Tests:** `ConfigurationModelTests` — `runEnabled` is false without a base
frame; the `set-base-frame` command shape; `RollLibraryTests` — the block
decodes, including `lockedAt`; `CLICommandTests` — argument shape.

### B-7 — fixtures and tooling

**Files:** `cli/tools/generate_base_frame_dng.py` (new),
`tests/fixtures/base-frame/`.

**Do:** a generator for a synthetic base frame — flat orange field, two
rebate bands with a picture strip between them, an optional bright sliver —
modelled on `cli/tools/generate_bare_light_dng.py`. Commit the output so the
fast tier has a realistic fixture, per `AGENTS.md`'s fixture rules.

May land any time after B-1; B-3's and B-5's slow-tier tests want it.

---

## 11. The measurement gate — run this between B-4 and B-5

Nine thresholds in §2.1 are provisional. This is how they get pinned. **B-5
must not merge until the user has seen these numbers and approved them.**

**Step 1 — shoot the frames.** For at least three rolls already in the
library, covering at least two film stocks, shoot base frames:

- one "easy" frame per roll (mostly clear leader);
- one "realistic" frame per roll (two rebate bands with picture between);
- for **one** roll, the same framing at 1, 2, 3 and 4 stops down;
- a set of deliberate failures: sprocket holes in view, bare light along one
  edge, a frame at the roll's own scanning exposure (probably clipped), a
  frame with only a sliver of rebate.

**Step 2 — verify the exposure-invariance claim (§0.2) on real film.** Run
`film_base.load` on the four-exposure set. Report `luma` for each exposure
and the three per-channel *deviations from their own median* for each. The
deviations must agree across exposures to well within
`FILM_BASE_MAX_COMPONENT_SPREAD`. **If they do not, stop — the premise of
the feature is wrong and §4.1 must not land.** The likely culprits would be
sensor non-linearity near clipping or stray light; both are guarded by the
"stop down" capture rule, which is why the 1-stop sample is in the set.

**Step 3 — pin the detector and the gates.** For every frame in step 1,
report the full `populations` list — area fraction, luma, spread — plus the
per-channel densities and clipped fractions of the chosen one. Pin
`FILM_BASE_MAX_COMPONENT_SPREAD`, `FILM_BASE_MERGE_SEPARATION`,
`FILM_BASE_MIN_AREA_FRACTION` and `FILM_BASE_AMBIGUOUS_RATIO` to sit clear of
both clusters, the way `MONO_CHROMA_MAX` / `COLOUR_CHROMA_MIN` were pinned
from a measured gap. Confirm every deliberate failure is caught by the
intended gate — especially that the bare-light frame does **not** silently
win step 7's "largest population" rule.

**Step 4 — pin `FILM_BASE_MIN_CHANNEL` from the blue channel.** From the
four-exposure set, report the blue median inside the rebate at each exposure
and the noise (MAD) of the per-channel medians across repeated shots at the
same exposure. Set the floor where the MAD starts to matter against
`FILM_BASE_MAX_COMPONENT_SPREAD`, and confirm the §1 guidance ("two stops,
not more than three") sits comfortably above it on every stock tested.

**Step 5 — check the dense-end neutral gate (§4.2).** For one roll, run
`analyze_bounds` with and without `base_refs` and report, per negative, how
often `_same_pixel_color_floor_refs` returned `None` (the fallback) in each
case, and the median chroma of the surviving neutral set. If either moves
materially, re-pin `NEUTRAL_CHROMA_CAP` and `NEUTRAL_FIRST_PASS_CAP` at this
same gate.

**Step 6 — check the anchor against the per-negative rebate (§6).** For every
negative in the sample rolls where `detect_rebate` fired unclipped, report
`base_check.shape_residual`. This is the direct test of whether the roll
anchor agrees with what the negatives themselves show, and it is also the
data a future `FILM_BASE_DRIFT` threshold would need.

Record every pinned number, with its date and the measurement it came from,
in the constant's own comment in `film_base.py` — the way `MONO_CHROMA_MAX`'s
comment does.

---

## 12. Rejected alternatives

- **Anchoring the *level* as well as the colour.** Setting
  `ceils[ch] = base[ch]` outright would give a physically anchored white
  point. Rejected for v1 because it is not exposure-invariant: any capture
  drift between the base frame and a negative — a dimming panel, a changed
  aperture, a session stitched weeks later — would shift every negative's
  white point by the drift. §6 records `level_offset` precisely so this can
  be revisited from data instead of argument.
- **Consuming the per-negative `Rebate.base_density` directly.** §0.3: absent
  on many negatives, and would make the anchor vary within a roll — the
  failure `clamp_bounds` exists to prevent.
- **Extending `detect_rebate` to handle a mostly-rebate frame.** Would mean
  weakening the separation gate that makes "no rebate" return cleanly on a
  real negative. A separate detector with its own gates is cheaper and safer.
- **Requiring the base frame to be pure rebate.** Simpler to measure (a
  whole-frame median with flatness gates) but the user's film does not always
  offer it. §0.4.
- **Choosing the *thinnest* population instead of the largest.** Bare light
  and sprocket holes are thinner than base, so this picks exactly the wrong
  thing whenever the film edge is in frame.
- **A `--base-frame` flag on `run`/`stitch`.** §3.4: two writers, two places
  for the lock logic, and a silent re-anchor from a command whose purpose is
  something else.
- **A library-level base profile, like the flat-field profile.** Film base is
  a property of the roll, not the rig. Two rolls of different stock share a
  copy stand but not a mask.
- **Correcting for exposure difference from EXIF.** Unnecessary given §0.2,
  and it would import reciprocity and metering error into a measurement that
  currently has neither.
- **Migrating existing rolls.** The user has chosen invalidation (§9), which
  removes the `stitch_params` forward shim entirely.

---

## 13. Risks and known failure modes

1. **A bad base frame poisons a whole roll (§0.5).** Mitigations: §2.3's
   gates, the unlocked window that lets the user replace it after seeing the
   summary, and §6's recorded residual. Accepted deliberately.
2. **The user shoots the base frame at the roll's exposure and it clips.**
   Caught by gate 4 with a message naming the remedy in stops. This is the
   most likely user error, and the error text matters more than the code.
3. **The user overshoots and stops down too far.** Caught by gate 5, which is
   per-channel because blue through the mask runs out first (§1.2).
4. **Bare light wins the "largest population" rule.** Only possible when bare
   light covers more of the frame than the rebate does, which the capture
   instruction and gate 6 both target. §11 step 3 must confirm it on a real
   frame with bare light in it.
5. **The base frame is not from that roll.** Undetectable. The UI text is the
   only defence; §6's `shape_residual` is the after-the-fact evidence.
6. **A mono roll's user resents shooting a frame that is then ignored.**
   Accepted (§5); the alternative is a roll that can never be anchored if the
   detector guessed wrong.
7. **Flat-field profile changes mid-roll.** `--flatfield` is chosen freely
   per run (`ConfigurationModel.swift:236` — "the roll does not lock to one"),
   and the gain map is normalised per channel to mean 1, so its effect on a
   median is second-order. Warned, not gated. If §11 step 2 shows otherwise,
   promote it to a roll invariant.
8. **`analyze_bounds`' degeneracy guard.** If a measured base ever produced
   `ceils[ch] <= floors[ch]`, `analyze_bounds` already raises
   `NORMALIZE_DEGENERATE_BOUNDS`. Correct behaviour, no new handling — but
   with an anchor it becomes possible for a *badly exposed negative* rather
   than a bad base frame to trip it, so keep that message as informative as
   it is today.
