
* ~~The embedded ICC profile does not match the pixels' actual transfer curve.
  `ProPhoto-v4.icc` declares true ROMM (encoded breakpoint 0.03125), but
  rawpy's `gamma=(1.8, 16)` writes LibRaw's generalised curve instead — a
  different breakpoint plus an offset term (measured 2026-08-29; see Phase 2
  plan section 2.3.1). A viewer honouring the profile renders every Phase 1
  TIFF too dark in the shadows: about 360 LSB at a linear value of 0.05
  (−5.2%), rising to −20.8% at 0.005 and falling to nothing at white. Phase 2
  reads the pixels correctly and is unaffected.~~ **Fixed in Phase 3 P3-1:**
  `ScannyBoy-ROMM-LibRaw-v4.icc` embeds LibRaw's curve; no pixel values change.
  ~~Superseded again by the linear decode~~ (see "Negative inversion" below):
  the profile is now `ScannyBoy-Linear-ProPhoto-v1.icc`, a linear TRC, which
  is what makes the *pixel* story match the profile story for good.
* ~~`probe --out` has no notion of `scanny-boy-roll.json` (Phase 2's stitched
  roll manifest) — only `scanny-boy-manifest.json`. It reports a folder
  holding only a roll manifest as `OUTPUT_NOT_EMPTY` rather than recognising
  it as a legitimate rerun/re-stitch target. The app works around the
  resulting false positive client-side (`ConfigurationModel.existingRoll`),
  but this means the app can't show an itemized preview of what a rerun or
  re-stitch would actually replace, the way it already does for a plain
  `convert`/`run` — it can only ask for one general, explicit
  acknowledgement before passing `--overwrite`.~~ **Fixed in Phase 3 P3-3:**
  `probe --roll` validates directly against the roll manifest and reports
  `roll_overlap` per prospective negative (§3.5); the app's Add Scans stage
  (P3-11) targets `--roll` exclusively now, so `ConfigurationModel` no
  longer needs the `--out`-blind-spot workaround at all —
  `ConfigurationModel.existingRoll` and its `OUTPUT_NOT_EMPTY` special case
  are gone, replaced by the overlap sheet's itemized Skip/Replace review.

Phase n:

* Maybe change border fill-in to cyan or some contrasting color
* extended metadata editing (location, camera, lens, film stock)
* Crop based on manifest data, maybe lock in an appropriate aspect ratio
* White balance / base neutralization
* A purpose-built rebate-deviation detector. Phase 2 specifies
  `rebate_deviation_px` in the contract and records it in the roll manifest,
  but never gates on it and never implements detection at all — Chunk P2-1
  found the rebate isn't cleanly detectable with a generic straight-edge
  finder. A detector constrained to edges near the frame margin and roughly
  parallel to the solved strip axis (rather than the longest line anywhere in
  the image) could make this a real gate instead of an always-`null` field.
* Measure the photometric-gain thresholds from real scans. Stitch-phase gain
  compensation (per-frame, per-channel gains solved globally in log space,
  geometric mean 1) shipped with two **unmeasured** constants that need a
  user gate: `MIN_GAIN_OVERLAP_PX` (borrows NegPy's 1000px floor) and
  `GAIN_DRIFT_WARN`. `MAX_OVERLAP_MAD` itself now gates the *post-gain
  residual*, but its value (0.20) was measured against uncorrected overlaps
  and is far looser than a healthy residual — re-measure it at the same
  gate. See composite.py's module docstring and DECISIONS.md "Quality
  gates".
* Manual negative reordering. Phase 3 orders a roll's negatives by capture
  time alone (§3.7); an optional `sequence_override` on `negative`, consumed
  by `roll_sequence.py` ahead of capture time, is where a manual order would
  attach.
* Deleting a negative outright. With replacement now in-place
  (no tombstone, no supersede-with-null-replacement to reuse), this needs
  its own real delete mechanism — a `roll delete-negative` command is the
  likely shape; the Edit tab is where it would attach.
* Setting a roll's capture date or a per-negative date override from the
  app. Phase 3's Edit tab (P3-12) shows both read-only: no CLI command
  writes `metadata.roll_capture_date` or a negative's
  `capture_time.date_override` — see Phase 3 plan §5.6. A `roll set-date`
  command, by analogy with `roll rename` (§5.5), is the likely shape of the
  fix. (An app-level `shots_per_negative` editor was retired entirely: the
  grouping is each stitch batch's own choice, picked on Add Scans, and the
  roll record no longer stores it.)
* Flat-field deferred pieces (see FLATFIELD_PLAN.md §4 for what did ship):
  - **Non-RAW references.** NegPy accepts ordinary images too; here a
    reference must be a `.NEF`, because a JPEG reference would have to be
    guessed into linear light.
  - **A per-image / per-negative toggle.** NegPy has one; this design
    applies a profile to a whole roll by construction — a per-negative
    toggle would defeat the roll invariants.
  - **Black-frame subtraction.** The correction is multiplicative gain
    only, same as NegPy; a dark-frame reference would be additive.
  - **Re-measure `MAX_OVERLAP_MAD`** now that overlaps arrive
    de-vignetted — the falloff the old measurement carried is gone, so the
    gate can probably tighten. Fold into the same user gate as the gain
    thresholds above.
  - ~~**Interacts with linear-gamma intermediates.**~~ **Resolved** by the
    linear decode (see "Negative inversion" below): the round trip is now
    the plain fixed-point scaling of `linear.py`.

Phase n: 
Negative inversion

~~might need to put the tiff in linear (gamma 1, 1???) instead of whatever gamma rawpy gives us on conversion right now.~~
**Done:** `RAW_PARAMS` is now `gamma=(1, 1)`, `output_color=raw`, unity
white balance — NegPy's decode exactly. The written TIFFs are linear
sensor-channel data, tagged `ScannyBoy-Linear-ProPhoto-v1.icc`; previews
sRGB-encode for display. Old rolls must be reconverted (their manifests pin
the old profile hash and `processing_params`).

Eventual:
- would it be easy to convert this to an electron app for better cross-platform compatibility (plus familiarity to me)

Geometric calibration (docs/GEOMETRIC_PLAN.md, protocol version 7) deferred
pieces:

* **Rename the `--flatfield` flag / command family.** The flag now names a
  whole calibration profile (gain map + distortion + CA), not just a
  flat-field gain map. A rename reaches `cli.py`, `CONTRACT.md`,
  `schema.json`, `CLIRunner.swift`, `ConfigurationModel.swift`, and the
  stored-defaults key — cosmetic, and deliberately not done in the same
  change as the substance.
* **Re-measure `SCALE_DRIFT_WARN` / `SCALE_DRIFT_FAIL` /
  `MAX_PAIR_RMS_PX` / `MAX_GLOBAL_RMS_PX`.** Undistorting matched points
  before registration can only reduce apparent scale drift and pair
  residuals, so these gate-C thresholds are now loose for
  geometry-corrected rolls. Fold into the same user gate as the
  gain-threshold and `MAX_OVERLAP_MAD` re-measurements above.
* **Detect on green?** The luminance detection image carries a sub-pixel
  CA displacement from green at the frame corners. It is measured at
  calibration time and recorded as `detection_channel_ca_px` on the
  profile's `calibration_report`; if real profiles show it is a
  meaningful fraction of `RANSAC_REPROJ_PX`, switching
  `detection.build_detection_image` to the green channel (and re-measuring
  `DETECTION_LONG_EDGE` / `USE_CLAHE`) becomes worthwhile.

Normalization (docs/DECISIONS.md, "Normalization decisions"; protocol
version 8) deferred pieces:

* **Roll-consistent colour bounds (D-4).** The orange mask and the lamp are
  constant across a roll; the scene content is not. Record exists
  (`normalization_aggregate`); `--colour-bounds run-median` is the likely
  shape of the fix.
* **Run-propagated film base (D-3's staging step 2).** Film base is a
  property of the *roll*; Dmin measured from whichever negatives do show
  rebate can set the ceiling and the thin-end colour reference for every
  negative in the run, including tight ones. Needs the per-negative
  `log10(t_ref / t_n)` exposure correction from the recorded raw
  `base_density`. Subsumes D-4's colour axis with a better estimator.
* **The five unmeasured rebate constants** (`REBATE_ANCHOR_PERCENTILE`,
  `REBATE_DENSITY_TOLERANCE`, `REBATE_MIN_AREA_FRACTION`,
  `REBATE_MAX_SPREAD`, `REBATE_MIN_SEPARATION`) — provisional and
  unmeasured, same status as `MIN_GAIN_OVERLAP_PX` / `GAIN_DRIFT_WARN`.
  Fold into the same user gate as the other unmeasured thresholds: run the
  detector over real rolls with a dump of `mask_fraction`, `base_density`,
  whether it fired, and the per-channel clip fraction inside the mask.
  Until then, a detector that never fires is the safe failure.
* **`rebate_deviation_px` retired via the rebate mask** (§3.13's bonus).
  Given the rebate mask, the edge's deviation from the solved strip axis
  falls out nearly for free.
* **Channel unmix / spectral crosstalk.** Deferred entirely. When it comes
  back: a 3×3 on the raw log densities, slotted between `to_log_density`
  and `analyze_bounds` — and it invalidates every roll's `normalize_params`.
* **Intermediate-precision measurement (N-1's measurement task).** Decode
  one real negative's frames, composite through the current linear-uint16
  path and a float32 path, normalize both, report RMS against grain sigma.
  Within a quarter of grain: close out. Otherwise: a new plan for log
  intermediates.
* **Intermediate precision itself** stays linear uint16 (§1.4 of the plan);
  changing it touches `linear.py`, `tiff_writer.py`, `flatfield`, the ICC
  story, and most of the test suite.
* **Print-stage metering consumers.** Shadow refs, anchor, textural range
  are recorded and read by nothing; Phase 4 will need them per negative.
* **NegPy's dropped controls** (bound trims, lock bounds, roll averages) as
  edit-page candidates.
* **Re-measure `STITCH_UNITS_PER_NEGATIVE`** (now 10, was 9) with
  `scripts/measure-registration.py` rather than asserting the +1.
