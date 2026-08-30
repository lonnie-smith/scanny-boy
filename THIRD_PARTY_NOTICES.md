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

## ScannyBoy-ROMM-LibRaw-v4.icc (ROMM RGB ICC colour profile)

Embedded in every TIFF Scanny Boy writes, at
`cli/src/scanny_boy/resources/ScannyBoy-ROMM-LibRaw-v4.icc`.

- Derived from the CC0 `ProPhoto-v4.icc` profile in Compact ICC Profiles
  (<https://github.com/saucecontrol/Compact-ICC-Profiles/blob/master/profiles/ProPhoto-v4.icc>):
  primaries, white point, and `chad` are copied byte for byte; only the
  transfer curve (`rTRC`/`gTRC`/`bTRC`) is replaced with LibRaw's generalised
  curve so the profile matches the pixels rawpy writes. Generated deterministically
  by `cli/tools/generate_icc_profile.py`.
- Licence: CC0 1.0 Universal for the upstream profile; Scanny Boy's derivation
  is all rights reserved (see [`LICENSE`](LICENSE)).
- SHA-256: `18760274dbf58e150f5d3d391a762b51ad7799b26dac5acc4d74289d70998575`
  (verified at startup by `scanny_boy.icc_profile`).

## OpenCV

Used at build time and runtime for feature detection, matching, and image
warping, via the `opencv-python-headless` Python package.

- Project: <https://opencv.org/>
- Licence: Apache License 2.0.
- No modifications have been made to OpenCV's source.

Python runtime dependencies (`rawpy`, `numpy`, `tifffile`, `imagecodecs`,
`exifread`, `tifftools`, `opencv-python-headless`, and their own
dependencies) are used under their respective upstream licences and are not
relicensed by this project.
