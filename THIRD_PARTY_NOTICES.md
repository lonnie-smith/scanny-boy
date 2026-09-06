# Third-party notices

Scanny Boy's own code is all rights reserved — see [`LICENSE`](LICENSE).
This project bundles or embeds the following third-party assets under
their own licences.

## LibRaw

Bundled inside the packaged `scanny-boy` program as a shared library
(`libraw_r.25.dylib`), via the `rawpy` Python package.

- Project: <https://www.libraw.org/>
- Licence: dual-licensed under the LGPL 2.1 or the CDDL 1.0, at the user's
  choice. Both licences require attribution when the library is
  redistributed. Scanny Boy uses LibRaw only as a shared library, which
  satisfies the redistribution terms of both licences.
- No modifications have been made to LibRaw's source.

## ScannyBoy-Linear-v1.icc (linear ICC colour profile with wide-container colorants)

Embedded in every TIFF Scanny Boy writes, at
`cli/src/scanny_boy/resources/ScannyBoy-Linear-v1.icc`.

- Derived from the CC0 `ProPhoto-v4.icc` profile in Compact ICC Profiles
  (<https://github.com/saucecontrol/Compact-ICC-Profiles/blob/master/profiles/ProPhoto-v4.icc>):
  primaries, white point, and `chad` are copied byte for byte and used as a
  deliberately **wide container** — not a measurement of any camera's
  primaries, whose camera-native pixels the container keeps a viewer from
  clipping; only the transfer curve (`rTRC`/`gTRC`/`bTRC`) is replaced with
  a parametric type 0 curve of g = 1.0 — the identity — so the profile
  declares the linear pixels the decode writes. Generated deterministically
  by `cli/tools/generate_icc_profile.py`.
- Licence: CC0 1.0 Universal for the upstream profile; Scanny Boy's derivation
  is all rights reserved (see [`LICENSE`](LICENSE)).
- SHA-256: `f2253a1355ad41246c5126a601d147290e5efd04fb2bca6fa44e24179c525536`
  (verified at startup by `scanny_boy.icc_profile`).

## ScannyBoy-Export-AdobeRGB-v1.icc / ScannyBoy-Export-Grey-v1.icc (the export profiles)

Embedded in every JPEG XL Scanny Boy exports, at
`cli/src/scanny_boy/resources/ScannyBoy-Export-AdobeRGB-v1.icc` and
`cli/src/scanny_boy/resources/ScannyBoy-Export-Grey-v1.icc`.

- The colorimetry (primaries, white point, chromatic adaptation, and the
  563/256 transfer curve) is the published Adobe RGB (1998)
  specification, cross-checked against the colorant values in Apple's
  `/System/Library/ColorSync/Profiles/AdobeRGB1998.icc`. **The bytes are
  this project's own** — generated deterministically by
  `cli/tools/generate_icc_profile.py` from the published numbers — and
  the profiles are not an Adobe product and not derived from Adobe's
  profile. Their `desc` tags say so, and describe the colour space as
  *compatible with* Adobe RGB (1998) for the same reason: "Adobe RGB" is
  Adobe's trademark, and the colour space here is an independent profile
  with the same colorimetry (docs/EXPORT_PLAN.md section 2.3).
- Licence: Scanny Boy's own, all rights reserved (see
  [`LICENSE`](LICENSE)). The colorimetry is a published specification.
- SHA-256: `85fc817bb230d5617578087e35d79e5e89cbae0930b9929e639690a44973a25c`
  (RGB) and `87a776bac58693beb3de9a6effa0ce82177fe326f0813a5cf888382282a4f86d`
  (Grey), verified at startup by `scanny_boy.icc_profile`.

## OpenCV

Used at build time and runtime for feature detection, matching, and image
warping, via the `opencv-python-headless` Python package.

- Project: <https://opencv.org/>
- Licence: Apache License 2.0.
- No modifications have been made to OpenCV's source.

## SciPy

Used at runtime for the geometric calibration fit
(`scipy.optimize.least_squares`), via the `scipy` Python package.

- Project: <https://scipy.org/>
- Licence: BSD 3-Clause.
- No modifications have been made to SciPy's source.

Python runtime dependencies (`rawpy`, `numpy`, `tifffile`, `imagecodecs`,
`exifread`, `tifftools`, `opencv-python-headless`, `scipy`, and their own
dependencies) are used under their respective upstream licences and are not
relicensed by this project.
