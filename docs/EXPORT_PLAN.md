# Export plan: a rendered positive, in Adobe RGB, as lossless JPEG XL

Three changes to the export path, in order. First a **JPEG XL writer** that
can do what the export needs and `imagecodecs` cannot — embed an arbitrary
ICC profile in a 16-bit lossless file. Then the **output colour space**: two
generated profiles, an Adobe RGB (1998) compatible one and its grey
companion, added alongside the three existing profiles. Then the
**render**: the published TIFF's normalized log density becomes a positive,
in Adobe RGB, with the negative's recorded tone op baked in — which is the
first time export owns pixel-level tone, and retires the "Phase 4 print
stage" placeholder for this one path.

This plan follows the conventions of `docs/MONOCHROME_PLAN.md`,
`docs/GEOMETRIC_PLAN.md` and `docs/STITCH_QUALITY_PLAN.md`: every constant
lives in exactly one module, every fact that shapes an output is recorded in
the roll manifest so a file can be interpreted without knowing which build
wrote it, and every rejected alternative is written down beside the one that
was taken so a later reader does not "fix" a deliberate choice.

Two things this plan assumes, both settled with the user before it was
written:

- **The export becomes a rendered positive**, tone included. Not a
  negative, and not the same pixels with a new label.
- **There is no migration.** Existing rolls will be deleted rather than
  carried forward. Sections that would otherwise need a compatibility shim
  therefore fail loudly instead, and say so.

**Not in this plan:** renaming the three existing ProPhoto-named profiles.
§0.1 exposes that they assert primaries the pixels never had, but fixing it
shares nothing with this work mechanically and is the only change that
breaks the roll invariants. It is `docs/PROFILE_HONESTY_PLAN.md`, and
neither plan depends on the other. *(Update, on landing here: §3 of
PROFILE_HONESTY_PLAN — the renames and re-pinned hashes — has already been
carried out, so the profiles are now described as a "wide container, not a
measurement" rather than claiming ProPhoto. §0.1's colour-space point
survives unchanged: the pixels are still the camera's own filter responses
with no colorimetric conversion, which is exactly what §3's matrix
addresses.)*

---

## 0. Why this shape, and why in this order

### 0.1 What export is today

`exporter.run_export` (`exporter.py`) reads each negative's published TIFF,
replays its geometric ops (`repo.net_edit_state`'s `quarter_turns`,
`flipped`, `fine_angle` — the `tone` element is destructured into `_tone`
and thrown away at `exporter.py:_export_negative`), writes the pixels
straight through with `tifffile.imwrite` plus
`ProfileKind.DENSITY`/`DENSITY_GREY`, and then reopens the file with
`tifftools` to add EXIF and XMP (`export_metadata.write_export_metadata`).

So today's exported file is: **normalized log density, still a negative,
tagged with a profile whose primaries and g = 2.2 TRC describe nothing the
pixels were ever converted into.** (`docs/DECISIONS.md`'s D-2 calls the
colorants a "deliberately wide container, not a measurement" — candid, but
the consequence stands.) `raw_decode.RAW_PARAMS` sets `output_color=raw`
and `user_wb=[1,1,1,1]`, so the channels are the camera's own filter
responses and have never been converted into any colorimetric space at all.

Both halves of that situation end here, for the export path only. The export
gets its own colour-managed profile; the density profile keeps its pixels
and its wide-container label, which `PROFILE_HONESTY_PLAN.md` §3 has
already renamed and §5 rewords further.

### 0.2 What it becomes

```
published TIFF (uint16 normalized log density, negative, 1 or 3 channels)
  -> decode_normalized                       val in [0, 1] + headroom
  -> 1 - val, clipped                        positive, normalized log exposure
  -> ^ GAMMA_ADOBE                           linear light
  -> 3x3 matrix (colour only)                camera primaries -> Adobe RGB
  -> clip to [0, 1]
  -> ^ (1 / GAMMA_ADOBE)                     back to display encoding
  -> tone + colour curve (the negative's `tone` and `color` ops)
  -> dye separation when active
  -> uint16
  -> lossless JPEG XL, ICC = Adobe RGB (1998) compatible (or Grey)
     + Exif box + XMP box
```

The two exponentiations bracket the matrix and invert each other, so **the
chain reproduces `tone.build_display_lut`'s rendering at 16 bits instead of
8.** That is the regression anchor §4.7 tests against, and the reason the
exported file looks like the Edit tab's preview rather than a second,
differently-tuned rendering.

They invert *mathematically*, not bit-for-bit: `x ** g ** (1/g)` is not
exact in float32, and §4.1's colour path quantizes to uint16 before the tone
LUT. §4.1 therefore makes the no-matrix path a single fused LUT with no
exponentiation at all, so the anchor is exact where it can be and bounded at
4 codes where it cannot.

### 0.3 What "the matrix conversion" means, in plain language

*(Requested explicitly. Reproduce the substance of this in
`DECISIONS.md` — it is the part of this plan most likely to be
misunderstood and "corrected" later. Everywhere else in this plan that
touches the matrix points back here rather than re-explaining it.)*

Every camera sensor sees colour slightly differently. Its red photosite is
not measuring "red"; it is measuring "whatever got through this particular
red filter", and the same is true of green and blue. Point two different
camera bodies at the same negative under the same light and they record
different numbers for the same colour. Neither is wrong — they are just two
different instruments reporting in their own units.

Up to now this program has treated those three sensor numbers *as if* they
were already red, green and blue in a known colour space. First ProPhoto,
and the export inherited that claim. It was never true. It was a label with
nothing behind it, and it is a large part of why exports have been awkward
in other software: a program that honours the profile is applying a
ProPhoto interpretation to numbers that were never ProPhoto.

The matrix is the correction, and it comes from the camera itself. LibRaw
ships, for every body it supports, a small measured table describing how
much of each real-world primary each sensor channel actually responded to.
Multiplying the three sensor numbers by that table converts them out of
"what this Nikon's filters recorded" and into a device-independent
description of the colour (CIE XYZ). A second, fixed table converts XYZ into
Adobe RGB's red, green and blue. Compose the two and the result is a single
3x3 matrix per camera body: sensor RGB in, Adobe RGB out. Nine numbers, one
matrix multiply per pixel.

What it does to a picture: mostly it corrects **saturation and hue**. Camera
filters overlap, so a pure red in the scene leaks a little into the green
channel and comes back reading slightly orange and slightly washed out. The
matrix subtracts that leakage back out. It is a rotation and mild stretch of
the colour cube. It is **not** a brightness, contrast, or white-balance
change — those are §4's tone curve and the stitch stage's per-channel
normalization respectively, and the matrix must not be allowed to duplicate
either of them.

One thing it deliberately does not do: **move neutrals.** The stitch stage's
per-channel normalization is what removes the orange mask and sets grey to
grey; if the matrix were applied raw it would tint that carefully
established neutral. So the matrix is row-normalized — each row scaled so
that its three entries sum to one — which guarantees that an equal-parts
input `(x, x, x)` comes out as `(x, x, x)`. The matrix then acts purely on
the *difference* between the channels, which is exactly the sensor-crosstalk
part it is qualified to fix, and leaves the part the normalization already
decided alone. **This is the single most likely thing for a future reader to
"simplify" back into a plain matrix multiply, which would silently re-tint
every export.** §4.3 states it as a testable invariant.

**The honest caveat, to be recorded and not quietly dropped.** The matrix
characterizes the camera's response to *light*. Here it is applied after the
negative has been per-channel normalized, inverted, and linearized against
the output TRC — which is not the same quantity the matrix was measured
against. It is a first-order correction, not a colorimetric
characterization of the film. Colour negative film's dye densities are not a
colorimetric measurement of the original scene and no 3x3 matrix makes them
one. What the matrix removes is the part that *is* well defined and *is*
per-camera — the sensor's own crosstalk. Removing it is strictly better than
pretending it is not there, which is the status quo. Do not let a future
reader conclude from "it is approximate" that the previous state (an
invented ProPhoto label) was somehow safer.

### 0.4 The order is mandatory

§1 before everything, because the encoder is the only unproven mechanism in
the plan and every later section's tests write files through it. §2 before
§4, because the render's TRC constant and the profile's TRC tag are the same
number and must be defined in one place before anything consumes it. §3
before §4, because the render cannot run without the matrix and must not
silently fall back to an identity one. §5 is the exporter rewire and needs
§1–§4 complete. §6–§8 (contract, Swift, packaging) follow §5.

§0.5 comes before all of it.

### 0.5 Before writing any code: confirm Lightroom reads JPEG XL

The whole point of this change is that the files land well in Adobe
Lightroom, and the installed copy here is Lightroom desktop 9.5.1, whose
`Info.plist` claims a wildcard document type and therefore proves nothing
either way.

**First task, before any of §1:** produce one 16-bit lossless JXL with an
Adobe RGB profile — the prototype in §1.3 is enough, no product code needed
— and import it into Lightroom. Confirm it opens, that the colour is not
shifted, and that the XMP caption/city survive.

If it does not import, **stop and report**, do not carry on. The rest of
this plan is still correct in shape but the container decision has to be
revisited (16-bit lossless AVIF and 16-bit PNG are the plausible
alternatives, and both would keep §2, §3 and §4 unchanged). Do not silently
substitute a format.

*(Update, on landing here: the prototype — 900×600, 16-bit lossless,
Apple's Adobe RGB profile, Exif + XMP boxes — was confirmed in Lightroom
desktop 9.5.1: it opens with correct colour and the caption survives.
`sips` reports `profile: Adobe RGB (1998)`, `bitsPerSample: 16`, and
`exiftool` reads both boxes. The gate passed; §5 proceeded.)*

macOS itself is known good: `sips` reads the prototype files and reports
`profile: Adobe RGB (1998)`, `bitsPerSample: 16`, and `exiftool` reads both
the Exif and XMP boxes.

---

## 1. The JPEG XL writer

### 1.1 Why a ctypes shim and not `imagecodecs`

`imagecodecs` is already a dependency, already bundles libjxl 0.12.0, and
`imagecodecs.jpegxl_encode` produces correct 16-bit lossless files. It
**cannot embed an ICC profile**: its signature is `(data, level, effort,
distance, lossless, decodingspeed, photometric, bitspersample, planar,
primaries, transfer, usecontainer, numthreads, out)`, and `primaries` /
`transfer` are CICP enum codes. Adobe RGB is not expressible as a CICP
primaries code, so the enum path cannot satisfy the requirement even in
principle.

Alternatives considered and rejected:

- **`pillow-jxl-plugin`.** Its encoder takes no ICC profile (it only
  *decodes* one), and it routes through Pillow, which has no 16-bit RGB
  mode at all. Twice unusable.
- **`jxlpy`.** Source-only distribution; needs `jxl/types.h` at install
  time. Cannot be bundled.
- **The `cjxl` binary.** A second executable to ship, sign and notarize
  inside the app bundle, for one function call.
- **Patching an ICC into the container after the fact.** JPEG XL is not
  HEIF: there is no `colr` box. The colour encoding lives inside the
  codestream header, bit-packed, and an ICC profile there is stored
  compressed. Rewriting it post-hoc is not a byte splice.

So: a small ctypes shim over the libjxl that is already in the process.
`JxlEncoderSetICCProfile` is exported by the bundled dylib.

### 1.2 Locating the library

Do **not** compute a path to `imagecodecs/.dylibs/libjxl.0.12.0.dylib`. That
path is correct in a development checkout and wrong inside the PyInstaller
bundle, and it pins a version number.

Instead, import `imagecodecs._jpegxl` — which causes the dynamic linker to
load libjxl, libjxl_threads and libjxl_cms into the process — and then open
the **global** symbol namespace:

```python
import ctypes
import imagecodecs._jpegxl  # noqa: F401 — loads libjxl into the process

_jxl = ctypes.CDLL(None)
```

Verified: after that import, `JxlEncoderCreate`, `JxlEncoderSetICCProfile`,
`JxlEncoderAddBox`, `JxlThreadParallelRunnerCreate`,
`JxlThreadParallelRunnerDefaultNumWorkerThreads` and `JxlGetDefaultCms` all
resolve through `CDLL(None)`. This is path-independent, version-agnostic,
and works identically frozen (§8).

Wrap the import and the first symbol lookup, and raise a dedicated
`JxlEncoderUnavailable` (mapping to a new `JXL_ENCODER_UNAVAILABLE` code)
rather than letting an `AttributeError` escape. A packaging regression must
say what broke.

### 1.3 The encode sequence

New module `cli/src/scanny_boy/jxl_writer.py`. This sequence is **proven
working** — it was run end to end against the bundled libjxl before this
plan was written, and produced 16-bit lossless files that round-trip
bit-identically through `imagecodecs.jpegxl_decode` and that macOS reports
as Adobe RGB (1998). Follow it exactly; the four footguns are called out.

```
JxlThreadParallelRunnerCreate(NULL, JXL_ENCODE_THREADS)
JxlEncoderCreate(NULL)
JxlEncoderSetParallelRunner(enc, JxlThreadParallelRunner, runner)
JxlEncoderSetCms(enc, JxlGetDefaultCms())
JxlEncoderUseContainer(enc, JXL_TRUE)
JxlEncoderUseBoxes(enc)                       # only when metadata is written
JxlEncoderInitBasicInfo(&info)
  info.xsize, info.ysize = width, height
  info.bits_per_sample = 16
  info.exponent_bits_per_sample = 0
  info.uses_original_profile = JXL_TRUE       # required for true lossless
  info.num_color_channels = 1 or 3
  info.num_extra_channels = 0
  info.alpha_bits = 0
JxlEncoderSetBasicInfo(enc, &info)
JxlEncoderSetICCProfile(enc, icc_bytes, len)  # AFTER SetBasicInfo
JxlEncoderAddBox(enc, b"Exif", b"\x00\x00\x00\x00" + tiff_bytes, n, 0)
JxlEncoderAddBox(enc, b"xml ", xmp_bytes, n, 0)
JxlEncoderCloseBoxes(enc)
fs = JxlEncoderFrameSettingsCreate(enc, NULL)
JxlEncoderFrameSettingsSetOption(fs, JXL_ENC_FRAME_SETTING_EFFORT, EFFORT)
JxlEncoderSetFrameLossless(fs, JXL_TRUE)
JxlEncoderSetFrameDistance(fs, 0.0)
JxlEncoderAddImageFrame(fs, &fmt, buf, nbytes)
JxlEncoderCloseInput(enc)
loop JxlEncoderProcessOutput until JXL_ENC_SUCCESS
JxlEncoderDestroy(enc); JxlThreadParallelRunnerDestroy(runner)
```

**Footgun 1 — `JxlDataType` is not densely numbered.** `JXL_TYPE_FLOAT = 0`,
`JXL_TYPE_UINT8 = 2`, `JXL_TYPE_UINT16 = 3`, `JXL_TYPE_FLOAT16 = 5`. Value
`1` is unassigned and produces a `JXL_ENC_ERR_GENERIC` from
`JxlEncoderProcessOutput` — *not* from `AddImageFrame`, which returns
success. Guessing `1` for uint16 costs an hour. Write the enum out with a
comment.

**Footgun 2 — every function needs `argtypes`.** An unprototyped call passes
the encoder handle as a 32-bit `int`, truncating the pointer, and segfaults
the interpreter. `JxlEncoderGetError` and `JxlEncoderDestroy` are the two
easiest to forget because their return values look uninteresting.

**Footgun 3 — the `Exif` box payload is not raw TIFF.** libjxl expects a
4-byte big-endian offset to the TIFF header first; use `\x00\x00\x00\x00`
followed by the TIFF bytes. Box types are `char[4]`, so ctypes needs
`(ctypes.c_char * 4)(*b"Exif")`, not a plain `bytes`.

**Footgun 4 — `JxlThreadParallelRunnerCreate(NULL, 0)` means zero worker
threads, not "one per core".** It encodes single-threaded and says nothing.
Call `JxlThreadParallelRunnerDefaultNumWorkerThreads()` (verified present;
returns 10 on this machine) and pass that.

`JxlBasicInfo` must be declared as a full `ctypes.Structure`. Validate the
layout in a test: call `JxlEncoderInitBasicInfo` on a zeroed instance and
assert the documented defaults come back (`bits_per_sample == 8`,
`orientation == 1`, `num_color_channels == 3`, everything else zero,
`sizeof == 204`). That test is the tripwire for a libjxl ABI change; without
it, a struct-layout drift shows up as silent pixel corruption.

### 1.4 The public surface

One function:

```python
def write_jxl(
    path: Path,
    pixels: np.ndarray,          # (H, W) or (H, W, 3) uint16
    *,
    icc_profile: bytes,          # required; never write untagged
    exif: bytes | None = None,   # TIFF bytes, no 4-byte prefix
    xmp: bytes | None = None,
) -> None
```

It writes to a `.tmp` sibling and `replace()`s on success, unlinking the tmp
on any `BaseException` — the same discipline `exporter._write_export` and
`export_metadata.write_export_metadata` already follow.

Refuse an empty `icc_profile` with a `ValueError`, mirroring
`tiff_writer.write_base_tiff`'s "never silently write untagged data" guard.

### 1.5 Constants

In `jxl_writer.py` and nowhere else — including the file extension, which is
a fact about this format and not about the exporter:

```python
JXL_EFFORT = 7          # libjxl's default; 3 is ~30% larger, 9 is much slower
JXL_BITS_PER_SAMPLE = 16
JXL_SUFFIX = ".jxl"
# Worker threads. NOT 0 — see footgun 4; 0 is zero workers, not one per core.
JXL_ENCODE_THREADS = JxlThreadParallelRunnerDefaultNumWorkerThreads()
```

Measured on the prototype: effort 7 gives roughly 1.9x over raw uint16 on
photographic content, which is comparable to the Deflate+predictor TIFF the
export writes today, at a fraction of the decode cost. Do not tune `effort`
without a measurement.

### 1.6 Tests (`jxl_writer_test.py`)

- `JxlBasicInfo` layout and defaults, as above.
- 3-channel uint16 round-trips bit-identically through
  `imagecodecs.jpegxl_decode`.
- 1-channel uint16 round-trips bit-identically.
- The embedded ICC profile is recoverable from the written file and equals
  the bytes passed in.
- `exif=` and `xmp=` payloads are present in the output and parse (assert on
  the box contents; do not shell out to `exiftool`, which is not a project
  dependency).
- An empty `icc_profile` raises.
- A failed write leaves no `.tmp` behind (patch the encoder to raise
  mid-call).
- A non-uint16 dtype and a 4-channel array both raise, with the message
  naming the shape.

---

## 2. The output colour space

### 2.1 Two new generated profiles

`cli/tools/generate_icc_profile.py` today derives all three bundled profiles
from vendored ProPhoto-v4 bytes: primaries, white point and the chromatic
adaptation tag are carried over byte-identical, and only the description and
TRC are rewritten. Adobe RGB has different primaries **and** a different
white point (D65, where ProPhoto is D50), so the carry-over trick does not
extend to it. The generator gains a second construction path that builds
the profile with **lcms2** (`imagecodecs.cms_profile`) from the published
Adobe RGB chromaticities.

> **Revised after v1 shipped.** This path originally assembled the ICC
> bytes by hand, from explicit colorant, white point and `chad` values.
> That construction shipped three defects: `chad` carried the `XYZ ` type
> signature instead of `sf32` (so a parser dispatching on the type read one
> XYZNumber and dropped six values), the required `cprt` tag was absent
> altogether, and the `desc` string was long enough that macOS reported no
> profile name at all. None are colour-science errors; all three are the
> failure mode of hand-writing a binary format with per-tag type
> signatures. lcms2 is already a runtime dependency — `imagecodecs` bundles
> it, and `jxl_writer` already loads it into the process — so the writer
> that lays out the bytes is now a mature one, and the generator supplies
> only the chromaticities and the text.

Two new files in `cli/src/scanny_boy/resources/`:

| File | Class | For |
| --- | --- | --- |
| `ScannyBoy-Export-AdobeRGB-v1.icc` | RGB, `mntr` | 3-channel exports |
| `ScannyBoy-Export-Grey-v1.icc` | GRAY, `mntr` | 1-channel (mono roll) exports |

`ProfileKind` (`icc_profile.py`) gains `EXPORT_RGB` and `EXPORT_GREY`, with
their SHA-256s pinned beside the existing three and verified on every load,
exactly as the others are. The three existing profiles are not modified by
this plan — their renames and re-pinned hashes were PROFILE_HONESTY_PLAN §3's
work, already carried out; §5 of that plan rewords their descriptions
further, separately and independently.

### 2.2 The exact tag values

The **input** to lcms2 is the published Adobe RGB (1998) specification: D65
white and the CIE xy primaries. These are the source of truth — the profile
is derived from them, rather than from s15Fixed16 integers transcribed out
of another vendor's file.

```
whitepoint (xy)   D65: 0.3127, 0.3290
primaries  (xy)   R 0.6400, 0.3300   G 0.2100, 0.7100   B 0.1500, 0.0600
gamma             563/256 = 2.19921875  (s15Fixed16 144128, exact)
```

lcms2 Bradford-adapts the colorants to D50 and writes the tags below. Pin
them; they are what the committed profiles must contain.

```
PCS               XYZ, D50 (ICC's fixed PCS)
wtpt              D50: 0.9642, 1.0000, 0.8249      ints 63190, 65536, 54061
rXYZ              0.609741, 0.311111, 0.019470     ints 39960, 20389,  1276
gXYZ              0.205276, 0.625671, 0.060867     ints 13453, 41004,  3989
bXYZ              0.149185, 0.063217, 0.744565     ints  9777,  4143, 48795
chad              Bradford D65 -> D50, type sf32
                  ints 68674, 1502, -3291,
                        1939, 64912, -1119,
                        -606,  988, 49262
r/g/bTRC          para, function type 0, g = s15Fixed16 144128
                  (one payload, registered against all three signatures)
chrm              lcms2's record of the chromaticities above
desc, cprt        §2.3
```

Every value here matches Apple's `AdobeRGB1998.icc` byte for byte except
`bXYZ`'s Z, where lcms2 rounds 0.744568 down to 48795 and Apple's file
rounds up to 48796. The difference is 1/65536 — 0.000015 — and is
colorimetrically nil.

The TRC is `para` (parametric, function type 0), not the `curv` this plan
originally specified. `para` carries the gamma in s15Fixed16, so
`TRC_G_EXPORT` is now the *only* representation of it — the u8Fixed8 `563`
that `curv` required is gone, and with it one of the three places the gamma
used to appear. It also makes all five profiles use one curve type.

The colorants are Bradford-adapted to D50; that is why `wtpt` is D50 and not
D65, and why a `chad` tag is present. Apple's own file writes a D65 `wtpt`
with no `chad`, which is the older non-conformant convention — do not copy
it. Write the conformant pair. **lcms2's gray profile writes exactly that
non-conformant pair** (D65 `wtpt`, no `chad`), so the generator replaces
both on the grey profile; the RGB profile needs no such correction.

The grey profile carries `wtpt` (D50), a single `kTRC` with the **same**
gamma, and the same `chad`. Using the same TRC as the colour profile is
deliberate: a mono roll and a colour roll of the same scene must have
identical tonality, and one TRC constant means one place to change it.

The gamma now appears in two places and must be one constant:
`icc_profile.TRC_G_EXPORT` (s15Fixed16 `144128`, exact) and
`render.GAMMA_ADOBE`, which derives from it (§4). The generator derives its
float gamma from the same integer. Do not type `2.2` anywhere in this
plan's code.

lcms2 stamps the wall-clock time into the header's creation date. The
generator pins it (`PINNED_CREATION_DATE`) so the output stays
byte-reproducible and the SHA-256 pins hold.

### 2.3 Naming

"Adobe RGB (1998)" is Adobe's trademark and Adobe's own ICC file carries its
own licence; neither is being redistributed here. The generated profile is an
independent profile with the same colorimetry, and it must say so.

The disclaimer goes in **`cprt`**, and a short name in **`desc`**:

```
desc   ScannyBoy Export RGB (Adobe RGB 1998 compatible)
cprt   Colour space compatible with Adobe RGB (1998): same primaries,
       white point and transfer function. Generated by
       cli/tools/generate_icc_profile.py; not an Adobe product and not
       derived from Adobe's profile.
```

This plan originally put the whole disclaimer in `desc`, on the reasoning
that `desc` is what a colour-management tool shows. That reasoning was
right about the goal and wrong about the mechanism: **macOS returns an
empty profile description for any `desc` of 100 characters or more**
(measured — 99 displays, 100 does not), so the long `desc` achieved the
opposite of its purpose and the profile showed up unnamed everywhere in
the OS. `cprt` is a required tag, it is the tag a copyright notice
belongs in, it has no such limit, and it was missing entirely from v1 —
so moving the disclaimer there fixes the naming and the missing-tag defect
together. Keep `desc` under 100 characters.

Keep the *file* name neutral (`ScannyBoy-Export-AdobeRGB-v1.icc` is fine —
it is descriptive). Add a line to `THIRD_PARTY_NOTICES.md` recording that
the colorimetry is the published Adobe RGB (1998) specification, that the
bytes are written by lcms2, and that neither Adobe's file nor its
trademark is redistributed.

### 2.4 Tests (`icc_profile_test.py`, extending the existing patterns)

- Both new profiles load, verify, and have the pinned SHA-256.
- Parsing each profile back out yields the §2.2 tag values exactly.
- The colour profile's colour space is `RGB `, the grey profile's is `GRAY`,
  and the grey profile has a `kTRC` and no `rXYZ`.
- The two profiles' TRC gammas are equal, and equal `TRC_G_EXPORT`.
- **`chad` is asserted to carry the `sf32` type signature, not just the
  right nine numbers.** v1's `chad` held correct values under an `XYZ `
  type signature and every value-only test passed; only a type assertion
  catches that class of defect. `_chad_tag_payload` exists for this.
- **The full tag set is asserted, not just the presence of the tags the
  plan names.** v1 omitted the required `cprt` and no test noticed,
  because nothing checked what was *absent*. The RGB profile's set is
  `desc cprt chrm wtpt chad rXYZ gXYZ bXYZ rTRC gTRC bTRC`; the grey
  profile's is `desc cprt wtpt chad kTRC`.
- **`desc` is under 100 characters** and starts with the profile's name,
  and the Adobe disclaimer is asserted against `cprt` (§2.3). A test that
  only looked for the disclaimer text would pass on a `desc` macOS cannot
  display.
- The generator is byte-reproducible across runs — `PINNED_CREATION_DATE`
  is what makes this true of the lcms2 path, and the existing
  `test_generator_is_deterministic` covers it.

**Five existing tests are parametrized over `list(ProfileKind)` and will
pick up the new kinds automatically. Three of them must not:**

- `test_wide_container_colorants_white_point_and_chad_are_unchanged_from_the_vendored_source`
  (`:176` — renamed from `test_primaries_white_point_and_chad_are_byte_identical_to_prophoto`
  when PROFILE_HONESTY §3 landed) asserts colorants matching the vendored
  ProPhoto source, which the Adobe RGB profiles deliberately do not.
  Parametrize it over an explicit `LINEAR, DENSITY, DENSITY_GREY` instead.
- `test_every_description_declares_a_wide_container_not_a_measurement`
  (`:229`, added by the same commit) asserts every description says "wide
  container" and "not a measurement" — true of the three existing profiles
  and false of these two, whose colorimetry *is* the claim being made.
  Parametrize it over `LINEAR, DENSITY, DENSITY_GREY` as well.
  (`test_no_filename_or_description_claims_prophoto`, `:221`, passes
  as-is: the §2.3 description mentions Adobe RGB, not ProPhoto.)
- `test_trc_tags_share_one_offset` (`:159`) picks `kTRC` vs `rTRC/gTRC/bTRC`
  with `kind is ProfileKind.DENSITY_GREY`. Widen that to a set including
  `EXPORT_GREY`, or the grey export profile is checked for RGB TRC tags it
  does not have.
- `test_profile_header_declares_icc_v4` (`:254`, moved from `:217`)
  *should* cover the new kinds — the second construction path must write a
  v4 header too. Leave it.

The guard test (`test_guard_nothing_outside_the_write_path_imports_the_loader`)
needs **no change**: it checks which *modules* mention `load_icc_profile`,
not which kinds exist, and `exporter.py` is already in
`PROFILE_LOADER_MODULES`. Recorded here so nobody goes looking.

---

## 3. Recording the camera colour matrix

### 3.1 What LibRaw gives, and which way it points

`rawpy.RawPy.rgb_xyz_matrix` is LibRaw's `cam_xyz`: a 4x3 array whose first
`num_colors` rows form the **XYZ -> camera RGB** matrix (the DNG
`ColorMatrix` convention). The fourth row is zero for a 3-colour camera. The
name is misleading; the direction is the opposite of what it reads like.

**Verify this empirically before building on it** — this is the single fact
in the plan that could not be checked while writing it, because the sample
NEFs at `tests/fixtures/nef/` are not present in this checkout. The check:
take `M = rgb_xyz_matrix[:3]`, compute `pinv(M)`, and confirm that
`pinv(M) @ XYZ_of_D65` is a roughly equal-parts, all-positive camera triple.
If it is not, the matrix points the other way and everything downstream
transposes. Record the result of that check as a comment beside the constant.

`camera_whitebalance` and `daylight_whitebalance` are **not** used by this
plan. The stitch stage's per-channel normalization already performs the
balancing job (§0.3), and layering a second white balance on top would
double-correct. Say so in the code, because reaching for
`daylight_whitebalance` is the obvious wrong instinct here.

### 3.2 Where it is recorded

Read it at decode, in the prepare stage, beside the other curated metadata.

1. `metadata.CuratedMetadata` gains `rgb_xyz_matrix: tuple[tuple[float,
   float, float], ...] | None`, serialised as a list of lists.
   `read_camera_whitebalance`'s sibling reads it from the same `rawpy.imread`
   context — do not open the RAW a second time.
2. `manifest.py`'s work-manifest round-trip carries it, with the same
   validation shape `scan_clip_fractions` already has (`manifest.py:237`).
3. The roll manifest gains a **top-level** block, written by the stitch
   stage from the run's first source:

```json
"camera_color": {
  "rgb_xyz_matrix": [[...], [...], [...]],
  "source": "libraw",
  "camera_model": "NIKON Z 7",
  "matrix_version": 1
}
```

It is top-level rather than per-negative because it is a property of the
camera body, and a roll is shot on one rig. Add it to
`shared/contract/roll-manifest.schema.json` as an **optional** object (the
schema stays loadable), and to `manifest.schema.json` for the curated block.

Written on the roll's first run and not rewritten. On a later run whose
source reports a different matrix — a different body mid-roll — emit a new
`CAMERA_MATRIX_CONFLICT` **warning** and keep the frozen one, the same
posture `MONOCHROME_PLAN` §2.3 takes for `film.kind`. Do not raise: half the
roll may already be exported.

### 3.3 It is not a roll invariant, and that is deliberate

The obvious move is to put the matrix in `processing_params` beside the
other pixel-processing parameters. **Do not.** `processing_params` describes
the decode that produced the intermediates, and it is compared by exact dict
equality (`manifest.py:540-542` and `roll_manifest.py:553`, both through a
`_processing_params_for_*` normalizer); adding a key breaks every
existing roll's `ROLL_INVARIANT_MISMATCH` check and drags in the
`upgrade_normalize_params` shim machinery for a value the decode never uses.

The matrix affects **no published pixel**. It is read only at export. It is
therefore recorded data, not an invariant, and the invariant system stays
exactly as it is. This is the single most important structural decision in
this plan and the one most likely to be "improved" later; put the reasoning
in the field's docstring.

### 3.4 Rolls without the block

Per §0's no-migration decision: a **colour** roll whose manifest has no
`camera_color` block **fails the export** with a new `CAMERA_MATRIX_MISSING`
error whose message says the roll predates the colour-managed export and
must be re-converted (or deleted). It is a `run_export`-level
`ExportFailure`, raised once before any negative is written — not a
per-negative warning — because every negative in the roll has the same
problem and twenty identical warnings help nobody.

**A mono roll does not need the matrix and must not be failed for its
absence** (§4.5: the mono path has no matrix stage at all). The check is
therefore conditional on the roll being colour. Until `MONOCHROME_PLAN` §2's
`film` block lands there is no roll-level film kind to test, so gate on the
same fact §4.5 gates on — the published TIFF's channel count — which means
the check moves inside the per-negative loop's first iteration, or reads the
first negative's `output` dimensions before the loop. Prefer the latter, so
the "raised once, before anything is written" property survives.

Do **not** add an identity-matrix fallback for the colour path. A silent
identity matrix produces a file that claims Adobe RGB and is not, which is
precisely the bug this plan exists to remove.

### 3.5 Tests (`metadata_test.py`, `manifest_test.py`, `roll_manifest_test.py`)

- `CuratedMetadata` round-trips the matrix through the work manifest,
  including the `None` case.
- The roll manifest writes `camera_color` on the first run and does not
  rewrite it on the second.
- A second run reporting a different matrix warns
  (`CAMERA_MATRIX_CONFLICT`) and keeps the first.
- A roll manifest with no `camera_color` still loads and validates.
- `--slow`, with the sample NEFs present: the matrix read from a real NEF is
  3x3-usable, its pseudo-inverse maps D65 XYZ to a positive near-neutral
  camera triple (the §3.1 direction check), and the composed export matrix's
  rows each sum to 1 after normalization (§4.3).

---

## 4. The render

New module `cli/src/scanny_boy/render.py`. It is the only place the
published TIFF's codes become display pixels at full resolution, and it is
deliberately separate from `exporter.py` (which stays about files, edits and
error handling) and from `previews.py` (which stays 8-bit and downscaled).

### 4.1 The chain

```python
def render_export(
    image: np.ndarray,            # uint16, (H, W) or (H, W, 3)
    matrix: np.ndarray | None,    # 3x3 camera -> Adobe RGB, or None for mono
    tone_params: dict[str, float] | None,
) -> np.ndarray:                  # uint16, same shape
```

**Two paths, and the no-matrix one is a single table.**

When `matrix is None` — every mono roll, and any test that wants the anchor
— the whole render is one precomputed 65536-entry uint16 → uint16 LUT:

```python
codes = np.arange(65536, dtype=np.float64)
positive = np.clip(1.0 - normalization.decode_normalized(codes), 0.0, 1.0)
LUT = np.rint(tone_curve(positive, tone_params) * 65535).astype(np.uint16)
out = LUT[image]
```

where `tone_curve` is `tone.curve_values(v, grade_r, snap_gamma)`, or the
identity when `tone_params is None`. No exponentiation appears: with no
matrix between them the two gamma steps are a mathematical no-op, and
writing them out only costs precision. This path reproduces
`tone.build_display_lut` exactly, at 16 bits instead of 8 — §4.7's anchor is
true by construction, not by measurement.

When a matrix is present, three stages, each a separate helper:

```python
# Stage 1 — code to linear, via a 65536-entry float32 LUT.
LINEAR_LUT = np.clip(1.0 - decode_normalized(arange(65536)), 0, 1) ** GAMMA_ADOBE
linear = LINEAR_LUT[image]                       # float32 (H, W, 3)

# Stage 2 — the matrix (colour only).
linear = linear @ matrix.T
np.clip(linear, 0.0, 1.0, out=linear)

# Stage 3 — back to display, tone, quantize, via a second LUT.
display = linear ** (1.0 / GAMMA_ADOBE)
j = np.rint(display * 65535).astype(np.uint16)
out = TONE_ENCODE_LUT[j]
```

where `TONE_ENCODE_LUT[j] = rint(tone_curve(j / 65535, tone_params) * 65535)`.

Stage 1 declares the modelling assumption: *the normalized positive
log-exposure is read as an Adobe-RGB-encoded value.* That assumption is not
new — it is what the preview has always relied on implicitly by writing
`1 - val` into an 8-bit PNG and letting the viewer apply a ~2.2 decode. §4
only names it.

Stage 3's quantize-before-LUT is why the colour path is not bit-exact
against the no-matrix path: a half-code rounding error enters the tone
curve, whose slope reaches 4.0. Measured across the full grade/snap range,
the worst disagreement is **3 codes out of 65535**. §4.7 pins the bound at
4. It is not worth removing — evaluating `tone.curve_values` (tanh,
softplus) over a full-resolution array instead of a table would cost far
more than 3 codes are worth.

Work in float32 and reuse buffers.

### 4.2 Why the matrix sits between the two exponentiations

- **Not before the inversion.** The published pixels are a negative; a
  primaries matrix on negative-sense values mixes complements and is
  meaningless.
- **Not after the tone curve.** The tone curve is a strong per-channel
  nonlinearity. Mixing channels after it makes the hue of a highlight depend
  on how hard the grade was, which shows up as coloured fringes on
  specular highlights — the classic symptom of matrixing after a curve.
- **Not in true scene-linear light** (i.e. by undoing the per-channel
  normalization with the recorded `floors`/`ceils` first). That would undo
  the orange-mask removal, which is the one thing the normalization exists
  to do, and would put the film-base offset back into the pixels the matrix
  then mixed. The mask must come off per channel *before* any channel
  mixing; that ordering is not negotiable.
- **Not at stitch time**, baked into the published TIFF, for the same reason
  plus two more: it would change every published pixel, break the roll
  invariants, and make `decode_normalized` no longer the single inverse of
  the encode. The published TIFF stays what `DECISIONS.md` says it is.

### 4.3 Building the matrix

In `render.py`:

```python
# XYZ (D65) -> Adobe RGB (1998) linear. The published inverse of the
# Adobe RGB (1998) specification's RGB -> XYZ matrix.
XYZ_TO_ADOBE_RGB = np.array([
    [ 2.0413690, -0.5649464, -0.3446944],
    [-0.9692660,  1.8760108,  0.0415560],
    [ 0.0134474, -0.1183897,  1.0154096],
])

def export_matrix(rgb_xyz_matrix) -> np.ndarray:
    cam_to_xyz = np.linalg.pinv(np.asarray(rgb_xyz_matrix)[:3])   # see 3.1
    m = XYZ_TO_ADOBE_RGB @ cam_to_xyz
    return m / m.sum(axis=1, keepdims=True)        # neutral-preserving
```

That last line is §0.3's row normalization. **`m @ (1, 1, 1) == (1, 1, 1)`
is an invariant of this function** — assert it directly, and put §0.3's
one-sentence reason in the docstring: it is what makes it safe to apply a
*camera* matrix to *already normalized* channels.

Raise on a singular or non-finite matrix rather than returning something
`pinv` improvised.

### 4.4 Gamut clipping

Stage 2's clip is doing real work: the camera gamut is not contained in
Adobe RGB, so saturated colours land outside `[0, 1]` and are clipped per
channel. Per-channel clipping shifts hue slightly on the most saturated
pixels. That is the ordinary, accepted behaviour of every matrix-based
render, and the alternative (a gamut-compression curve) is a feature with
its own plan, not a detail to invent here.

`render_export` returns the per-channel clipped fraction alongside the
pixels, and §5.2 records it in the export's XMP provenance. It costs one
comparison over an array already in cache. It does **not** get an event
field, a contract change, or Swift decoding: it is a forensic record on the
file, not a user-facing signal, and adding the protocol surface for it is a
separate decision. Put "surface the gamut clip fraction in the Exported
list" on `punchlist.md` if it is ever wanted.

### 4.5 Monochrome

A mono roll's published TIFF is 2-D (`MONOCHROME_PLAN` §3, §4). Select on
`image.ndim == 2`, exactly as `exporter._write_export` already does — **not**
on a `film.kind` manifest lookup, because `MONOCHROME_PLAN` §2's `film`
block has not landed yet (only §1's detector and §4's plumbing have) and the
channel count is the fact that actually matters to the writer. When §2 does
land, the two agree by construction; add a comment pointing at that.

Mono takes §4.1's no-matrix path: one LUT, no exponentiation, no matrix
stage. There is one channel, there are no primaries to rotate, and the
published channel is a collapse (MONOCHROME_PLAN §4 — *not yet landed*; a
mono roll's single-channel TIFF cannot exist until it is) that will merge
the camera's channels with an inverse-variance weighting that is a noise
argument, not a colorimetric one. Applying a colour matrix to its output
would be inventing a luminance weighting nobody measured. The file is
tagged `EXPORT_GREY`.

### 4.6 The tone op, and what changes about it

`repo.net_edit_state` already returns the net `tone` op as its fourth
element; `exporter._export_negative` currently destructures it into `_tone`
and drops it. It stops being dropped.

This is a real change in what the `tone` op *is*. `DECISIONS.md` and
`tone.py`'s docstring both currently describe it as preview-only — "a state
the display LUT consumes, never baked into any TIFF", "not the Phase 4 print
curve, which will own the pixels at export time". After this plan, export
*is* where the tone curve owns the pixels, for this rendering. Update both
docstrings and `previews.py`'s (§9); leaving them saying "never baked" while
the exporter bakes it is the kind of stale comment that costs someone a day.

The tone curve is applied through `tone.curve_values`, the same function the
preview's LUT is built from — not a re-derivation. The preview and the
export must agree, and the only way to guarantee that is to share the
function.

`tone_params is None` (no tone op, or the reset) means the identity ramp,
i.e. the flat look the preview shows today. That is the correct default and
not a placeholder.

### 4.7 Tests (`render_test.py`)

- **The anchor, exact:** with `matrix=None`, the render's output equals
  `rint(tone.curve_values(clip(1 - decode_normalized(code)), grade, snap)
  * 65535)` for every code in `0..65535`, for several `(grade_r,
  snap_gamma)` pairs including `None`. §4.1's no-matrix path makes this true
  by construction; the test is the guard against someone reintroducing the
  gamma round-trip there.
- **The anchor, bounded:** the colour path with an identity matrix agrees
  with the no-matrix path to **within 4 codes** over the whole ramp, for the
  same grade/snap sweep. Measured worst case is 3; the bound is 4. Assert
  the bound, and assert the mean absolute difference is under 1 code, so a
  genuine regression cannot hide under a loose ceiling.
- The 8-bit preview LUT and the 16-bit export render agree to within one
  8-bit code over the whole ramp, for the same tone params.
- `export_matrix` row sums are 1; `m @ (1,1,1) == (1,1,1)` to float
  tolerance; a singular input raises.
- A synthetic neutral wedge survives the full colour chain unchanged
  (neutral in, neutral out) — the end-to-end version of the invariant above.
- A saturated primary is *changed* by a non-identity matrix (the test that
  proves the matrix is actually being applied, and not silently identity).
- `NORMALIZED_FILL` renders to code 0 (black) through the full chain: the
  fill sits above 1.0, `1 - val` is negative, the clip takes it to 0, and
  every later stage maps 0 to 0. `MONOCHROME_PLAN` §3.4 tests the same
  property on the stitch side; this is its export counterpart.
- Mono: a 2-D input returns 2-D, and its values equal the colour path's
  with an identity matrix on a grey input (within the same 4-code bound).
- The reported clip fraction is 0 for an in-gamut image and non-zero for an
  out-of-gamut one.

---

## 5. The exporter

### 5.1 `_write_export` becomes a JXL write

`exporter._write_export` currently calls `tifffile.imwrite` with
`description=`, `iccprofile=` and a `.tmp` dance. It becomes:

```python
rendered, clipped = render.render_export(rotated, matrix, tone_params)
jxl_writer.write_jxl(
    destination,
    rendered,
    icc_profile=load_icc_profile(
        ProfileKind.EXPORT_GREY if rendered.ndim == 2 else ProfileKind.EXPORT_RGB
    ),
    exif=export_metadata.build_exif(metadata) if metadata.has_any else None,
    xmp=export_metadata.build_xmp(metadata, provenance),
)
```

The `.tmp`-and-replace lives in `write_jxl` now (§1.4), so `_write_export`
loses its own copy rather than nesting two. Note the `has_any` guard: the
current code skips the metadata pass entirely when nothing is set, and "a
field nobody set writes nothing" stays true at the box level too.

### 5.2 The metadata second pass collapses into the write

This is the largest simplification in the plan and should be taken, not
skipped. `export_metadata.write_export_metadata` exists because
`tifffile.imwrite` cannot write a nested EXIF IFD, so the file had to be
reopened with `tifftools` and rewritten. JPEG XL takes metadata as boxes at
encode time, so there is nothing to reopen.

`export_metadata.py` keeps `ExportMetadata`, `export_metadata_for` and
`_xmp_packet` (all unchanged in substance) and replaces
`write_export_metadata` / `_verify` with two pure builders.

**`build_exif(metadata) -> bytes`** — a little-endian TIFF stream: IFD0 with
`ImageDescription` (270) and `Model` (272), plus the nested EXIF IFD with
`DateTimeOriginal` (36867), `SubSecTimeOriginal` (37521) and `LensModel`
(42036). Same tags, same effective-value fallback rules, same "a field
nobody set writes nothing" behaviour.

**Build it with `tifftools`, not by hand.** `tifftools` is already a
dependency and `_exif_ifd_tags` already produces exactly the dict shape it
wants; hand-rolling nested-IFD offsets is the fiddliest work in this plan
and there is no reason to do it. Assemble the same `info` dict
`write_export_metadata` assembles today, write it to an in-memory buffer,
and hand those bytes to the `Exif` box. If `tifftools` turns out to require
a real image in the IFD, write a 1x1 dummy — readers take the tags from IFD0
and ignore the strip. **Settle this on the first day of §5**, not at the end.

**`build_xmp(metadata, provenance) -> bytes`** — the existing packet, plus
the provenance record.

**Where the normalization JSON goes.** `export_image_description` currently
writes `{kind, negative_id, normalization, normalized_fill}` into
`ImageDescription`. Keep the record — it is what makes an exported file
interpretable without the database — but not there: after §4 the pixels are
a *rendered positive*, while the `normalization` block describes the
*published TIFF's* encoding, which the export no longer uses.
`ImageDescription` would invite exactly the wrong interpretation.

Put it in the XMP packet under a `scannyboy:` namespace
(`http://scannyboy.local/ns/1.0/`), as a JSON string in a
`scannyboy:provenance` property, with a `"rendered"` sibling recording what
the export actually did: the export profile name, the gamma, the matrix, the
tone params, and the clip fractions (§4.4). `ImageDescription` gets the
short human string (`"<negative_id>: Scanny Boy export"`) instead.

`dc:description` (the user's caption), `photoshop:City` and
`photoshop:State` are unchanged.

The `_verify` read-back pass goes away with the rewrite it was guarding. Its
job — "the DateTimeOriginal we just wrote must read back identically" — was
about `tifftools` rewriting an existing file; nothing rewrites anything now.
Replace it with a `build_exif` unit test that parses the bytes it produces.

Metadata failures also change character: there is no longer a second write
that can fail after the pixels are safe, so the `METADATA_WRITE_FAILED`
downgrade path has nothing to downgrade. Keep the event code (§6 says why)
but the exporter's `_write_metadata` helper is deleted. A malformed
metadata value now fails the negative outright — `_export_negative`'s
existing broad `except` already turns it into `EXPORT_FAILED` — which is
correct: the file was never written.

### 5.3 Filenames

`negative.output["name"]` ends in `.tif`. The export destination becomes
`Path(negative.output["name"]).with_suffix(jxl_writer.JXL_SUFFIX)`. The
suffix constant lives in `jxl_writer.py` (§1.5), not in `exporter.py` — it
is a fact about the format. The `export_done` event's `output` field carries
the new name, so the Swift list shows `.jxl` with no Swift change.

Do not rename the published TIFF. It stays `.tif` and stays what it is.

### 5.4 Tests (`exporter_test.py`)

The existing suite reads exports back with `tifffile.imread` and
`tifftools.read_tiff`; those assertions move to `imagecodecs.jpegxl_decode`
and the §1.6 box helpers. Beyond the mechanical port:

- The exported file's extension is `.jxl` and the `export_done` event agrees.
- A colour roll's export is 3-channel and carries `EXPORT_RGB`; a mono
  roll's is 1-channel and carries `EXPORT_GREY`.
- A negative with a recorded `tone` op exports different pixels than the
  same negative without one, and the difference matches
  `tone.curve_values` (the op is genuinely applied, not accepted and
  ignored).
- The geometric replay is unchanged: rotation, flip and fine angle produce
  the same geometry as before, now composed with the render.
- A **colour** roll with no `camera_color` fails with
  `CAMERA_MATRIX_MISSING` before writing anything (assert the output
  directory is empty).
- A **mono** roll with no `camera_color` exports successfully (§3.4).
- One negative's failure still leaves the others exported, and the exit
  status is 1.
- The provenance XMP round-trips and contains the matrix and tone params.

---

## 6. CLI, events and contract

`--format` is **not** added. The user's decision is that JPEG XL replaces
TIFF outright; a format flag is surface with no chosen use, and an unused
flag is a maintenance cost plus a second set of tests. If Lightroom
compatibility (§0.5) forces a reconsideration, that is a new decision with
its own plan.

New codes in `events.py`:

- `JXL_ENCODER_UNAVAILABLE` — error; libjxl could not be reached (§1.2). A
  packaging failure, not a user error, and its message should say so.
- `CAMERA_MATRIX_MISSING` — error; the colour roll predates the
  colour-managed export (§3.4).
- `CAMERA_MATRIX_CONFLICT` — warning; a later run's source reports a
  different matrix (§3.2).

`METADATA_WRITE_FAILED` is **kept** even though §5.2 removes the
exporter's raiser (note it still has one in `apply_metadata.py`'s
DateOriginal rewrite — so the code is not orphaned by this plan either
way): it is in the shipped protocol and in Swift's `CLIEvent` handling,
and removing an event code is a breaking change for zero benefit. Mark it
reserved in `CONTRACT.md`.

**No change to the `export_done` payload.** The clip fraction lives in the
file's XMP (§4.4), not in the protocol.

Bump `PROTOCOL_VERSION` to 11 and update `shared/contract/CONTRACT.md` and
`schema.json` together. The export section (CONTRACT.md's export
subsection — the plan's original "lines 379–385" drifted as sections
landed above it; grep for "export" headings) needs rewriting rather than
editing — it currently describes writing TIFFs with edits applied and says
nothing about rendering, colour or tone. The error-code table near line 569
needs the three new entries. The `edit tone` section's claim that
"export ignores the op" / "the published TIFF is never touched" is now
false; fix it (the plan's original "line 18" reference drifted the same
way).

---

## 7. The Swift app

Deliberately small. The format is not a user choice, so no new control, and
with no `export_done` change there is no new decoding either.

Four copy sites still say "TIFF" and must say "a rendered positive in Adobe
RGB, written as a lossless JPEG XL" — with "the roll's own files are never
touched" still true throughout: `ExportStageView`'s body copy, its folder
picker's `message:`, the sentence above `CLIRunner.export`'s doc comment
(`CLIRunner.swift:312-316`), and `ExportedNegative`'s type comment.

The protocol version constant the probe-level tests compare goes to 11.
**That is the only change that can actually break the app**, and the
existing probe test will catch it — which is the intended mechanism, so do
not work around it.

No change to `ExportModel`'s phases, generation guard, or `clearResults`
semantics. Nothing about the export's *control flow* is changing.

---

## 8. Packaging

The `.dylibs` that `imagecodecs` ships are already collected by PyInstaller
(`cli/packaging/scanny_boy.spec` uses `collect_submodules("imagecodecs")`,
and PyInstaller follows each extension module's linked dependencies). §1.2's
`ctypes.CDLL(None)` approach is what makes this work frozen: no path is
hardcoded, and the symbols are already in the process by the time the shim
looks for them. (Verified live: every symbol §1.3 needs resolves through
`CDLL(None)` after `import imagecodecs._jpegxl`, against libjxl 0.12.0 /
imagecodecs 2026.8.16.)

Three additions:

- `packaging_test.py` gains a check that the frozen binary can import
  `jxl_writer` and encode a tiny image — the `--slow` packaged-app tier is
  where this belongs, and it is the only test that would catch a
  `.dylibs`-collection regression.
- The `.icc` files are **not** collected implicitly: the spec's `datas`
  lists each one explicitly. Add the two new profiles there — and fix the
  pre-existing gap this plan's review found: `ScannyBoy-Density-Grey-v1.icc`
  (mono step 1) was never added to the spec or to
  `test_bundle_carries_the_vetted_icc_profiles_and_its_own_metadata`, so the
  frozen app cannot export a mono roll today. The new names go into that
  test's enumeration too, or it keeps passing while checking the wrong set.

---

## 9. `DECISIONS.md` amendments

**D-2, "Two ICC profiles, and the profile is never load-bearing"** (which
PROFILE_HONESTY §3 has already partially rewritten — amend today's wording,
including its "wide container" table, rather than the pre-rename text this
plan was first written against) now describes three of five. Split its
central claim — never load-bearing *for the
intermediates and the published TIFF*, genuinely descriptive *for the
export* — and add an Export column:

| | Intermediates | Published TIFF | Export |
| --- | --- | --- | --- |
| TRC | true (linear) | viewing convention | true (Adobe RGB) |
| Primaries | wide container | wide container | true (matrix, §3) |

(`PROFILE_HONESTY_PLAN.md` §5 rewrites the first two columns' wording;
either order works.)

**"The published TIFF is a baked, normalized working intermediate"** — its
line "The deliverable is a positive export that does not exist yet" is now
satisfied. Rewrite, don't delete: the published TIFF is still the working
intermediate and still a negative; what changed is that the positive exists,
made at export.

**"Inversion is the print stage's (Phase 4)"** — record what this plan took
from Phase 4 (inversion, tone at full resolution) and what it left (a print
curve distinct from grade/snap, soft-proofing, paper simulation, printing).

**A new section**, whose one substantial part is **what the camera matrix
does and does not do** — reproduce §0.3, including the neutral-preserving
row normalization and the honest caveat, because that is the passage a
future reader is most likely to "simplify" into a plain matrix multiply. The
rest is a paragraph each: the export is a rendered positive and the
published TIFF is not (§0.2, §4.1); the matrix is recorded data, not a roll
invariant (§3.3); Adobe RGB over ProPhoto because the destination is
Lightroom, and the ProPhoto claim was never true of the pixels anyway (§0.1)
so this is not a gamut reduction from a correct state; the tone op stopped
being preview-only and `tone.py`/`previews.py`/`DECISIONS.md` were corrected
together (§4.6); there is no `--format` flag (§6).

---

## 10. Work order

0. **§0.5** — prototype JXL, confirm Lightroom imports it. Stop if not.
1. **§1** — `jxl_writer.py` and its tests. Nothing consumes it yet. Ship it.
2. **§2** — the two profiles, the generator's second construction path, the
   `ProfileKind` entries, the pinned hashes, §2.4's parametrized-test
   fixes. No pixel changes and **no invariant break**.
3. **§3** — the matrix, `rawpy` through the work manifest to the roll
   manifest, plus the two schemas. Do §3.1's direction check against a real
   NEF before going further. Still no behaviour change.
4. **§4** — `render.py` and its tests, including §4.7's two anchors. Not yet
   wired into the exporter.
5. **§5** — rewire `exporter.py` and `export_metadata.py`. Settle §5.2's
   `tifftools`-into-a-buffer question first. The output format changes here.
6. **§6, §7, §8** — protocol bump, Swift copy and version constant,
   packaging checks.
7. **§9** — `DECISIONS.md`, plus a `punchlist.md` entry for §11's deferrals.

Run `uv run pytest --slow` at steps **3, 5 and 6**: step 3 is the only one
that reads real RAW files, step 5 changes every exported byte, and step 6 is
the packaged-app tier where the `.dylibs` question is answered.

`docs/PROFILE_HONESTY_PLAN.md` is independent and can be done before, after,
or never. It is the only change that breaks the roll invariants; nothing
above does.

---

## 11. Explicitly out of scope

- **Any change to the published TIFF.** It stays 16-bit normalized log
  density, a negative, tagged with the density profile.
- **Renaming the three existing profiles** → `PROFILE_HONESTY_PLAN.md`.
- **Generating the intermediates' ICC profile per camera body** from §3's
  matrix — the prerequisite it was waiting on. Attachment point is
  `probe.py`'s invariant seeding. → `punchlist.md`.
- **Gamut compression, soft-proofing, or any UI for the clip fraction**
  (§4.4). It is measured and recorded in the file; acting on it is a
  separate feature.
- **A separate print curve** distinct from the preview's grade/snap.
- **White balance from `camera_whitebalance`/`daylight_whitebalance`**
  (§3.1). The per-channel normalization is the white balance.
- **A colour-space choice, a `--format` flag, lossy JXL, and migrating or
  re-exporting existing files.** One space and one container, both pinned;
  re-running the export is the migration.
