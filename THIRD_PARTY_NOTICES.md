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
- SHA-256: `3b5600f280c5686b08409a10a803f7a2770cc59840681cacef7f02a41d1ea2f3`
  (verified at startup by `scanny_boy.icc_profile`).

## ScannyBoy-Export-AdobeRGB-v1.icc / ScannyBoy-Export-Grey-v1.icc (the export profiles)

Embedded in every JPEG XL Scanny Boy exports, at
`cli/src/scanny_boy/resources/ScannyBoy-Export-AdobeRGB-v1.icc` and
`cli/src/scanny_boy/resources/ScannyBoy-Export-Grey-v1.icc`.

- The colorimetry (the D65 white point, the CIE xy primaries, and the
  563/256 transfer curve) is the published Adobe RGB (1998)
  specification. **The bytes are this project's own** — generated
  deterministically by `cli/tools/generate_icc_profile.py`, which hands
  those published chromaticities to lcms2 (via `imagecodecs.cms_profile`)
  and sets only the description, the copyright and the grey profile's D50
  white point and `chad`. Neither Adobe's ICC file nor any part of it is
  redistributed, and the profiles are not an Adobe product and not
  derived from Adobe's profile. Their `cprt` tags say so, and describe
  the colour space as *compatible with* Adobe RGB (1998) for the same
  reason: "Adobe RGB" is Adobe's trademark, and the colour space here is
  an independent profile with the same colorimetry
  (docs/EXPORT_PLAN.md section 2.3).
- Licence: Scanny Boy's own, all rights reserved (see
  [`LICENSE`](LICENSE)). The colorimetry is a published specification.
- SHA-256: `1e399e18f9f6dbba2ecaa053251a51509ca03bd8e0f5168e6675eb4ad0ea250c`
  (RGB) and `c20576031ab3b1cca6ec7949d74f6fe72bc13b81ee41117f6ac067c088adaccf`
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
