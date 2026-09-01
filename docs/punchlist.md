
* ~~The embedded ICC profile does not match the pixels' actual transfer curve.
  `ProPhoto-v4.icc` declares true ROMM (encoded breakpoint 0.03125), but
  rawpy's `gamma=(1.8, 16)` writes LibRaw's generalised curve instead — a
  different breakpoint plus an offset term (measured 2026-08-29; see Phase 2
  plan section 2.3.1). A viewer honouring the profile renders every Phase 1
  TIFF too dark in the shadows: about 360 LSB at a linear value of 0.05
  (−5.2%), rising to −20.8% at 0.005 and falling to nothing at white. Phase 2
  reads the pixels correctly and is unaffected.~~ **Fixed in Phase 3 P3-1:**
  `ScannyBoy-ROMM-LibRaw-v4.icc` embeds LibRaw's curve; no pixel values change.
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
* Setting a roll's capture date or a per-negative date override, and editing
  an unlocked roll's `shots_per_negative`, from the app. Phase 3's Edit tab
  (P3-12) shows all three read-only: no CLI command writes
  `metadata.roll_capture_date`, a negative's `capture_time.date_override`,
  or an existing roll's `shots_per_negative` — see Phase 3 plan §5.6. A
  `roll set-date` command, by analogy with `roll rename` (§5.5), is the
  likely shape of the fix.

Phase n: 
Negative inversion

might need to put the tiff in linear (gamma 1, 1???) instead of whatever gamma rawpy gives us on conversion right now. 

Eventual:
- would it be easy to convert this to an electron app for better cross-platform compatibility (plus familiarity to me)
