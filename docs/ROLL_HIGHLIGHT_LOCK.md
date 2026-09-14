# Roll highlight lock: a roll-level dense-end colour estimate

## 0. The problem

`normalization.analyze_bounds` ties the published TIFF's dense-end colour
(`floors`, the scene-highlight end) to `_same_pixel_color_floor_refs` —
the brightest near-neutral pixels *of that one negative*. The thin end
(`ceils`) has had a roll-level anchor since docs/REBATE_ANCHORING.md: every
negative's shadow colour is tied to one locked film-base measurement, so
it is identical across the roll. The dense end never got the same
treatment, for the reason `CAST_REMOVAL_PLAN.md`'s §0.4 gives: independent
per-channel percentiles at the dense end read a *different scene object
per channel* and mistake coloured highlights for film cast, so the fix
there was a shared, chroma-gated pixel set per negative — better than raw
percentiles, but still a per-negative estimate.

Two consequences follow directly:

1. A negative whose brightest content is legitimately coloured (a sunset,
   a tungsten interior, blue-shade snow) has its highlights forced toward
   grey, because the gate finds no trustworthy neutral and falls back to
   plain percentiles of whatever is brightest.
2. Two negatives shot on the same roll — same film stock, same chemistry,
   same scan session — can display different highlight colour balance
   purely because their scene content differs, even though the *film's*
   colour response at the dense end is a roll property, not a per-frame
   one.

## 1. The chosen design, and why published pixels are untouched

docs/DECISIONS.md's rule: **published TIFFs never change.** Every
roll-level decision that already exists (film base, film kind, the camera
colour matrix) is frozen data consulted at render time, never baked
backward into files already on disk. This feature follows the same shape:

- A **roll-level highlight-colour estimate**, `highlight_lock.HighlightLock`,
  derived from the `highlight_refs` every stitch already records per
  negative (CAST_REMOVAL_PLAN R-1) — no new per-negative measurement, no
  new image I/O, no change to `normalization.analyze_bounds` or
  `NORMALIZE_FORMAT_VERSION`.
- A **render-time correction**, applied identically wherever a negative's
  published pixels become display pixels — preview and export both, since
  both already share `render.render_positive_float` and
  `tone.build_channel_tables`.
- **Recomputed wholesale, never merged**, every time the roll's negative
  set changes (`stitch_pipeline.run_stitch`'s end-of-run write,
  `edits.run_edit_delete`), so a negative added in a later run — or one
  removed — updates the estimate for the whole roll, older negatives
  included. That is a deliberate, user-approved consequence: **older
  negatives' displayed appearance can change as the roll grows.**

The published TIFF, its `floors`/`ceils`, and everything
`normalization.py` computed at stitch time stay exactly what they always
were. Nothing here bumps `NORMALIZE_FORMAT_VERSION` or
`ROLL_MANIFEST_FORMAT_VERSION` — the estimate is additive data, the same
posture `camera_color` already has (docs/DECISIONS.md, "Two ICC profiles,
and the profile is never load-bearing"): it shapes no published pixel, so
an existing roll benefits immediately, with no re-stitch, and without
tripping the `ROLL_PREDATES_FILM_BASE` refusal
(`stitch_pipeline.run_stitch` §REBATE_ANCHORING §9,
`cli.py`'s mirror of the same check).

## 2. Where the estimate lives

`RollManifest.highlight_lock: dict | None` — a new, nullable, additive
field alongside `film_base` and `camera_color`, backed by a new nullable
`TEXT` column (`library/migrations/versions/0014_highlight_lock.py`),
serialized through `roll_manifest.RollManifest.to_dict()` and the
`shared/contract/roll-manifest.schema.json` `highlightLock` definition.
`None` on a mono roll, a roll with no locked film base, or a colour roll
with no qualifying negative yet.

**Every trigger that changes the roll's negative set recomputes it:**

- `stitch_pipeline.run_stitch`, at the very end of the run, after every
  negative this run touched (composited, adopted, or removed) has already
  been applied to `roll.negatives` and the run's `normalization_aggregate`
  is computed.
- `edits.run_edit_delete`, after the selection's negatives are removed
  from `roll.negatives` and before the batch's single `write_roll_manifest`.

Both call `highlight_lock.compute_roll_highlight_lock(roll)` fresh — never
an incremental update — and both compare the new value against the
previous one to decide whether cached previews need to be forced to
regenerate (§5).

## 3. The math

### 3.0 Sign convention

Everything here is log10 *density*, not brightness. A negative's **base**
(clear film) transmits nearly all the light hitting it, so its density is
the *least negative* value on the roll. A **scene highlight** is the
*densest* silver on the negative — it blocks the most light, because a
bright scene point exposed the film hardest — so its log-density is *more
negative* than base. `H` (a negative's own highlight reference) is
therefore always **below** `B` (the roll's base density): `H - B < 0` in
every channel, on any real roll. This shipped backwards once, briefly: an
amplitude gate written as "reject below `_MIN_AMPLITUDE`" (positive-side
thinking) rejected every real negative, because every real amplitude is
negative. §5 records the fix.

### 3.1 Why every deviation here is green-relative, not median-relative

`analyze_bounds` recentres its three-channel colour references on their
own **median** (`c_floors[ch] - median(c_floors)`) — robust, but `median`
of three numbers is not linear: `median(a) + median(b) != median(a + b)`
in general. This module needs to *add and subtract* deviation vectors (a
negative's own colour deviation, the roll's locked base colour deviation,
the roll's fitted ratio) and have the algebra come out exact, not
approximate. Recentring on a **fixed reference channel** — green, the same
channel `color.cast_slopes` already never modifies ("green is the
reference channel and is never modified... exposure stays anchored") —
is an ordinary linear functional (`x - x[green]`), so every identity below
holds exactly. This is the one deliberate deviation from the math
sketched when this feature was scoped (which proposed `k_ch = (H_ch -
B_ch) / median_ch(H - B)`): a median-based amplitude does not close under
the addition this correction needs, and green-relative does, for free,
with no extra guard.

### 3.2 The roll estimate

For a qualifying negative — `highlight_refs` (`H`) non-null, i.e. the
per-negative neutral gate (`_same_pixel_color_floor_refs`) found a
trustworthy near-neutral set — with `B` the roll's locked film-base
density (`roll.film_base["density"]`) and `G` green's channel index:

```
k_ch = (H_ch - B_ch) / (H_G - B_G)
```

`H_G - B_G` (this negative's green-channel highlight amplitude above base,
**always negative** — §3.0) legitimately varies with exposure and scene
brightness; the *ratio* between channels does not — it is a property of
the dye set, constant for the roll. A negative whose green amplitude is
not sufficiently negative (within `_MIN_AMPLITUDE` of zero, or — impossible
on real film — positive) does not qualify: it is a measurement failure,
not a colour reading.

`K = median over qualifying negatives of k` (an ordinary median over
independent samples — no linearity requirement on this axis). `K[G]` is
always exactly `1.0`. `HighlightLock` also stores `base` — the roll's
locked density verbatim — because the render-time correction (§3.3) needs
an *absolute* reference point that `k` alone, a pure ratio, cannot supply.

### 3.3 The render-time correction

Define, relative to green: `own_dev = floors - floors[G]`, `base_dev =
ceils - ceils[G]`. Because `analyze_bounds` builds `floors[ch] = mean_lf +
(F[ch] - median(F))` (`F` is whichever per-channel reference this
negative's dense end actually used — the gated `H`, or the plain-percentile
fallback when the gate failed) and `ceils[ch] = mean_lc + (B[ch] -
median(B))`, the `mean_lf`/`mean_lc` and `median(F)`/`median(B)` terms are
each a common additive constant across channels, so they cancel exactly in
the green-relative subtraction: `own_dev[ch] = F[ch] - F[G]`, `base_dev[ch]
= B[ch] - B[G]`, **exactly, and with no `H` needed at all** —
`own_dev`/`base_dev` come from the already-recorded `floors`/`ceils` alone,
for every negative, qualifying or not.

The one quantity that genuinely needs `H` is the amplitude — and it must
come from `H` directly, **never from fitting this negative's own R/B
deviations against `(K - 1)`.** An earlier version of this module did
exactly that (least-squares projection of `own_dev - base_dev` onto
`K - 1`), and it is wrong, not merely approximate: for typical colour
negative film `K` is close to `(0.9, 1.0, 1.1)`, so `K - 1` is
approximately the pure red-blue axis — exactly where a warm/cool scene
cast (sunset, tungsten, open shade) lives. A projection onto that axis
keeps whatever part of the scene's own cast already lies along it and only
corrects the perpendicular part, so a real warm/cool cast on a qualifying
negative could pass through the "correction" essentially untouched
(confirmed numerically: a `(+0.08, 0, -0.08)` cast — aligned with a
`(0.9, 1, 1.1)`-shaped `K - 1` — survived unchanged; a `(+0.08, 0, +0.08)`
cast, perpendicular to it, was fully removed). That inverts the feature's
whole point.

**Qualifying negative** (`H` recorded): read the amplitude directly, no
fitting —

```
a = H_G - lock.base_G
dev_new[ch] = base_dev[ch] + a * (K[ch] - 1)
floor_new[ch] = floors[G] + dev_new[ch]
```

**Non-qualifying negative** (`H` is `None`): there is no `H` to read an
amplitude from, and reading one from this negative's own R/B `floors`
would reintroduce the same bug. Instead, approximate from the **green
channel alone** — no R/B read:

```
a_approx = floors[G] - lock.base_G
```

`a_approx - a_true = mean_lf - median(F)` (`a_true` being the analogous
quantity `F_G - B_G`, defined against the untrustworthy `F` the same way
`a` is defined against a validated `H`): the negative's overall
near-densest scene luma percentile against the median of its own three
dense-end references, both drawn from the same physical region of the
frame (the near-extreme dense tail), so typically close — the error
shrinks with the fallback's own chroma spread (weakest exactly where the
correction has the least to gain) and grows with it (strongest on a
genuinely colourful highlight, where even an imperfectly calibrated
*magnitude* still moves the colour in the roll's correctly measured
*direction*). Documented as an approximation, not assumed exact; a future
measurement pass could bound the error empirically, out of this plan's
scope.

Either branch's amplitude is gated the same way `k`'s amplitude is (§3.2):
not sufficiently negative means no trustworthy amplitude, and `floors` is
returned unchanged.

At `ch == G` in either branch: `floor_new[G] = floors[G] + base_dev[G] + a
* (K[G] - 1) = floors[G] + 0 + a * 0 = floors[G]` — **green's own floor
never moves**, for every negative, correction or not.

**Identity, exactly** (qualifying branch): when a qualifying negative's
own `k` already equals `K`, i.e. `(H_ch - B_ch)/(H_G - B_G) = K_ch` for
every channel,

```
dev_new[ch] = base_dev[ch] + a * (K[ch] - 1)
            = (B_ch - B_G) + (H_G - B_G) * K_ch - (H_G - B_G)
            = (B_ch - B_G) + (H_ch - B_ch) - (H_G - B_G)   # since a*K_ch = H_ch-B_ch
            = H_ch - H_G = own_dev[ch]
```

— exactly, algebraically, no fitting residual — so `floor_new == floor_old`
for every channel, to the bit
(`highlight_lock_test.py::test_identity_when_negative_matches_roll`).

**Degenerate `K`** no longer needs its own case: because the amplitude is
read (or approximated) rather than fitted, there is no `dot(K-1, K-1)`
denominator to vanish. A roll whose `K` is uniformly `1.0` simply produces
`dev_new = base_dev` for every negative — tied to the film base's own
colour, no cast — which is what it should mean physically.

**Safety net**: a correction that would push a channel's `floor_new` at or
past its own `ceils[ch]` (degenerating or inverting that channel's stretch)
falls back to the original `floors[ch]` for that channel only — never for
the whole triple, so one pathological channel does not discard the other
two's otherwise-good correction.

Applying the correction to *decoded pixels* is then a per-channel affine
in normalized space, fixing the thin end (`val = 1`) exactly:

```
delta = floor_old - floor_new         # this channel's shift, log10 D
range = ceil - floor_new              # Metering.ranges[ch], already corrected
val_new = (delta + val * (range - delta)) / range
```

`color.remap_dense_end` applies this, and it is called immediately after
`normalization.decode_normalized` and before global CMY / `1 - val` in
every render path (`render._linear_lut_from_codes`,
`tone.build_channel_tables`), so everything downstream — the camera
matrix, the tone curve, cast removal, dye separation — composes unchanged.

### 3.4 What this deviates from the original sketch, and why

The feature was scoped with a median-based ratio
(`k_ch = (H_ch - B_ch) / median_ch(H - B)`) and a median-based deviation
basis, matching `analyze_bounds`' own convention. Implementing it that way
produces a formula that is only an *approximate* identity: a numerical
check (`median(a) + median(b) != median(a + b)` for three-element vectors
in general) shows `own_dev - base_dev` is not exactly `amplitude * (K -
median(K))` even when a negative's own ratio matches the roll's — the
"identity when a negative's k equals the roll's" requirement the
deliverables list explicitly cannot be met exactly under that basis. The
green-relative reformulation (§3.1) is mathematically equivalent in
spirit — still a per-channel ratio, still robust to per-negative exposure
variance — but closes under addition, so the identity is provable rather
than merely approximately true, and it reuses a convention
(`color.cast_slopes`'s green-anchored ties) already established in this
codebase.

A **second**, more serious flaw was caught in review after the first
implementation shipped internally (never released): that first version
also estimated the amplitude by least-squares projection onto `K - 1`, on
the reasoning that it "lets the same formula apply to a non-qualifying
negative too." §3.3 above explains why that reasoning is backwards — the
projection preserves exactly the casts (warm/cool, along the film's own
`K - 1` axis) this feature exists to fix. The corrected design reads the
amplitude directly off `H` for a qualifying negative (exact, no fitting)
and falls back to a documented green-only approximation for a
non-qualifying one (§3.3), never touching R/B for the non-qualifying case
either. A **first** flaw, a plain sign error (§3.0), also shipped in the
same internal version and was caught in the same review — `_MIN_AMPLITUDE`
was gated as if a real amplitude were positive, so `compute_roll_
highlight_lock` returned `None` on every real roll it was tried against.

## 4. Consistency decisions

**`color.read_metering` sees the corrected bounds.** `read_metering` takes
an optional `highlight_lock` (a `HighlightLock` instance, the roll
manifest's raw dict, or `None`); when it resolves, the `floors` every
other computed field is measured against — `ranges` (feeding
`cmy_offsets`, the global CMY sliders' per-channel scale) and
`highlight_refs_norm` (feeding `cast_slopes`' two-point cast-removal tie)
— are the *corrected* floors, and `Metering.highlight_floor_delta` records
what changed for the render path to apply to pixels. One function, one
place the correction is computed; every consumer (preview, export, Auto
Cast Removal, the metering-unavailable warning) reads the same corrected
`Metering`, per the "one effective-bounds function, not ad hoc patches"
brief.

**Auto Density / Auto Grade** (`auto_tone.solve_density`/`solve_grade`)
take the same optional `highlight_lock` and apply the correction to the
Rec.709-*luma*-weighted floor/ceil bound they solve against, for the same
reason: the negative's displayed density level should match what Auto
Density is tuning toward. The shift here is normally tiny — the
correction is zero at green and small at R/B, while Rec.709 luma weights
(0.2126/0.7152/0.0722) are close to, but not exactly, a plain mean — but
it is threaded through rather than left stale.

**`neutral_residual` is left unmodified — documented staleness, not a
silent gap.** It was measured at stitch time against the *published*
(uncorrected) bounds and cannot be re-measured without the pixels
(published pixels are never touched, by design — §1). `auto_color.solve_cmy`
now takes the roll's `highlight_lock` and threads it into the
`cast_slopes` compensation its Step 3 already performs (the two-point tie
compensation, evaluated at one anchor point), but the residual itself is
used as recorded. This was already a first-order approximation before a
highlight lock existed (the plan doc's own §3.3 language: "a first-order
proxy... not an exact cancellation"), so the correction does not introduce
a new kind of staleness, only widens an existing one very slightly. A
future measurement pass could re-derive `neutral_residual` against
corrected bounds; that needs the pixels and is out of this plan's scope.

**Previews are cached; a changed lock invalidates them.**
`previews.sync_previews` gained a `force: bool` parameter: when the roll's
`highlight_lock` changed (compared before/after the recompute),
`run_stitch` and `run_edit_delete` call it with `force=True`, regenerating
every completed negative's cached preview PNG, not just the ones the
triggering run touched — because a lock change can move a negative this
run never touched. The CLI's *decoded-pixel* cache
(`previews.cached_preview_codes`) needs no change: it caches pre-LUT
density codes, and the correction is a LUT step (`color.remap_dense_end`),
applied fresh on every render exactly like tone and colour already are.
On the Swift side, `EditModel.renderGeneration` gained a
`highlightLock` cache-generation term (`RollManifest.HighlightLock.
cacheTerm`), parallel to the existing `cameraColor` term, so the app's own
full-resolution region/preview render cache (`PreviewCache`) invalidates
the same way a camera-matrix change already does.

**Export provenance.** `exporter.provenance_record`'s `rendered` block
gained a `highlight_lock` entry — the roll's lock in effect for that
export, or `null` — so an exported file states, without the database,
whether (and to what) its dense-end colour was retargeted.

## 5. Risks and open items

- **Headroom clipping.** A negative whose dense end already sat near the
  encode's headroom rail (`NORMALIZED_HEADROOM_LOW`) can have its
  corrected floor land closer to (or past) that rail once retargeted;
  values already clipped by the original encode's headroom cannot be
  recovered by a render-time correction — no different in kind from any
  other headroom-clipped tone move, but worth remeasuring
  `HEADROOM_CLIP_WARN_FRACTION`-style statistics once this ships against
  real rolls.
- **No threshold was invented.** `_MIN_AMPLITUDE` (1e-4 log10 D) is a
  division-by-near-zero guard, not a tuned sensitivity constant — it
  exists only to keep a negative whose green amplitude is at or below the
  film base from producing a nonsensical (near-infinite or sign-flipped)
  ratio. No minimum qualifying-negative count is enforced; the house rule
  is "no threshold ships ungrounded," and a sample-size floor here would
  be exactly that without a measurement backing it.
- **Older negatives' appearance changes as the roll grows.** Explicitly
  intended (§1), but worth surfacing to the user in some form (a "roll
  colour updated" notice, or similar) — out of this plan's scope, which is
  the correction and its plumbing, not UI surfacing.
- **`neutral_residual` staleness compounds slowly.** See §4; unresolved
  because it needs pixels this plan deliberately never touches.
- **The non-qualifying green-only approximation's error is documented, not
  bounded.** `a_approx - a_true = mean_lf - median(F)` (§3.3) is derived
  algebraically but not measured against real rolls; it is expected to be
  small because both terms are drawn from the same near-extreme dense
  tail, but "expected" is not "measured." A future pass could compare
  `a_approx` against a held-out `a_true` on rolls where the gate happens
  to succeed, to put a real number on it.
