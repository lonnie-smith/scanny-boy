
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

* extended metadata editing (location, camera, lens, film stock)
* Crop based on manifest data, maybe lock in an appropriate aspect ratio
* White balance / base neutralization
* Flat-field deferred pieces (see FLATFIELD_PLAN.md §4 for what did ship).

Geometric calibration (docs/GEOMETRIC_PLAN.md, protocol version 7) deferred
pieces:

* **Rename the `--flatfield` flag / command family.** The flag now names a
  whole calibration profile (gain map + distortion + CA), not just a
  flat-field gain map. A rename reaches `cli.py`, `CONTRACT.md`,
  `schema.json`, `CLIRunner.swift`, `ConfigurationModel.swift`, and the
  stored-defaults key — cosmetic, and deliberately not done in the same
  change as the substance.
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
  shape of the fix. (Partially shipped: the clamp reads the per-negative
  manifest blocks, not the aggregate; a user-facing run-median *mode*
  would supersede the clamp.)
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
* **The eight unmeasured dense-border constants** (`DENSE_BORDER_*`,
  `CLAMP_*`) — provisional and unmeasured, shipped against one diagnosed
  roll (R1). Same disposition as the rebate five: run over real rolls with
  a dump of `dense_border.detected`/`mask_fraction` and
  `clamped`/`unclamped_floors`, then measure.
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
