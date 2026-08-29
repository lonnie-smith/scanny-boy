
* Save the last-opened input folder and use it as the opening point for browsing for a new input folder. If the previous input folder is no longer present, walk up the path until you find a directory that is still present.
* should we rename the manifest?
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

Phase 3:

* Maybe change border fill-in to cyan or some contrasting color
* extended metadata editing (location, camera, lens, film stock)
* Crop based on manifest data, maybe lock in an appropriate aspect ratio
* White balance / base neutralization

Phase 4: 
Negative inversion

Eventual:
- would it be easy to convert this to an electron app for better cross-platform compatibility (plus familiarity to me)
