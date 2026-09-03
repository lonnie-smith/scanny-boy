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

## ScannyBoy-Linear-ProPhoto-v1.icc (linear ProPhoto ICC colour profile)

Embedded in every TIFF Scanny Boy writes, at
`cli/src/scanny_boy/resources/ScannyBoy-Linear-ProPhoto-v1.icc`.

- Derived from the CC0 `ProPhoto-v4.icc` profile in Compact ICC Profiles
  (<https://github.com/saucecontrol/Compact-ICC-Profiles/blob/master/profiles/ProPhoto-v4.icc>):
  primaries, white point, and `chad` are copied byte for byte; only the
  transfer curve (`rTRC`/`gTRC`/`bTRC`) is replaced with a parametric
  type 0 curve of g = 1.0 — the identity — so the profile declares the
  linear pixels the decode writes. Generated deterministically
  by `cli/tools/generate_icc_profile.py`.
- Licence: CC0 1.0 Universal for the upstream profile; Scanny Boy's derivation
  is all rights reserved (see [`LICENSE`](LICENSE)).
- SHA-256: `a739982a10dc1b9de27dd262c4d7a8269c2a48ec42c4eb3743e1a108c6a8d744`
  (verified at startup by `scanny_boy.icc_profile`).

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
