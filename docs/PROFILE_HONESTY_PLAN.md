# Profile honesty: retiring the ProPhoto claim on the three bundled profiles

A rename and a re-description. **No pixel changes anywhere.** It is split
out of `docs/EXPORT_PLAN.md` — which discovered the problem — because it
shares nothing with that plan mechanically and because it is the one change
in either document that **breaks the roll invariants** and makes existing
rolls unstitchable. Bundling it into the export work would have put a
data-destroying step in the middle of a plan about file formats.

Do this before or after `EXPORT_PLAN.md`, whenever existing rolls are
expendable. Neither plan depends on the other.

---

## 1. The problem

`raw_decode.RAW_PARAMS` sets `output_color=raw` and `user_wb=[1,1,1,1]`, so
the pixels in every intermediate and every published TIFF are the camera's
own filter responses — they have never been converted into any colorimetric
space at all. The three bundled profiles nonetheless carry ProPhoto
primaries, and one of them says so in its description:

> `LINEAR_DESCRIPTION = "Scanny Boy Linear RGB (ProPhoto primaries, linear TRC)"`

That is a falsehood stated outright. `DENSITY_DESCRIPTION` is candid about
its TRC ("a viewing convention ... not a colorimetric claim") but silent on
primaries, which reads as tacit endorsement.

`docs/DECISIONS.md` (D-2, "Two ICC profiles, and the profile is never
load-bearing") is candid that the TRC is a viewing convention. It is not
candid that the *primaries* are equally invented.

## 2. What cannot be removed

ICC has no way to express "unknown primaries". A matrix/TRC profile must
carry `rXYZ`/`gXYZ`/`bXYZ`, so every tagged file asserts *something*. Nor
can the tag simply be dropped: `tiff_writer.write_base_tiff` raises on an
empty profile by design, and both `icc_profile_sha256` and
`published_icc_profile_sha256` are roll invariants
(`roll_manifest.py:388-394`, seeded at `probe.py:238-239`) — deleting
invariant fields is a larger change than changing their values, for a worse
result. An untagged file does not read as "unknown" to a viewer; it reads
as sRGB, which is a *different* false claim with no documentation attached.

**The bytes of the colorants therefore do not change.** A wide container is
the right choice for a debugging tag, ProPhoto's primaries are a perfectly
good wide container, and changing them would alter how every existing
viewer renders the intermediates for no gain.

## 3. What is removed: the assertion

1. Rename the three files, dropping "ProPhoto":
   `ScannyBoy-Linear-v1.icc`, `ScannyBoy-Density-v1.icc`,
   `ScannyBoy-Density-Grey-v1.icc` (the grey one has no primaries at all
   and is renamed only for consistency).
2. Rewrite the descriptions in `cli/tools/generate_icc_profile.py` to say
   what the colorants actually are: a deliberately **wide container**,
   chosen so that a viewer applying the profile does not clip camera-native
   values, and *not* a measurement of this camera's primaries. Name
   `raw_decode.RAW_PARAMS` as the reason.
3. Re-pin the three SHA-256 constants in `icc_profile.py` and update the
   filename constants.
4. Update `icc_profile.py`'s module docstring, which names all three files.
5. Rename `test_primaries_white_point_and_chad_are_byte_identical_to_prophoto`
   (`icc_profile_test.py:177`). Keep what it asserts — the colorant bytes
   are still carried over from the vendored ProPhoto source, and that is
   still the thing to hold stable — but the name should say "the wide
   container's colorants", not treat ProPhoto as a claim being verified.

## 4. The invariant break

Both bundled SHAs move, so every existing roll fails
`check_roll_invariants`. Under the project's no-migration decision that is
acceptable, and it must be a loud, legible break: the existing
`ROLL_INVARIANT_MISMATCH` path already names the field that differs, and
that is sufficient. **Do not add a shim.** Confirm the rolls are expendable
before taking this step.

## 5. `DECISIONS.md` (D-2)

The table's `Claim` row ("true — the pixels are linear") conflates two
claims. The linear profile's *TRC* claim was always true; its *primaries*
claim never was. Split the row:

| | Intermediates | Published TIFF |
| --- | --- | --- |
| TRC | true (linear) | viewing convention |
| Primaries | wide container, not a measurement | wide container, not a measurement |

Record that the program stopped asserting something it cannot support, and
that the colorant bytes were deliberately left alone.

## 6. Tests

- The three renamed profiles load and verify against the re-pinned hashes.
- Neither any filename nor any rewritten description contains the string
  "ProPhoto".
- All three rewritten descriptions state that the colorants are a wide
  container and not a measurement.
- The renamed colorant test still passes: the colorant, white point and
  `chad` bytes are unchanged from the vendored source, so only the
  description and filename moved.
- `test_generator_reproduces_the_committed_profiles` and
  `test_generator_is_deterministic` still pass against the new filenames.
- The packaging test that enumerates bundled profiles names the new files.

## 7. Out of scope

**Generating the intermediates' ICC profile per camera body**, which would
make that one profile fully *true* rather than merely honestly labelled.
`EXPORT_PLAN.md` §3 records `camera_color.rgb_xyz_matrix`, which is the
prerequisite; with it, the intermediates' profile could carry the sensor's
real primaries and a linear TRC.

Two reasons it is not done here:

- It only half-helps the published TIFF. Its primaries would become true,
  but its TRC is normalized log density, which no ICC TRC expresses, and
  the per-channel normalization has moved its effective white point. That
  profile cannot be fully true whatever is done to the colorants.
- It converts two bundled build-time constants into runtime-generated
  bytes, which touches the invariant seeding in `probe.py`, `pipeline.py`
  and `stitch_pipeline.py`, the `generate_icc_profile.py` tool, and the
  packaging tests that enumerate bundled resources.

Record it on `punchlist.md` as "generate the intermediates' ICC profile per
camera from `camera_color.rgb_xyz_matrix`", with the note that the
attachment point is `probe.py`'s invariant seeding.
