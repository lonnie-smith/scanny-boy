
* The embedded ICC profile does not match the pixels' actual transfer curve.
  `ProPhoto-v4.icc` declares true ROMM (encoded breakpoint 0.03125), but
  rawpy's `gamma=(1.8, 16)` writes LibRaw's generalised curve instead — a
  different breakpoint plus an offset term (measured 2026-08-29; see Phase 2
  plan section 2.3.1). A viewer honouring the profile renders every Phase 1
  TIFF slightly wrong, about 360 LSB at a linear value of 0.05. Phase 2 reads
  the pixels correctly and is unaffected. Fixing it means either writing a
  profile whose curve matches LibRaw's, or decoding linear and encoding true
  ROMM ourselves — both change Phase 1's pixel output, so neither belongs in
  Phase 2.
* `probe --out` has no notion of `scanny-boy-roll.json` (Phase 2's stitched
  roll manifest) — only `scanny-boy-manifest.json`. It reports a folder
  holding only a roll manifest as `OUTPUT_NOT_EMPTY` rather than recognising
  it as a legitimate rerun/re-stitch target. The app works around the
  resulting false positive client-side (`ConfigurationModel.existingRoll`),
  but this means the app can't show an itemized preview of what a rerun or
  re-stitch would actually replace, the way it already does for a plain
  `convert`/`run` — it can only ask for one general, explicit
  acknowledgement before passing `--overwrite`. Fixing it means generalising
  `probe`'s output-folder handling over which manifest it's reading, the way
  Chunk P2-6 already generalised `output_folder.py` itself.

Phase 3:

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

Phase 4: 
Negative inversion

Eventual:
- would it be easy to convert this to an electron app for better cross-platform compatibility (plus familiarity to me)
