# Monochrome plan: detect the film, merge the channels

Two changes to the normalization path, in order. First a **detector** that
decides, per roll, whether the film is a silver black-and-white negative or a
colour negative, recording its evidence and acting on nothing. Then a
**collapse** that merges the three channels into one before the bounds
analysis, so a monochrome roll publishes single-channel TIFFs.

This plan follows the conventions of `docs/GEOMETRIC_PLAN.md`,
`docs/FLATFIELD_PLAN.md` and `docs/STITCH_QUALITY_PLAN.md`: every constant
lives in exactly one module, every threshold that shapes an output is
recorded in the roll manifest so a record can be read without knowing which
build wrote it, and **no threshold is pinned without a measurement the user
has approved**. Section 1 exists precisely to produce that measurement.

---

## 0. Why this shape, and why in this order

### 0.1 Where normalization actually lives

The convert (`prepare`) stage decodes RAW, flat-fields, and writes a
**linear** base TIFF per frame; the only normalization thing it touches is
`measure_clip_fractions` (`pipeline.py:480`). The transfer, the meters and
the encode all run in the **stitch** stage, fused into the composite's encode
at `composite.py:711-744`. Everything below is therefore stitch-stage work.

### 0.2 The collapse goes between the log and the bounds

Insert at `composite.py`, after `to_log_density` (line 711) and before
`analyze_bounds` (line 718).

- **Not before the log.** Averaging in linear light weights by intensity, not
  by density. On a negative that biases the merge toward the film base.
- **Not after `analyze_bounds`.** The colour axis would first solve for an
  orange mask that is not there. `_same_pixel_color_floor_refs`' chroma gate
  is meaningless on a mono negative and can only inject noise into the
  bounds. The gate still runs on the collapsed image — see §4's one-channel
  path for it — but on one channel its chroma is identically zero and it
  degenerates harmlessly.
- **Not after the encode.** That merges quantized codes, and the manifest's
  `floors`/`ceils` would then describe three channels the file no longer has.

Collapsing here preserves the invariant that the manifest's recorded bounds
correspond one-for-one to the published channels, so `decode_normalized`
stays the single inverse, unchanged.

### 0.3 The decision belongs to the roll, not the negative

A roll is one film stock. A per-negative decision will eventually flip on a
snow scene, a fogbank, or a frame of a grey wall, and produce a roll where
negative 14 is single-channel and the rest are not. That is the same failure
`clamp_bounds` already exists to prevent for bounds, and `_reference_bounds`
(`stitch_pipeline.py:658`) is the pattern to copy.

Once frozen for a roll, the decision **never changes** except by re-stitching
the whole roll. A later run whose evidence contradicts the frozen decision
warns; it does not flip.

### 0.4 Ordering is mandatory

§1 before §2, because §2's threshold must be pinned from §1's recorded
numbers on real rolls, not guessed. §2 before §3, because the collapse must
never run on a decision that has not been frozen and recorded. §4 (the
one-channel plumbing) can be developed in parallel with §2 but must land
before §3 is switched on. §5.1's shim lands before §2, so that §2's
thresholds can enter `build_params()` without breaking existing rolls (§5.1).

---

## 1. The detector, recorded and acted on by nothing

### 1.1 The statistic

A silver B&W negative photographed through a Bayer CFA under white light
gives three channels recording *the same* image, differing only by a
per-channel gain and offset — the CFA passband times the light times silver's
near-neutral absorption. Remove that affine and the channels are identical to
within noise. A colour negative's channels are not, whatever affine you
remove.

Removing the affine first is load-bearing. Without it, the orange mask is an
enormous per-channel offset that swamps everything and the statistic says
nothing.

In `normalization.py`, on the log-density image:

```python
# Per channel: subtract its median, divide by its MAD. What survives is
# the part of the channel that is not a CFA gain or a film-base offset —
# on a silver negative, noise; on a colour negative, the picture.
# The MAD floor keeps a near-constant channel (a dense or heavily
# rebate-dominated frame — likely, because §1.2 deliberately includes the
# rebate) from dividing by ~zero.
resid  = (img_log - median_ch) / np.maximum(mad_ch, MONO_MAD_FLOOR)
chroma = resid.max(axis=-1) - resid.min(axis=-1)
statistic = percentile(chroma, MONO_CHROMA_PERCENTILE)   # start at 90.0
```

`MONO_MAD_FLOOR` (log10 density units; start at `1e-3`) and its behaviour
when it trips: a channel whose MAD sits below the floor carries no usable
spread. If the channel is genuinely flat, its numerator is flat too and the
floor costs nothing — the residual stays ~zero and contributes no chroma. If
the channel has real structure under a vanishing MAD, the inflated residual
pushes the statistic **up**, toward "colour" — the lossless direction for a
misclassification.

A **high percentile**, not a mean: a colour negative of a mostly-neutral
scene still has colour somewhere, and the mean drowns it.

**Alternative considered — per-pair channel correlation** (what NegPy's
autodetect uses). Weaker. Correlation is scale-invariant so it removes the
affine for free, but it measures *shape* agreement only, and a low-colour-
variance colour scene correlates near 1.0 too. Record it alongside as
`channel_correlation` if you want the diagnostic; decide on the chroma
percentile, which measures magnitude.

### 1.2 Where it runs

A dedicated pre-pass at the top of the stitch run — **before the solve
loop**, not inside `composite_and_normalize`. Mono-ness is a property of the
film, visible in any single frame, so it does not need the composite, and
running it before compositing avoids ever having to re-normalize a negative
because the decision arrived late.

At §1 the pre-pass only records, so its exact position is free. §2.3 pins it
earlier than that: **before the roll's invariants are seeded**
(`stitch_pipeline.py:854-862`), because the seeded published-ICC record is
film-kind-dependent (§2.3). That is earlier than the flat-field geometry
check, too.

New function in `normalization.py`:

```python
def measure_mono_statistic(linear: np.ndarray) -> MonoStatistic
```

taking one staged **linear** intermediate (as `_read_intermediate` returns
it), doing its own `to_log_density` and block-median decimation.

Sample **up to `MONO_DETECT_MAX_SAMPLES = 6` negatives**, one frame each
(the group's first member), spread evenly across the roll's canonical order.
Six bounded reads, not one per negative.

**Do not exclude the rebate on this pass.** On colour film the rebate is the
orange mask at full strength — the single strongest mono/colour discriminator
available — and on B&W film the base is near neutral. Including it widens the
separation between the two classes. This is the opposite of what
`analyze_bounds` wants, and is deliberate. (It also makes a near-constant
sample channel more likely — hence §1.1's MAD floor.)

### 1.3 What gets recorded

Per sampled negative, in its `normalization` block beside the other section
3.7 meters (`shadow_refs`, `anchor`, `textural_range` — all "recorded, never
acted on"):

```json
"mono": { "chroma": 0.0000, "channel_correlation": [0.0, 0.0], "sampled": true }
```

Add the `mono` object to the `normalization` definition in
`shared/contract/roll-manifest.schema.json` (line 111). The object has no
`additionalProperties: false`, so an unlisted key validates today, but the
record belongs in the schema.

**Nothing else in §1.** The roll's top-level `film` block is §2's: it carries
`kind`, which requires the threshold §1.4 forbids pinning and §2 forbids
starting on. §1 writes the per-negative statistic and nothing more.

### 1.4 Ship this alone

Land §1 with **no behaviour change of any kind**: no collapse, no decision,
no new CLI flag. Run it over every roll in the library, collect the numbers, and
only then pin §2's threshold. This is the measurement the house rule requires.

§1's constants (`MONO_CHROMA_PERCENTILE`, `MONO_DETECT_MAX_SAMPLES`,
`MONO_MAD_FLOOR`) stay **out of `build_params()`** at this step: they shape
recorded evidence only, never published output, and the per-negative `mono`
block is not an invariant input. `build_params()` gains keys in §2 and §3,
when constants that shape output arrive — §5.1's shim, landed before §2,
absorbs that break (§5.1).

### 1.5 Tests (`normalization_test.py`)

- A synthetic 3-channel image built as `base + per-channel affine` of one
  plane scores near zero, for several affines including a strong offset that
  stands in for the orange mask.
- The same image plus per-channel independent noise scores near zero.
- A synthetic image with genuine per-channel content scores well above it.
- The statistic is invariant to a per-channel gain and offset applied to the
  input (this is the property the whole design rests on — assert it directly).
- A near-constant channel (MAD below the floor, flat content) scores near
  zero; a channel with real structure under a near-zero MAD pushes the
  statistic up (the §1.1 floor behaviour).
- `--slow`: each real sample NEF (colour) scores **above every synthetic mono
  fixture's score under the same statistic** — a relative assertion against
  §1.5's own fixtures, not against a colour band (no threshold exists until
  §2 pins one).

---

## 2. The roll decision

**Do not start until §1's numbers exist and the user has approved a
threshold.**

### 2.1 The gate

```python
# Pinned from measurements over the library on <date>; see §1.4.
MONO_CHROMA_PERCENTILE = 90.0
MONO_CHROMA_MAX = <measured>          # at or below -> monochrome
COLOUR_CHROMA_MIN  = <measured>       # at or above -> colour
```

Two thresholds with a deliberate gap. A roll whose median sample lands
**between** them is ambiguous: default to **colour** — the lossless choice, a
colour roll is never wrong to publish as three channels — and emit
`MONO_DETECT_AMBIGUOUS` with the measured value so the user can override.

Decide on the **median** of the samples, not the mean.

These are correctly called colour, and the plan does not try to be clever
about them: chromogenic B&W (XP2, BW400CN) is dye-based C-41 with a real
orange mask, and pyro/PMK-stained negatives carry a genuine stain. Both
should normalize down the colour path.

These thresholds **do** shape output (they gate the collapse), so at this
step they enter `build_params()`, under §5.1's format bump and shim.

### 2.2 The override

Add `--film-kind {auto,colour,monochrome}` to `stitch` and `run`, defaulting
to `auto`. A manual value skips the detector's decision (but not its
measurement — keep recording the statistic either way, it is free evidence).
Update `shared/contract/CONTRACT.md` and `schema.json`.

### 2.3 Freezing it

New top-level block in the roll manifest, and in
`shared/contract/roll-manifest.schema.json`:

```json
"film": {
  "kind": "colour",              // "colour" | "monochrome"
  "source": "auto",              // "auto" | "manual"
  "statistic": 0.0000,           // the deciding median, null when manual
  "samples": ["neg-003", "..."], // which negatives were measured
  "detector_version": 1
}
```

Written on the roll's **first** stitch run and never rewritten. On every
later run:

- Re-measure and record the per-negative statistic as usual.
- The frozen `kind` is **read from the manifest on load and fed into this
  run's candidate** before its `RollInvariants` are built
  (`stitch_pipeline.py:854-862`). It is **not** a plain `RollInvariants`
  field compared by `check_roll_invariants` (`roll_manifest.py:526`): that
  function raises on any mismatch and has no auto/manual notion, so run 2's
  re-measurement disagreeing would raise `ROLL_INVARIANT_MISMATCH` — exactly
  the flip-vs-raise behaviour §0.3 forbids as a raise. Instead, the fresh
  median is compared against the frozen `kind` separately, and on
  disagreement emits `MONO_DECISION_CONFLICT` as a **warning** and proceeds
  with the frozen kind. It must not flip: half the roll is already published.
- `--film-kind` naming the *other* kind on a roll that already **has runs**
  is an **error** (`ROLL_INVARIANT_MISMATCH`), with the message saying the
  roll must be re-stitched from scratch to change film kind. The guard is
  "has runs", not "has published negatives": `check_roll_invariants`
  early-returns on `not manifest.runs` (`roll_manifest.py:539`), so that is
  the invariant's unseeded state. The mismatch surfaces through the
  published-ICC invariant below, which is what a kind flip changes.

Two consequences to state plainly:

- **The published ICC profile is itself a roll invariant, and it becomes
  film-kind-dependent.** `published_icc_profile_sha256` is compared at
  `roll_manifest.py:552` and seeded from `profile_record(ProfileKind.DENSITY)`
  at `stitch_pipeline.py:861` and `roll_manifest.py:439`. Tagging a mono roll
  `DENSITY_GREY` (§4) makes that seed depend on the frozen kind, so the
  detector pre-pass must run **before the invariants are seeded** — at the
  top of `run_stitch`, earlier than §1.2's "before the solve loop" and
  earlier than the flat-field geometry check. A mono roll's candidate seeds
  `DENSITY_GREY`; a colour roll's, `DENSITY`; a v1 manifest with no `film`
  block is treated as colour (§5.2).

`film.kind` joins the data the invariant system protects — but via the
candidate-seeding path above, not as a compared field.

### 2.4 Tests (`stitch_pipeline_test.py`, `roll_manifest_test.py`)

- A fresh roll writes `film` on its first run and does not rewrite it on the
  second.
- Contradicting evidence on run 2 warns (`MONO_DECISION_CONFLICT`) and does
  not flip the kind.
- `--film-kind monochrome` on a roll with runs of the other kind errors
  (`ROLL_INVARIANT_MISMATCH`).
- A mono roll's seeded `published_icc_profile_sha256` is the `DENSITY_GREY`
  record's; a colour roll's is `DENSITY`'s.
- An ambiguous statistic resolves to colour and warns.
- A pre-`film` roll manifest (no block) loads and is treated as colour — see
  §5.2.

---

## 3. The collapse

### 3.1 The merge

Offset-aligned, inverse-variance-weighted mean in log density. In
`normalization.py`:

```python
def collapse_to_mono(img_log: np.ndarray) -> np.ndarray
```

1. Per channel, subtract its median **over the covered pixels**. `covered`
   (`composite.py:703`) is available at the insertion point; the analysis
   region is not — `keep` is built after the collapse
   (`composite.py:715-717`), and deriving it first would mean computing
   `grid`, `keep`, `rebate` and `dense_border` three-channel and recomputing
   them post-collapse, with pre-collapse rebate masks feeding post-collapse
   bounds. "Covered" is the region that exists.
2. Weighted sum, weights `MONO_MERGE_WEIGHTS`.
3. Add back the **weighted mean of the three channel medians**.

Step 1 is not optional: without it you are averaging quantities on different
scales, and the weights conflate "undo the CFA gain" with "combine the
estimates".

Step 3 is not cosmetic either, but be precise about what it buys.
`detect_rebate`'s `REBATE_DENSITY_TOLERANCE`,
`withhold_dense_border`'s `DENSE_BORDER_TOLERANCE` and `measure_shadow_refs`
all carry **absolute** log-density thresholds. A weighted mean of three
channels is by construction **narrower** than their envelope, and adding back
the weighted mean of the medians re-centres the output without making it
bracket its inputs. What step 3 actually guarantees is that the merged
channel sits at the weighted mean of the inputs' density level — close enough
that those absolute constants keep meaning what they were measured to mean.
§3.4's test asserts exactly that property, not a bracketing one.

A weighted sum in log is a weighted **geometric mean** of the linear
transmittances. That is a defensible physical quantity and is what
`10 ** (floor + val * (ceil - floor))` will recover. Say so in the docstring.

### 3.2 The weights

```python
# Three noisy measurements of one physical quantity — silver density — not a
# colorimetry problem. The minimum-variance estimator weights by 1/sigma^2;
# a Bayer CFA has twice as many green sites, so green carries about twice the
# photons and needs no interpolation at half its positions.
MONO_MERGE_WEIGHTS = (0.25, 0.50, 0.25)
```

**Do not use Rec.709 luma, and do not reuse `luma_of_log`.** Those
coefficients model the eye's response to display primaries — a photometric
weighting for scene brightness. That is the right job for the luma *axis* of
`analyze_bounds` (a perceptual proxy for "how bright", which is what a black
point should track) and the wrong job here.

Two honest caveats to record in the docstring: demosaicing correlates the
channels (R and B at a green site are partly interpolated *from* green), so
the real gain over green-only is closer to `sqrt(1.5)` than `sqrt(2)`; and
R/B have worse post-demosaic MTF, so weighting them in costs a little
sharpness. Green-only `(0, 1, 0)` is a defensible fallback if a measurement
ever says so.

Measuring the real per-channel sigma from the flat-field calibration frames —
which is exactly what they are for — is **out of scope here** and listed in
§8. `MONO_MERGE_WEIGHTS` enters `build_params()` at this step; §5.1's shim
absorbs the invariant break.

### 3.3 Wiring it in

In `composite.py`, between lines 711 and 718:

```python
img_log = to_log_density(result_linear)
del result_linear
if film_kind is FilmKind.MONOCHROME:
    img_log = collapse_to_mono(img_log)
```

`composite_and_normalize` takes a new `film_kind` argument, passed down from
`stitch_pipeline` alongside `reference_bounds`. Everything after the collapse
runs unchanged on `img_log.shape[-1] == 1`, **once §4 has landed** — the
hardcoded 3 is not a handful of functions but a dozen-odd sites between the
collapse point and the last consumer; §4's table is the checklist, and §4
must be complete before anything here can run.

Nothing upstream changes: `accum` stays `(H, W, 3)`, the warp, the feather
and `layout.solve_gains` all stay in linear light on three channels, which is
where they are physically correct. `solve_gains` will keep recording three
per-frame gains for a one-channel output; that is correct and should be left
alone — including in the schema, where the gains array stays `minItems: 3,
maxItems: 3` (§4 says so explicitly, since it touches that schema file).

### 3.4 Tests (`composite_test.py`, `normalization_test.py`)

- **The regression that matters most:** a colour roll's published output is
  unchanged by this whole plan. Assert that the **pixel data** is identical
  before and after, and that the recorded `floors`/`ceils` and meters are
  identical. Do **not** assert byte identity: `write_stitched_tiff` stamps
  `conversion_time=datetime.now()` and `software=software_tag_value()`
  (`stitch_pipeline.py:1552-1555`), so no two runs of any build produce
  identical bytes, today included. Assert on a real stitched fixture, not a
  synthetic one.
- `collapse_to_mono` on an image whose channels are one plane under three
  different affines returns that plane, up to the reference offset — and the
  merged channel's median equals the **weighted mean of the input medians**
  (the §3.1 step-3 property), with the output's log-density range sitting
  **inside** the inputs' envelope. That is what holds; a weighted mean does
  not bracket its inputs. It is enough to keep `REBATE_DENSITY_TOLERANCE`
  (0.10) and `DENSE_BORDER_TOLERANCE` (0.2) in scale.
- A mono composite publishes a 2-D uint16 array; `decode_normalized` of it,
  against the recorded 1-element `floors`/`ceils`, round-trips to the merged
  log density within quantization.
- `NORMALIZED_FILL` still lands on the thin rail in the uncovered canvas.

---

## 4. One-channel plumbing

Develop alongside §2; land before §3 is switched on. Each item is small and
independently testable. The hardcoded 3 is **not** limited to the two
obvious functions: every site between the collapse point (`composite.py:711`)
and the last consumer breaks on one channel, most with an `IndexError` or a
`ValueError` from a `reshape(-1, 3)`.

| Site | What breaks on 1 channel / change |
| --- | --- |
| `tiff_writer.py:81` | `photometric="rgb"` is hardcoded. Select `"minisblack"` when `pixels.ndim == 2`. |
| `icc_profile.py` | Add `ProfileKind.DENSITY_GREY` — the DENSITY profile is ProPhoto RGB and cannot tag a 1-channel file. Generate with the existing `cli/tools/generate_icc_profile.py`, same `TRC_G_DENSITY`. Pin its SHA256 beside the other two. |
| `stitch_pipeline.py:1561` | **The primary published-TIFF tag site**: the stitched TIFF's profile comes from `load_icc_profile(ProfileKind.DENSITY)` here, written through `write_stitched_tiff`. Select by the frozen film kind (`DENSITY_GREY` on a mono roll) — consistent with the invariant seed at `stitch_pipeline.py:861` (§2.3). |
| `exporter.py:104` | The secondary path: `_write_export` passes `ProfileKind.DENSITY` unconditionally; select by channel count. |
| `normalization.py:143` | `luma_of_log` indexes `[..., 0]`, `[..., 1]`, `[..., 2]` → `IndexError`. On one channel, luma is the channel itself. Called by `analyze_bounds`, `detect_rebate`, `withhold_dense_border`. |
| `normalization.py:340` | `analyze_bounds`: `reshape(-1, 3)` → `ValueError`; plus `range(3)` at 357, 365, 370–373. See "the bounds axis on one channel" below — the function does not survive untouched, by design. |
| `normalization.py:314` | `_same_pixel_color_floor_refs`: `range(3)` for the base-colour refs. On one channel the chroma is identically 0, so the neutral-chroma gate passes trivially — but the function still runs and needs a defined 1-channel path. |
| `normalization.py:395` | `measure_shadow_refs`: `reshape(-1, 3)`, `range(3)`. |
| `normalization.py:569` | `detect_rebate`: `range(3)` for `base_density` and the clip check. |
| `normalization.py:827` | `clamp_bounds`: `range(3)` in `clamp_axis` and the degeneracy loop. |
| `normalization.py:108` | `Bounds`' fields are typed `tuple[float, float, float]`, as are three return annotations (`analyze_bounds`, `measure_shadow_refs`, `clamp_bounds`). Generalise. |
| `composite.py:747` | The `fill_code` construction builds `(1, 1, 3)`; size it to the collapsed channel count. |
| `roll-manifest.schema.json` | `floors`, `ceils`, `shadow_refs`, `observed_min`, `observed_max`, `headroom_clipped_*`, `unclamped_*` are all `minItems: 3, maxItems: 3`. Change each to a `oneOf` of a 1-array and a 3-array, so **existing colour rolls validate unchanged**. `rebate.base_density` (schema lines 160–169) is different: it already carries a `oneOf` of a 3-array and `null`. It becomes **three-way** — `null`, a 1-array, a 3-array — not the uniform 1-or-3 treatment. |
| `roll-manifest.schema.json` (gains) | **Deliberately unchanged.** `solve_gains` keeps recording three per-frame gains for a one-channel output (§3.3), so the gains array stays `minItems: 3, maxItems: 3`. It sits in the same schema file as the rows above; do not "fix" it while editing them. |
| `auto_rotate.py:84` | `_FILL_CODE` builds `(1,1,3)` and indexes `[0,0,0]` — correct either way, but confirm `rotate_with_fill`'s `cv2.warpAffine` path on a 2-D uint16 input under test. |

**The bounds axis on one channel.** `analyze_bounds`' two-axis recombination
degenerates on one channel: the luma percentile pair *is* the channel's
percentile pair, `c_floors[0] == mean_cf` and `mean_lf` is computed from the
same single channel, so `floors` reduces to the luma percentile pair and the
colour axis vanishes entirely. That is the **correct** answer — with no
colour there is no colour deviation to add back — and it is why the collapse
belongs before `analyze_bounds` (§0.2). Generalise the `reshape` and the
`range(3)` loops to `shape[-1]`; the recombination arithmetic then collapses
to identity on its own. Document this in the function's docstring rather
than special-casing it.

`_same_pixel_color_floor_refs` likewise keeps running: on one channel
`refined_chroma` is identically 0, so the chroma gate admits every kept
pixel, and the function returns the single channel's percentile — identical
to the `None`-fallback path. Either outcome is fine; generalise the indexing
and let it run.

**Already handled, do not "fix":** `previews.generate_preview` and
`previews._promote_to_rgb` both already promote `ndim == 2` to RGB. The
preview and Edit-tab paths need no change.

**Not affected:** `measure_clip_fractions` runs on the linear RAW during
convert and stays three-channel; the flat-field gain map and the CA
correction are lens and sensor properties, not film properties, and stay.

---

## 5. Contract and migration

### 5.1 The roll-invariant break

`pipeline.build_processing_params` writes
`processing_params["normalize"] = normalization.build_params()`, and
`manifest.py:527` compares `processing_params` **by exact dict equality**.
Adding any key to `build_params()` therefore breaks every existing roll with
`ROLL_INVARIANT_MISMATCH` on its next add-scans.

That is not acceptable collateral. Handle it deliberately:

1. Bump `NORMALIZE_FORMAT_VERSION` to `2`.
2. Teach the invariant comparison a **forward shim**: when the stored block
   is `format_version: 1`, upgrade it in memory by injecting the current
   defaults for every key the stored block lacks, then compare. Because the
   injected defaults are read from the live `build_params()`, keys added
   later — §2's thresholds, §3's weights — are covered by the same shim
   without a second migration. A v1 roll then compares equal to a v2 build
   as long as the new constants are at their defaults, which for an existing
   colour roll they are.
3. New keys enter `build_params()` **unconditionally** when the steps that
   introduce them land — a conditional invariant is worse than a migrated
   one. §1's constants stay out entirely (§1.4: they shape no output); §2's
   thresholds and §3's weights join here.

The shim lives in one function next to `build_params()`, is covered by its
own test (including a key added after the shim was written, to prove the
forward property), and is the only place that knows v1 existed.

### 5.2 Manifests without a `film` block

A roll manifest predating §2 has no `film` block. Treat a missing block as
`{"kind": "colour", "source": "auto", "detector_version": 0}` on load, and
write it out on the next run. `film` is therefore **optional** in the schema
even though it is always written by current builds. The loaded default is
what seeds the candidate's published-profile invariant (§2.3) — a legacy roll
seeds `DENSITY`, not `DENSITY_GREY`.

### 5.3 Protocol version

`--film-kind`, the two new warning codes and the `film` block in `roll_info`
are all additive to the event protocol. Bump `PROTOCOL_VERSION` to 10 and
update `shared/contract/CONTRACT.md` and `schema.json` together; the Swift
side's probe-level tests will catch a mismatch.

### 5.4 New event codes (`events.py`)

- `MONO_DETECT_AMBIGUOUS` — warning; the statistic landed in the gap, colour
  was assumed.
- `MONO_DECISION_CONFLICT` — warning; fresh evidence disagrees with the
  frozen kind.

---

## 6. `DECISIONS.md` amendments

The **"Colour negative only"** section (line 865) currently reads as a scope
decision covering both film kinds. It is specifically a decision about *E-6
and `ProcessMode`*. Rewrite it to say so, and add a new section covering:

- Detect at the roll, freeze, never flip (§0.3, §2.3), and why.
- Collapse between the log and the bounds, and the three rejected sites
  (§0.2).
- Why the merge weights are an estimator, not a luma curve (§3.2) — this is
  the single most likely thing for a future reader to "correct" back to
  Rec.709.
- Why the merge does not bracket its inputs, and why the absolute
  tolerances survive anyway (§3.1 step 3, §3.4).
- The published mono TIFF is single-channel and that is final: toning is not
  a goal of this project, so there is no downstream consumer that needs the
  per-channel record back.

---

## 7. Work order

1. **§1** — the statistic (with the MAD floor), the pre-pass, the per-negative
   recording, the schema entry for `mono`. No behaviour change; §1's
   constants stay out of `build_params()`. Ship it.
2. **Measure.** Run over every roll in the library. Get the user's approval on
   the two thresholds before writing them down.
3. **§4** — the one-channel plumbing, including the grey ICC profile, the
   stitch-pipeline tag site, and the schema `oneOf`s (but not the gains
   array). Still no behaviour change: nothing produces a 2-D image yet.
4. **§5.1** — the format-version bump and the forward shim, with its test
   (including the forward property).
5. **§2** — the gate (thresholds enter `build_params()`, absorbed by the
   shim), the `film` block, the freeze with candidate-seeding, the
   `--film-kind` override, and the pre-pass moved ahead of the
   invariant seed.
6. **§3** — the collapse itself, switched on by `film.kind`.
7. **§6** — the `DECISIONS.md` amendments, and a `punchlist.md` entry for
   what §8 defers.

Run `uv run pytest --slow` at steps **3, 4 and 6**: step 3 touches TIFF
writing and the schema the slow stitch tests validate against; step 4 is the
invariant shim the slow stitch tests exercise directly; step 6 switches on
the collapse. Step 5 touches no TIFF writing or stitch geometry, and the
fast tier covers its manifest logic.

---

## 8. Explicitly out of scope

- **Measuring the per-channel sigma** from the flat-field calibration frames
  to replace `(0.25, 0.50, 0.25)` with rig-specific weights. Worth doing;
  needs its own measurement protocol. → `punchlist.md`.
- **Per-negative film kind.** Deliberately refused; see §0.3.
- **Mixed-kind rolls.** A roll is one film stock. Scan two stocks as two
  rolls.
- **A user-facing merge control** (channel weights, a darkroom contrast
  filter). The weights are pinned constants, like every other normalization
  constant.
- **Any Swift/UI work beyond surfacing `film.kind`** in the roll info the app
  already reads.
- **Transparency, chromogenic B&W, and stained negatives.** All normalize as
  colour, correctly.
