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

## ProPhoto-v4.icc (ROMM RGB / ProPhoto RGB ICC colour profile)

Embedded in every TIFF Scanny Boy writes, at
`cli/src/scanny_boy/resources/ProPhoto-v4.icc`.

- Source: <https://github.com/saucecontrol/Compact-ICC-Profiles/blob/master/profiles/ProPhoto-v4.icc>
- Licence: CC0 1.0 Universal (public domain dedication).
- SHA-256: `090daf740c136b4a63bf979d64f034b4a65aa5abbb04a0917729222afe2bb5c2`
  (verified at startup by `scanny_boy.icc_profile`).

Python runtime dependencies (`rawpy`, `numpy`, `tifffile`, `imagecodecs`,
`exifread`, `tifftools`, and their own dependencies) are used under their
respective upstream licences and are not relicensed by this project.
