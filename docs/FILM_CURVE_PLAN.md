# Film curves: a per-stock, per-rig shape correction measured from a test roll

The three scan channels do not record a neutral subject with the same
*curve shape*. Measured on Kodak Gold 200 (session of 2026-09-19, 22
frames of a lit neutral card bracketed −4…+6 stops): the blue channel's
contrast climbs across the midtones, roughly 0.18 → 0.23 density per stop,
while green holds near 0.15–0.17 and red near 0.10–0.12. Per-channel
normalization can only stretch each channel — a gain and an offset — so a
shape difference survives it. What is left after the best straight stretch
is ±0.02 density through the normal tonal range and ±0.04 at the extremes,
which is roughly ΔE 2–3 and ΔE 4–8 in the rendered positive, and it
*reverses sign* between shadows and highlights, so the eye never adapts
it away.

This plan measures that shape from a test roll and corrects it.

It follows the house conventions (`docs/AUTO_CROP_PLAN.md`,
`docs/REBATE_ANCHORING.md`): numbered chunks, each independently green;
every constant in exactly one module; and **no threshold pinned without a
measurement the user has approved** (§6).

---

## 0. Why this shape

### 0.1 The correction needs no exposure axis, no control shots, no registration

Each sampled patch measures all three channels **of the same piece of
film**. A patch is therefore already a matched triple of densities, and the
quantity to fit is "what green density accompanies this red density". The
exposure that produced the patch never enters the fit.

That removes, from the hand analysis, everything expensive:

- **Exposure labels** — used for reporting only.
- **Digital control frames** — they verify the *target* (neutral, evenly
  lit, flash colour stable). The film says the same thing through the
  duplicate frames and the grey-card frame, so they stay a shooting
  convention, not an app input.
- **Tape-corner registration and per-patch lighting offsets** — needed only
  to place frames on a shared exposure axis, which the fit does not use.
  The tape stays useful to the *photographer* (it marks the unshadowed
  area), and the fit simply excludes it (§1.2).

The bracket's only job is to cover the density range.

### 0.2 The curve must carry shape only — no gain, no offset

`normalization.normalize_log_image` already applies a per-channel gain and
offset (floor/ceil), and `auto_neutral` applies a two-point per-channel
cast correction at render time. A LUT that carried its own gain/offset
would fight both, and a cast would be corrected twice.

So the fitted mapping has its **best affine part removed over the covered
range** and only the curvature is stored. The removed affine part is the
film's per-channel contrast ratio; it is *reported* (it is informative)
and not applied.

### 0.3 The insertion point is the one the crosstalk work already identified

`composite.py:881`, between `to_log_density(result_linear)` and
`block_median_grid(img_log)` — before the meters, so `analyze_bounds`
meters the corrected densities. A roll cannot mix curves: the token is a
roll invariant (§4.2).

### 0.4 Precedents this follows

- **Profile storage**: `GridProfile` (`grid_profile.py`, `library/models.py`
  `GridProfileRow`, `repo.save_grid_profile` …) — a frozen dataclass, an
  Alembic revision, five repo functions, a summary in `events.py`, and a
  `create` / `list` / `delete` CLI noun.
- **Roll assignment**: `roll set-flatfield-reference` (`cli.py:1958`) — a
  block written onto the roll manifest.
- **Invariant token**: `pipeline.build_processing_params` (`pipeline.py:660`)
  — the key is **absent** when no profile applies, exactly as `flat_field`
  and `chromatic_aberration` are, so every pre-feature roll keeps comparing
  equal in `check_roll_invariants` (`roll_manifest.py:674`).

---

## 1. Chunk 1 — the measurement and the fit (`film_curve.py`)

Pure numpy over decoded frames; no library, no manifest, no CLI.

### 1.1 Input

A list of raw paths (one capture per test frame — these are *not* tiled
scans and never enter stitch), decoded through the locked `RAW_PARAMS`,
optionally flat-fielded through an existing reference
(`flatfield.apply_in_place`), then `normalization.to_log_density`.

### 1.2 Sampling

A grid of patches per frame; per-patch per-channel medians. Non-target
patches (tape, holder, edges, dust, the grey card when present) are
rejected **by robustness, not by geometry**: the target dominates the
frame, so patches whose density sits far from the frame's modal density
are dropped. The rejection width is a §6 measurement.

### 1.3 Fit

Pool every patch from every frame into triples. For red and for blue:
bin against green, take per-bin medians, fit a monotone piecewise-linear
mapping, subtract its best affine part over the covered range (§0.2), and
store the residual curvature as a sampled LUT plus the covered density
range. Outside that range the correction clamps — it never extrapolates.

### 1.4 Report

Points fitted, residual RMS, covered density range per channel, agreement
between duplicate frames, and the affine part that was removed. This is
what tells the user whether the roll was good enough to freeze.

### 1.5 Tests

Synthetic triples built from known curves (fit recovers them), the
affine-invariance property (feeding curves that differ only by gain and
offset yields a null correction), clamping outside the covered range, and
rejection of injected non-target patches.

---

## 2. Chunk 2 — storage and the CLI noun

`FilmCurveProfile`: `profile_id`, `name`, `film` (the stock label, matching
the existing `film` catalogue field), `rig_profile_id`, `scanny_boy_version`,
`created_at`, `curves`, `report`. One Alembic revision adding
`film_curve_profiles`; `save` / `list` / `load` / `delete` /
`rolls_using_film_curve_profile` in `library/repo.py`; a
`FilmCurveProfileSummary` in `events.py`; and
`film-curve create --name --film --frames … [--rig] [--flatfield]`,
`film-curve list`, `film-curve delete` in `cli.py` beside `rig` and `grid`.

Independently green: a profile can be created, listed and deleted with
nothing yet consuming it.

---

## 3. Chunk 3 — assigning a curve to a roll

`roll set-film-curve --roll DIR --profile ID`, modelled on
`_run_roll_set_flatfield_reference`. Writes a `film_curve` block onto the
`RollManifest` (id, name, film, a content hash of the curves). `roll info`
reports it. Refuses on a monochrome roll. **Warns** when the roll's rig
profile differs from the curve's.

Still independently green: nothing reads the block yet.

---

## 4. Chunk 4 — applying it

### 4.1 The apply

`film_curve.apply_in_place(img_log, curves)` at `composite.py:881`,
before `block_median_grid`. One interpolation per channel.

### 4.2 The invariant

`build_processing_params` gains `film_curve_block` and, when present,
sets `processing_params["film_curve"] = {"profile_id", "sha256"}` — absent
otherwise. `probe --roll` and `run_convert` both go through that one
function, as they already must. Because the block changes published
pixels it is a full invariant: unlike `flat_field`, it is **not** added to
`ROLL_PROFILE_PROCESSING_PARAMS_KEYS`, so a roll cannot change curve
mid-way.

### 4.3 Protocol

`PROTOCOL_VERSION` 23 → 24 (`events.py:14`), with the Swift fixtures moved
in the same commit, per `fd2cd06`'s precedent.

### 4.4 Tests

A roll stitched with and without a curve differs in the expected
direction; the invariant check rejects a second run with a different
curve and accepts an identical one; a pre-feature roll still passes;
monochrome rolls are untouched.

---

## 5. Chunk 5 — the Mac UI

A sheet that builds a profile from a folder of scans, shows the §1.4
report, and assigns it to a roll — mirroring the rig-profile sheet. The
CLI is complete without this chunk.

---

## 6. Measurements to approve before any threshold is pinned

1. **Roll-to-roll stability — the go/no-go.** A second test roll,
   developed in a separate run, must reproduce the first roll's curvature.
   If the difference between rolls approaches the ±0.02 the correction
   removes, a frozen per-film curve is fitting noise and **this feature
   should not be built**.
2. **The non-target rejection width** (§1.2), measured against frames
   containing the tape, the grey card and dust.
3. **The patch grid size**, chosen so per-patch noise is well below the
   effect being fitted.
4. **The dense-end exposure axis.** The top of the Gold 200 bracket
   flattens in all three channels by the same proportion, which is *not*
   the signature of scanning stray light (that would flatten blue far
   more). It is either the film's shoulder or the flash's top power steps
   under-delivering; a three-frame digital test (1/4, 1/2, 1/1) separates
   them. This does not block chunks 1–3, but it decides how much of the
   top of the range is trustworthy.

## 7. What this does not do

It does not address channel crosstalk (a 3×3 in the same domain, a
separate feature), stray light (measured as absent at these densities on
this rig), development crossover that varies per roll, or where
`analyze_bounds` places its anchors — which is a separate and possibly
larger source of the casts being corrected by hand.
